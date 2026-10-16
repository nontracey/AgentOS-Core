"""Synthetic replica media contracts; these tests never launch an inference model."""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import wave
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from agentos.adapters.aigc.media import compose, ffmpeg_run, probe_duration
from agentos.adapters.aigc.recipes import recipe_graph
from agentos.adapters.aigc.tools import AigcToolAdapter
from agentos.domains.aigc.director import Director
from agentos.domains.aigc.project import Project
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import CallContext, utc_now
from tests.integration.test_aigc import actor
from tests.integration.test_aigc_production import production


def guided_args() -> dict:
    return {'recipe': 'story-shot-guided', 'input_asset_id': 'head', 'input_asset_ref': 'head.png',
            'end_asset_id': 'tail', 'end_asset_ref': 'tail.png', 'prompt': 'a single short nod',
            'width': 256, 'height': 256, 'frames': 33, 'fps': 16, 'seed': 0}


def guided_adapter(tmp_path: Path) -> AigcToolAdapter:
    adapter = AigcToolAdapter(base_url='http://synthetic.invalid', sandbox_dir=tmp_path, ffmpeg='unused')
    adapter.assets.mkdir()
    Image.new('RGB', (32, 64), 'red').save(adapter.assets / 'head.png')
    Image.new('RGB', (64, 32), 'blue').save(adapter.assets / 'tail.png')
    return adapter


def install_scheduler(adapter, monkeypatch, *, lose_first_reply=False):
    observations = {'uploads': {}, 'submissions': [], 'requests': 0}
    ready = False

    def request(client, method, path, **kwargs):
        nonlocal ready
        observations['requests'] += 1
        if path.startswith('/.well-known'):
            return httpx.Response(200, json={'service': 'local-model-scheduler'})
        if path == '/object_info':
            graph = recipe_graph(guided_args(), 'head.png', 'synthetic', end_image_name='tail.png')
            return httpx.Response(200, json={node['class_type']: {} for node in graph.values()})
        if path == '/upload/image':
            name, stream, mime = kwargs['files']['image']
            assert mime == 'image/png' and name not in observations['uploads']
            observations['uploads'][name] = stream.read()
            return httpx.Response(200, json={})
        if path == '/prompt':
            observations['submissions'].append(kwargs['json'])
            ready = True
            if lose_first_reply and len(observations['submissions']) == 1:
                raise AgentOSError(ErrorCode.UNAVAILABLE, 'synthetic lost reply after submission')
            return httpx.Response(200, json={'prompt_id': kwargs['json']['prompt_id']})
        if path.startswith('/history/'):
            pid = path.rsplit('/', 1)[-1]
            return httpx.Response(200, json={pid: {'status': {'completed': True},
                'outputs': {'16': {'images': [{'filename': 'synthetic.webm'}]}}}} if ready else {})
        raise AssertionError(path)

    monkeypatch.setattr(adapter, '_request', request)
    monkeypatch.setattr(adapter, '_download', lambda *args: b'synthetic video output')
    return observations


@pytest.mark.parametrize('recipe,steps,split,cfg', [
    ('story-shot-guided', 4, 2, 1), ('story-shot-guided-quality', 20, 10, 3.5),
])
def test_guided_graph_wires_independent_endpoints_and_native_sampler_schedule(recipe, steps, split, cfg):
    graph = recipe_graph({**guided_args(), 'recipe': recipe}, 'start.png', 'synthetic', end_image_name='end.png')
    assert graph['1']['inputs']['image'] == 'start.png' and graph['20']['inputs']['image'] == 'end.png'
    assert graph['2']['inputs']['image'] == ['1', 0] and graph['21']['inputs']['image'] == ['20', 0]
    assert graph['10']['class_type'] == 'WanFirstLastFrameToVideo'
    assert graph['10']['inputs']['start_image'] == ['2', 0] and graph['10']['inputs']['end_image'] == ['21', 0]
    assert graph['10']['inputs']['length'] == 33 and graph['16']['inputs']['fps'] == 16
    high, low = graph['11']['inputs'], graph['14']['inputs']
    assert (high['steps'], high['start_at_step'], high['end_at_step']) == (steps, 0, split)
    assert (low['steps'], low['start_at_step'], low['end_at_step']) == (steps, split, steps)
    assert high['cfg'] == low['cfg'] == cfg and high['noise_seed'] == low['noise_seed'] == 0
    assert high['add_noise'] == 'enable' and low['add_noise'] == 'disable'
    assert low['latent_image'] == ['11', 0]
    loras = [node for node in graph.values() if node['class_type'] == 'LoraLoaderModelOnly']
    if steps == 4:
        assert {node['inputs']['lora_name'] for node in loras} == {
            'wan2.2_i2v_lightx2v_4step_high_noise.safetensors',
            'wan2.2_i2v_lightx2v_4step_low_noise.safetensors'}
        assert all(node['inputs']['strength_model'] == 1 for node in loras)
    else:
        assert not loras


@pytest.mark.parametrize('recipe', ['pet-motion', 'story-shot', 'story-shot-quality'])
@pytest.mark.parametrize('tail', ['name', 'id', 'ref'])
def test_ordinary_recipe_rejects_every_tail_input_form(recipe, tail):
    args = {'recipe': recipe, 'prompt': 'nod'}
    extra = {'end_image_name': 'end.png'} if tail == 'name' else {}
    if tail != 'name':
        args['end_asset_' + tail] = 'end.png'
    with pytest.raises(AgentOSError, match='does not consume an end image'):
        recipe_graph(args, 'start.png', 'synthetic', **extra)


def test_guided_adapter_uploads_correct_prepared_png_for_each_endpoint(tmp_path, monkeypatch):
    adapter = guided_adapter(tmp_path)
    seen = install_scheduler(adapter, monkeypatch)
    result = adapter._execute('aigc.generate', guided_args(), 'two-endpoints', 60)['value']
    assert result['visual_review'] == 'pending' and len(seen['uploads']) == 2
    graph = seen['submissions'][0]['prompt']
    names = [graph[node]['inputs']['image'] for node in ('1', '20')]
    assert names[0] != names[1] and set(names) == set(seen['uploads'])
    for name, color, filename in zip(names, [(255, 0, 0), (0, 0, 255)],
                                      ['prepared.png', 'prepared-end.png'], strict=True):
        data = seen['uploads'][name]
        assert data == (adapter.jobs / result['job_id'] / filename).read_bytes()
        with Image.open(io.BytesIO(data)) as image:
            assert image.size == (256, 256) and image.getpixel((128, 128)) == color
            assert image.getpixel((0, 0)) == (128, 128, 128)


@pytest.mark.asyncio
async def test_central_guided_transfer_authorizes_both_assets_and_prepares_both_images(
        system, tmp_path, monkeypatch):
    adapter = guided_adapter(tmp_path)
    seen = install_scheduler(adapter, monkeypatch)
    a = actor()
    head_data, tail_data = [(adapter.assets / name).read_bytes() for name in ('head.png', 'tail.png')]
    head = system.assets.register_local(a, head_data, mime='image/png')['asset_id']
    tail = system.assets.register_local(a, tail_data, mime='image/png')['asset_id']
    foreign = system.assets.register_local(actor('other'), b'foreign', mime='image/png')['asset_id']
    cap = next(spec for spec in adapter.list_tools(a) if spec.name == 'aigc.generate').model_dump(mode='json')
    system.nodes.pair(a.model_copy(update={'roles': ['admin']}), 'windows', 'http://fixture.invalid',
                      os='windows', arch='x64', token='synthetic')
    transferred, dispatched = {}, []

    def get(url, **kwargs):
        assert kwargs['trust_env'] is False
        if url.endswith('/tools'):
            return httpx.Response(200, json=[cap])
        return httpx.Response(200, content=(adapter.assets / url.rsplit('/', 1)[-1]).read_bytes())

    def post(url, **kwargs):
        assert kwargs['trust_env'] is False
        if url.endswith('/assets'):
            data, ref = kwargs['content'], kwargs['headers']['X-Asset-Filename']
            (adapter.assets / ref).write_bytes(data)
            transferred[ref] = data
            import hashlib

            return httpx.Response(200, json={'asset_id': ref, 'sha256': hashlib.sha256(data).hexdigest()})
        body = kwargs['json']
        dispatched.append(body)
        assert body['args']['end_asset_id'] == tail
        assert transferred[body['args']['input_asset_ref']] == head_data
        assert transferred[body['args']['end_asset_ref']] == tail_data
        value = adapter._execute(body['tool_name'], body['args'], body['idempotency_key'], 60)
        return httpx.Response(200, json=value)

    monkeypatch.setattr(httpx, 'get', get)
    monkeypatch.setattr(httpx, 'post', post)
    ctx = CallContext(actor=a, run_id='synthetic', step_id='guided', call_id='guided',
                      idempotency_key='central-guided', fence=1, deadline_at=utc_now() + timedelta(minutes=1))
    args = {key: value for key, value in guided_args().items() if not key.endswith('_asset_ref')}
    args.update(input_asset_id=head, end_asset_id=tail)
    result = await system.tool_hub.call('node_windows_aigc_generate', args, ctx)
    assert len(transferred) == 2 and len(seen['uploads']) == 2 and len(dispatched) == 1
    assert result.value['raw_video_asset_id']
    with pytest.raises(AgentOSError) as injected:
        await system.tool_hub.call('node_windows_aigc_generate', {**args, 'end_asset_ref': 'untrusted.png'}, ctx)
    assert injected.value.code == ErrorCode.DENIED
    with pytest.raises(AgentOSError) as unauthorized:
        await system.tool_hub.call('node_windows_aigc_generate', {**args, 'end_asset_id': foreign}, ctx)
    assert unauthorized.value.code == ErrorCode.DENIED and len(dispatched) == 1


@pytest.mark.parametrize('missing', ['end_asset_id', 'end_asset_ref'])
def test_guided_adapter_rejects_incomplete_endpoint_transfer_before_dispatch(tmp_path, monkeypatch, missing):
    adapter = guided_adapter(tmp_path)
    seen = install_scheduler(adapter, monkeypatch)
    args = guided_args()
    args.pop(missing)
    with pytest.raises(AgentOSError, match='transfer is incomplete'):
        adapter._execute('aigc.generate', args, 'incomplete', 60)
    assert not seen['submissions'] and not seen['uploads']


def test_completed_guided_result_still_rejects_incomplete_endpoint_transfer(tmp_path, monkeypatch):
    adapter = guided_adapter(tmp_path)
    seen = install_scheduler(adapter, monkeypatch)
    adapter._execute('aigc.generate', guided_args(), 'completed-endpoints', 60)
    args = guided_args()
    args.pop('end_asset_id')
    with pytest.raises(AgentOSError, match='transfer is incomplete'):
        adapter._execute('aigc.generate', args, 'completed-endpoints', 60)
    assert len(seen['submissions']) == 1


@pytest.mark.parametrize('endpoint', ['head.png', 'tail.png'])
def test_guided_lost_reply_recovers_completed_graph_and_endpoint_change_conflicts(tmp_path, monkeypatch, endpoint):
    adapter = guided_adapter(tmp_path)
    seen = install_scheduler(adapter, monkeypatch, lose_first_reply=True)
    with pytest.raises(AgentOSError, match='lost reply'):
        adapter._execute('aigc.generate', guided_args(), 'stable-guided', 60)
    result = adapter._execute('aigc.generate', guided_args(), 'stable-guided', 60)['value']
    assert len(seen['submissions']) == 1 and len(seen['uploads']) == 2
    assert result['prompt_id'] == seen['submissions'][0]['prompt_id']
    request_count = seen['requests']
    assert adapter._execute('aigc.generate', guided_args(), 'stable-guided', 60)['value']['reused']
    assert seen['requests'] == request_count
    Image.new('RGB', (32, 32), 'green').save(adapter.assets / endpoint)
    with pytest.raises(AgentOSError) as changed:
        adapter._execute('aigc.generate', guided_args(), 'stable-guided', 60)
    assert changed.value.code == ErrorCode.CONFLICT and seen['requests'] == request_count


def test_speech_request_reaches_worker_with_emotion_and_recovery_binds_voice_content(tmp_path, monkeypatch):
    adapter = AigcToolAdapter(base_url='http://synthetic.invalid', sandbox_dir=tmp_path, ffmpeg='unused',
                              speech_command=['registered-synthetic-worker'])
    adapter.assets.mkdir()
    voice = adapter.assets / 'voice.wav'
    voice.write_bytes(b'synthetic voice reference')
    emotion = [0, .2, 0, 0, .1, 0, 0, 0]
    args = {'text': 'A synthetic line.', 'input_asset_id': 'voice', 'input_asset_ref': voice.name,
            'emotion': emotion}
    requests = []

    def worker(command, **kwargs):
        assert command[0] == 'registered-synthetic-worker'
        request = json.loads(Path(command[-1]).read_text())
        requests.append(request)
        assert request['text'] == args['text'] and request['emotion'] == emotion
        assert Path(request['reference']) == voice
        with wave.open(request['output'], 'wb') as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(22050)
            output.writeframes(b'\0\0' * 2205)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr('agentos.adapters.aigc.tools.subprocess.run', worker)
    result = adapter._execute('aigc.speak', args, 'stable-emotion', 60)['value']
    assert result['visual_review'] == 'pending'
    assert adapter._execute('aigc.speak', args, 'stable-emotion', 60)['value']['reused']
    with pytest.raises(AgentOSError) as emotion_changed:
        adapter._execute('aigc.speak', {**args, 'emotion': [.3] + [0] * 7}, 'stable-emotion', 60)
    assert emotion_changed.value.code == ErrorCode.CONFLICT
    voice.write_bytes(b'changed synthetic voice reference')
    with pytest.raises(AgentOSError) as voice_changed:
        adapter._execute('aigc.speak', args, 'stable-emotion', 60)
    assert voice_changed.value.code == ErrorCode.CONFLICT and len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('extra', [
    {'worker_path': 'synthetic-outside/worker'}, {'reference': 'synthetic-outside/voice.wav'},
    {'output': 'synthetic-outside/output.wav'}, {'emotion': [0] * 7}, {'emotion': [0] * 9},
    {'emotion': [-.1] + [0] * 7}, {'emotion': [1.1] + [0] * 7},
    {'emotion': [True] + [0] * 7}, {'emotion': ['.2'] + [0] * 7},
    {'emotion': [float('nan')] + [0] * 7}, {'emotion': [float('inf')] + [0] * 7},
])
async def test_public_speech_contract_rejects_paths_and_invalid_emotion_before_execution(tmp_path, monkeypatch, extra):
    adapter = AigcToolAdapter(base_url='http://synthetic.invalid', sandbox_dir=tmp_path, ffmpeg='unused',
                              speech_command=['registered-synthetic-worker'])
    ctx = CallContext(actor=actor(), run_id='synthetic', step_id='speech', call_id='speech',
                      idempotency_key='synthetic', fence=1, deadline_at=utc_now() + timedelta(minutes=1))

    def must_not_execute(*args):
        raise AssertionError('Invalid public arguments reached execution')

    monkeypatch.setattr(adapter, '_execute', must_not_execute)
    with pytest.raises(AgentOSError) as rejected:
        await adapter.call('aigc.speak', {'text': 'Synthetic.', 'input_asset_id': 'voice', **extra}, ctx)
    assert rejected.value.code == ErrorCode.INVALID


def test_output_30fps_resamples_16fps_clips_without_truncating_last_clip_or_subtitles(tmp_path):
    ffmpeg = shutil.which('ffmpeg')
    ffprobe = shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        pytest.skip('FFmpeg and FFprobe are required for actual timeline verification')
    clips = []
    for name, color in [('first', 'red'), ('last', 'blue')]:
        clip = tmp_path / (name + '.mp4')
        ffmpeg_run(ffmpeg, ['-f', 'lavfi', '-i', f'color=c={color}:s=256x256:r=16:d=1',
                            '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(clip)])
        clips.append(clip)
    voice = tmp_path / 'voice.wav'
    ffmpeg_run(ffmpeg, ['-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.4', str(voice)])
    data = production('synthetic-image', 'synthetic-voice')
    data.update(width=256, height=256, fps=16, output_width=1280, output_height=720, output_fps=30)
    data['production']['dialogue'] = []
    data['shots'][0].update(frames=17, duration=1, selected_asset_id='first')
    second = {**data['shots'][0], 'id': 'two', 'selected_asset_id': 'last'}
    data['shots'].append(second)
    data['subtitles'] = [{'start': .1, 'end': .4, 'text': 'Synthetic caption.'}]
    project = Project.model_validate(data)
    assert Director.video_args(project, project.shots[0])['fps'] == 16
    assert Director.video_args(project, project.shots[0])['frames'] == 17
    args = Director.timeline_args(project, still=False)
    output = tmp_path / 'film.mp4'
    duration = compose(ffmpeg, clips, tmp_path, output, width=args['width'], height=args['height'],
                       fps=args['fps'], audio=voice, subtitles=args['subtitles'], edits=args['clip_edits'])
    assert duration == pytest.approx(2, abs=1 / 30)
    assert probe_duration(ffmpeg, output) == pytest.approx(2, abs=1 / 30)
    probe = subprocess.run([ffprobe, '-v', 'error', '-count_frames', '-show_streams', '-of', 'json', str(output)],
                           check=True, capture_output=True)
    streams = json.loads(probe.stdout)['streams']
    video = next(stream for stream in streams if stream['codec_type'] == 'video')
    assert (video['width'], video['height'], video['r_frame_rate']) == (1280, 720, '30/1')
    assert int(video['nb_read_frames']) == 60
    assert next(stream for stream in streams if stream['codec_type'] == 'subtitle')['codec_name'] == 'mov_text'
    assert (tmp_path / 'subtitles.srt').read_text().endswith('Synthetic caption.\n')
    assert next(stream for stream in streams if stream['codec_type'] == 'audio')['duration'] == '2.000000'
    ffmpeg_run(ffmpeg, ['-i', str(output), '-f', 'null', '-'])
    last = tmp_path / 'last-frame.png'
    ffmpeg_run(ffmpeg, ['-sseof', '-0.05', '-i', str(output), '-frames:v', '1', str(last)])
    with Image.open(last) as image:
        red, green, blue = image.convert('RGB').getpixel((640, 360))
        assert blue > 220 and red < 30 and green < 30
