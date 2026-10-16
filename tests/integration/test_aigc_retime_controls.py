"""Opt-in retiming reaches the lip worker before target-time dialogue, with synthetic media."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from agentos.adapters.aigc.media import ffmpeg_run
from agentos.adapters.aigc.tools import AigcToolAdapter
from agentos.domains.aigc.director import Director
from agentos.domains.aigc.project import Project
from agentos.kernel.errors import AgentOSError, ErrorCode
from tests.integration.test_aigc import actor
from tests.integration.test_aigc_native_retime import assert_blue, assert_red, frames_rgb, moving_native
from tests.integration.test_aigc_production import production


def speaking_project():
    data = production('image', 'voice')
    data['output_fps'] = 30
    data['shots'][0].update(selected_asset_id='native', selected_run_id='generated', review='accepted',
        duration=43 / 30, frames=25, retime_native=True,
        lipsync={'character_id': 'examiner', 'face_box': [.1, .1, .9, .9]})
    data['production']['dialogue'][0].update(start=.1, end=1.2)
    return Project.model_validate(data)


def test_retime_is_opt_in_for_timeline_and_lipsync_fps_but_native_generation_stays_at_sixteen():
    project = speaking_project()
    shot = project.shots[0]
    lip = Director.lipsync_args(project, shot)
    assert lip['retime_native'] is True and lip['fps'] == 30 and lip['duration'] == 43 / 30
    assert lip['dialogue_timing'] == [{'start': .1, 'end': 1.2}]
    video = Director.video_args(project, shot)
    assert video['fps'] == 16 and video['frames'] == 25 and 'retime_native' not in video
    shot.lipsync.asset_id, shot.lipsync.run_id, shot.lipsync.review = 'spoken', 'lip-run', 'accepted'
    timeline = Director.timeline_args(project, still=False)
    assert timeline['clip_edits'] == [{'start': 0, 'duration': 43 / 30, 'retime_native': True}]
    assert Director.timeline_args(project, still=True)['clip_edits'] == [{'start': 0, 'duration': 43 / 30}]
    shot.retime_native = False
    legacy = Director.lipsync_args(project, shot)
    assert legacy['fps'] == 16 and 'retime_native' not in legacy
    assert 'retime_native' not in Director.timeline_args(project, still=False)['clip_edits'][0]
    plain = Project.model_validate(production('image', 'audio'))
    assert Director.timeline_args(plain, still=True)['plan_digest'] == (
        'c4595d84466a934b2e311cdadbda6c7079fce4cb84f735cf9b54653651276202')


@pytest.mark.parametrize('value,trim,duration', [(1, 0, 3), ('true', 0, 3), (True, .1, 3), (True, 0, None)])
def test_whole_native_option_rejects_coercion_partial_trim_or_missing_target(value, trim, duration):
    data = production('image', 'voice')
    data['shots'][0].update(retime_native=value, trim_in=trim, duration=duration)
    with pytest.raises(ValidationError):
        Project.model_validate(data)


def test_explicit_retime_can_stretch_complete_native_span_without_requesting_more_generation_frames():
    project = Project.model_validate(production('image', 'audio'))
    project.production.animatic_asset_id = 'coarse'
    project.production.animatic_run_id = 'reviewed'
    project.production.animatic_review = 'accepted'
    project.shots[0].duration = 6
    with pytest.raises(AgentOSError, match='enough native frames'):
        Director.ready(project, video=True)
    project.shots[0].retime_native = True
    Director.ready(project, video=True)
    assert Director.video_args(project, project.shots[0])['frames'] == 65


def test_lip_worker_receives_whole_native_retimed_at_target_thirty_fps_before_audio(tmp_path, monkeypatch):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg or not shutil.which('ffprobe'):
        pytest.skip('Real FFmpeg is required for lip preprocessing')
    adapter = AigcToolAdapter(base_url='http://synthetic.invalid', sandbox_dir=tmp_path / 'runtime',
                              ffmpeg=ffmpeg, lipsync_command=['registered-synthetic-lip-worker'])
    adapter.assets.mkdir()
    source = moving_native(ffmpeg, tmp_path, frames=25)
    movie = adapter.assets / 'native.mkv'
    shutil.copyfile(source, movie)
    original_sha = hashlib.sha256(movie.read_bytes()).hexdigest()
    audio = adapter.assets / 'voice.wav'
    ffmpeg_run(ffmpeg, ['-f', 'lavfi', '-i', 'sine=frequency=440:duration=1.1:sample_rate=48000',
                        '-c:a', 'pcm_s16le', str(audio)])
    project = speaking_project()
    args = Director.lipsync_args(project, project.shots[0])
    args.update(input_asset_ref='native.mkv', dialogue_asset_refs=['voice.wav'])
    original = subprocess.run
    calls = []

    def run(command, **kwargs):
        if command[0] != 'registered-synthetic-lip-worker':
            return original(command, **kwargs)
        request = json.loads(Path(command[-1]).read_text())
        frames = frames_rgb(ffmpeg, Path(request['video']))
        assert len(frames) == 43
        assert_red(frames[0].getpixel((4, 4)))
        assert_blue(frames[-1].getpixel((4, 4)))
        assert request['fps'] == 30 and request['active'] == [{'start': .1, 'end': 1.2}]
        calls.append(request)
        shutil.copyfile(request['video'], request['output'])
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr('agentos.adapters.aigc.tools.subprocess.run', run)
    result = adapter._execute('aigc.lipsync', args, 'retime-before-lip', 60)['value']
    assert result['duration'] == 43 / 30 and result['visual_review'] == 'pending'
    assert len(frames_rgb(ffmpeg, adapter.assets / result['asset_ref'])) == 43
    assert hashlib.sha256(movie.read_bytes()).hexdigest() == original_sha
    cached = adapter._execute('aigc.lipsync', args, 'retime-before-lip', 60)['value']
    assert cached['reused'] and cached['sha256'] == result['sha256'] and len(calls) == 1
    with pytest.raises(AgentOSError) as changed:
        adapter._execute('aigc.lipsync', {**args, 'retime_native': False}, 'retime-before-lip', 60)
    assert changed.value.code == ErrorCode.CONFLICT and len(calls) == 1


def test_public_retime_schema_is_boolean_and_rejects_partial_lip_offsets(tmp_path):
    adapter = AigcToolAdapter(base_url='http://synthetic.invalid', sandbox_dir=tmp_path, ffmpeg='unused',
                              lipsync_command=['registered-synthetic-lip-worker'])
    specs = {spec.name: spec.input_schema for spec in adapter.list_tools(actor())}
    from jsonschema import ValidationError as JsonError
    from jsonschema import validate
    project = speaking_project()
    args = Director.lipsync_args(project, project.shots[0])
    validate(args, specs['aigc.lipsync'])
    for change in ({'retime_native': 1}, {'start': .1}):
        with pytest.raises(JsonError):
            validate({**args, **change}, specs['aigc.lipsync'])
    for name in ('aigc.compose', 'aigc.animatic'):
        assert specs[name]['properties']['clip_edits']['items']['properties']['retime_native'] == {'type': 'boolean'}
