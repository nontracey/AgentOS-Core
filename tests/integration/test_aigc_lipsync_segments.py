"""Segmented speech windows, verified with real FFmpeg and a synthetic lip worker."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import wave
from array import array
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentos.adapters.aigc.media import ffmpeg_run, mix_dialogue, probe_duration
from agentos.adapters.aigc.tools import AigcToolAdapter
from agentos.domains.aigc.director import Director
from agentos.domains.aigc.production import LipSync
from agentos.domains.aigc.project import Project
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import CallContext, utc_now
from tests.integration.test_aigc import actor
from tests.integration.test_aigc_production import production


@pytest.fixture
def tones(tmp_path):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg or not shutil.which('ffprobe'):
        pytest.skip('FFmpeg and FFprobe are required for speech-window validation')
    audio = tmp_path / 'line.wav'
    ffmpeg_run(ffmpeg, ['-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=1',
        '-f', 'lavfi', '-i', 'sine=frequency=880:sample_rate=48000:duration=1',
        '-filter_complex', '[0:a][1:a]concat=n=2:v=0:a=1[out]', '-map', '[out]',
        '-c:a', 'pcm_s16le', str(audio)])
    return ffmpeg, audio


def segmented_project() -> Project:
    project = Project.model_validate(production('image', 'audio'))
    line = project.production.dialogue[0]
    line.start, line.end = .5, 2.5
    shot = project.shots[0]
    shot.duration = 1.5
    shot.selected_asset_id, shot.selected_run_id, shot.review = 'video-one', 'run-one', 'accepted'
    shot.lipsync = LipSync(character_id='examiner', face_box=[.1, .1, .9, .9])
    second = shot.model_copy(deep=True, update={'id': 'two', 'selected_asset_id': 'video-two',
                                              'selected_run_id': 'run-two'})
    project.shots.append(second)
    return project


def samples(path: Path, start: float, end: float) -> tuple[array, int]:
    with wave.open(str(path), 'rb') as stream:
        rate, channels = stream.getframerate(), stream.getnchannels()
        assert stream.getsampwidth() == 2
        stream.setpos(round(start * rate))
        data = array('h', stream.readframes(round((end - start) * rate)))
    return data[::channels], rate


def tone_frequency(path: Path, start: float, end: float) -> float:
    data, rate = samples(path, start, end)
    assert max(abs(value) for value in data) > 1000
    crossings = sum(left < 0 <= right for left, right in zip(data, data[1:], strict=False))
    return crossings / (len(data) / rate)


def install_worker(monkeypatch):
    original = subprocess.run
    requests = []

    def run(command, **kwargs):
        if command[0] != 'registered-synthetic-lip-worker':
            return original(command, **kwargs)
        request = json.loads(Path(command[-1]).read_text())
        requests.append(request)
        # Only the speech input and media wrapper are tested, not a lip model.
        shutil.copyfile(request['video'], request['output'])
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr('agentos.adapters.aigc.tools.subprocess.run', run)
    return requests


def test_director_intersects_one_line_without_changing_absolute_dialogue_or_compose_timing():
    project = segmented_project()
    first = Director.lipsync_args(project, project.shots[0])
    second = Director.lipsync_args(project, project.shots[1])
    assert first['dialogue_asset_ids'] == second['dialogue_asset_ids'] == ['audio']
    assert first['dialogue_timing'] == [{'start': .5, 'end': 1.5, 'source_start': 0}]
    assert second['dialogue_timing'] == [{'start': 0, 'end': 1, 'source_start': 1}]
    assert (project.production.dialogue[0].start, project.production.dialogue[0].end) == (.5, 2.5)
    assert Director.timeline_args(project, still=True)['dialogue_timing'] == [{'start': .5, 'end': 2.5}]
    project.production.dialogue[0].audio_asset_id = None
    with pytest.raises(AgentOSError, match='bind audio'):
        Director.lipsync_args(project, project.shots[0])


def test_adjacent_lip_jobs_take_correct_audio_halves_and_keep_slot_padding(tones, tmp_path, monkeypatch):
    ffmpeg, source = tones
    adapter = AigcToolAdapter(base_url='http://synthetic.invalid', sandbox_dir=tmp_path / 'runtime',
                              ffmpeg=ffmpeg, lipsync_command=['registered-synthetic-lip-worker'])
    adapter.assets.mkdir()
    shutil.copyfile(source, adapter.assets / 'line.wav')
    movie = adapter.assets / 'native.mp4'
    ffmpeg_run(ffmpeg, ['-f', 'lavfi', '-i', 'color=c=blue:s=256x256:r=16:d=2',
                        '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(movie)])
    original_sha = hashlib.sha256((adapter.assets / 'line.wav').read_bytes()).hexdigest()
    project = segmented_project()
    requests = install_worker(monkeypatch)
    results, segment_args = [], []
    for shot in project.shots:
        args = Director.lipsync_args(project, shot)
        args.update(input_asset_ref='native.mp4', dialogue_asset_refs=['line.wav'])
        segment_args.append(args)
        results.append(adapter._execute('aigc.lipsync', args, 'segment-' + shot.id, 60)['value'])
    assert len(requests) == 2
    first, second = [Path(request['audio']) for request in requests]
    assert probe_duration(ffmpeg, first) == pytest.approx(1.5, abs=1 / 48000)
    assert probe_duration(ffmpeg, second) == pytest.approx(1.5, abs=1 / 48000)
    assert tone_frequency(first, .6, 1.3) == pytest.approx(440, abs=3)
    assert tone_frequency(second, .1, .8) == pytest.approx(880, abs=3)
    assert tone_frequency(first, 1.4, 1.49) == pytest.approx(440, abs=15)
    assert tone_frequency(second, .9, .99) == pytest.approx(880, abs=15)
    assert max(abs(value) for value in samples(first, .05, .4)[0]) == 0
    assert max(abs(value) for value in samples(second, 1.1, 1.4)[0]) == 0
    assert requests[0]['active'] == [{'start': .5, 'end': 1.5, 'source_start': 0}]
    assert requests[1]['active'] == [{'start': 0, 'end': 1, 'source_start': 1}]
    assert hashlib.sha256((adapter.assets / 'line.wav').read_bytes()).hexdigest() == original_sha
    assert all(result['visual_review'] == 'pending' for result in results)
    restored = AigcToolAdapter(base_url='http://synthetic.invalid', sandbox_dir=tmp_path / 'runtime',
                              ffmpeg=ffmpeg, lipsync_command=['registered-synthetic-lip-worker'])
    cached = restored._execute('aigc.lipsync', segment_args[0], 'segment-' + project.shots[0].id, 60)['value']
    assert cached['reused'] and cached['sha256'] == results[0]['sha256'] and len(requests) == 2
    changed = {**segment_args[0], 'dialogue_timing': [{'start': .5, 'end': 1.5, 'source_start': .1}]}
    with pytest.raises(AgentOSError) as conflict:
        restored._execute('aigc.lipsync', changed, 'segment-' + project.shots[0].id, 60)
    assert conflict.value.code == ErrorCode.CONFLICT and len(requests) == 2


def test_whole_line_request_fingerprint_and_completed_cache_remain_compatible(tones, tmp_path, monkeypatch):
    ffmpeg, source = tones
    adapter = AigcToolAdapter(base_url='http://synthetic.invalid', sandbox_dir=tmp_path / 'runtime',
                              ffmpeg=ffmpeg, lipsync_command=['registered-synthetic-lip-worker'])
    adapter.assets.mkdir()
    shutil.copyfile(source, adapter.assets / 'line.wav')
    video = adapter.assets / 'native.mp4'
    ffmpeg_run(ffmpeg, ['-f', 'lavfi', '-i', 'color=c=red:s=256x256:r=16:d=3',
                        '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(video)])
    project = Project.model_validate(production('image', 'audio'))
    shot = project.shots[0]
    shot.selected_asset_id, shot.review = 'video', 'accepted'
    shot.lipsync = LipSync(character_id='examiner', face_box=[.1, .1, .9, .9])
    legacy = {'input_asset_id': 'video', 'start': 0, 'duration': 3.0, 'fps': 16, 'face_box': [.1, .1, .9, .9],
              'dialogue_asset_ids': ['audio'], 'dialogue_timing': [{'start': 0.0, 'end': 2.0}]}
    args = Director.lipsync_args(project, shot)
    assert json.dumps(args, sort_keys=True) == json.dumps(legacy, sort_keys=True)
    args.update(input_asset_ref='native.mp4', dialogue_asset_refs=['line.wav'])
    normalized = {key: value for key, value in legacy.items() if not key.endswith(('_asset_id', '_asset_ids'))}
    hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in (video, adapter.assets / 'line.wav')]
    old_signature = hashlib.sha256(json.dumps(['aigc.lipsync', normalized, hashes],
                                             sort_keys=True, allow_nan=False).encode()).hexdigest()
    requests = install_worker(monkeypatch)
    result = adapter._execute('aigc.lipsync', args, 'legacy-whole-line', 60)['value']
    record = json.loads((adapter.jobs / result['job_id'] / 'job.json').read_text())
    assert record['fingerprint'] == old_signature
    recovered = AigcToolAdapter(base_url='http://synthetic.invalid', sandbox_dir=tmp_path / 'runtime',
                                ffmpeg=ffmpeg, lipsync_command=['registered-synthetic-lip-worker'])
    cached = recovered._execute('aigc.lipsync', args, 'legacy-whole-line', 60)['value']
    assert cached['reused'] and cached['sha256'] == result['sha256'] and len(requests) == 1
    assert 'source_start' not in requests[0]['active'][0]
    (adapter.assets / 'line.wav').write_bytes(b'changed source')
    with pytest.raises(AgentOSError) as changed:
        recovered._execute('aigc.lipsync', args, 'legacy-whole-line', 60)
    assert changed.value.code == ErrorCode.CONFLICT


@pytest.mark.parametrize('cue', [
    {'start': 0, 'end': 1, 'source_start': -.1}, {'start': 0, 'end': 1, 'source_start': 1.1},
    {'start': 0, 'end': 1, 'source_start': float('nan')},
    {'start': 0, 'end': 1, 'source_start': float('inf')}, {'start': 0, 'end': 1, 'source_start': True},
    {'start': 0, 'end': 1, 'source_start': '0'}, {'start': 0, 'end': 1.51, 'source_start': 0},
    {'start': 0, 'end': 0, 'source_start': 0}, {'start': 0, 'end': 1},
])
def test_invalid_source_windows_and_unsliced_full_audio_do_not_enter_short_slots(tones, tmp_path, cue):
    ffmpeg, source = tones
    work = tmp_path / 'mix'
    work.mkdir()
    with pytest.raises(AgentOSError):
        mix_dialogue(ffmpeg, work, 1.5, [source], [cue])
    assert not (work / 'dialogue-master.wav').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('cue', [
    {'start': 0, 'end': 1, 'source_start': float('nan')},
    {'start': 0, 'end': 1, 'source_start': -.1}, {'start': float('nan'), 'end': 1, 'source_start': 0},
    {'start': 0, 'end': float('inf'), 'source_start': 0},
])
async def test_public_partial_timing_is_finite_before_execution(tmp_path, monkeypatch, cue):
    adapter = AigcToolAdapter(base_url='http://synthetic.invalid', sandbox_dir=tmp_path, ffmpeg='unused',
                              lipsync_command=['registered-synthetic-lip-worker'])
    ctx = CallContext(actor=actor(), run_id='synthetic', step_id='lip', call_id='lip',
                      idempotency_key='invalid', fence=1, deadline_at=utc_now() + timedelta(minutes=1))
    args = {'input_asset_id': 'video', 'start': 0, 'duration': 1.5, 'fps': 16,
            'face_box': [.1, .1, .9, .9], 'dialogue_asset_ids': ['audio'], 'dialogue_timing': [cue]}

    def must_not_execute(*args):
        raise AssertionError('Invalid partial speech timing reached execution')

    monkeypatch.setattr(adapter, '_execute', must_not_execute)
    with pytest.raises(AgentOSError) as invalid:
        await adapter.call('aigc.lipsync', args, ctx)
    assert invalid.value.code == ErrorCode.INVALID


def test_compose_and_animatic_schema_keep_whole_dialogue_contract(tmp_path):
    adapter = AigcToolAdapter(base_url='http://synthetic.invalid', sandbox_dir=tmp_path, ffmpeg='unused',
                              lipsync_command=['registered-synthetic-lip-worker'])
    schemas = {spec.name: spec.input_schema for spec in adapter.list_tools(actor())}
    assert 'source_start' in schemas['aigc.lipsync']['properties']['dialogue_timing']['items']['properties']
    for name in ('aigc.compose', 'aigc.animatic'):
        assert 'source_start' not in schemas[name]['properties']['dialogue_timing']['items']['properties']
