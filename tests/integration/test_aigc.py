"""Synthetic production regressions; real GPU evidence is recorded separately."""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import zipfile
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from agentos.adapters.aigc.media import compose, ffmpeg_run, pack_alpha, prepare_image, probe_duration
from agentos.adapters.aigc.recipes import recipe_graph
from agentos.adapters.aigc.tools import AigcToolAdapter
from agentos.apps.gateway import create_app
from agentos.domains.aigc.project import Project
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import AuthContext, CallContext, utc_now
from agentos.kernel.workflow_graph import FlowGraph
from agentos.services.aigc import AigcService
from tests.conftest import add_subject, owner_token


def actor(subject: str = 'owner') -> AuthContext:
    return AuthContext(subject=subject, tenant_id='default', roles=['member'],
        scopes=['read', 'write', 'exec'], policy_version='test', expires_at=utc_now()+timedelta(hours=2))


def payload(asset_id: str) -> dict[str, Any]:
    return {'name': 'Synthetic story', 'kind': 'story', 'node_id': 'windows',
        'shots': [{'id': 'one', 'input_asset_id': asset_id, 'prompt': 'a small nod', 'seed': 0}]}


def test_recipe_preserves_zero_seed_and_separates_pet_graph() -> None:
    graph = recipe_graph({'recipe': 'pet-motion', 'prompt': 'wave', 'seed': 0}, 'x.png', 'aigc/test')
    assert graph['11']['inputs']['noise_seed'] == 0
    assert graph['14']['inputs']['noise_seed'] == 0
    assert graph['10']['inputs']['start_image'] == graph['10']['inputs']['end_image']
    assert '24' in graph
    shot = recipe_graph({'recipe': 'story-shot', 'prompt': 'nod'}, 'x.png', 'aigc/test')
    assert shot['10']['class_type'] == 'WanImageToVideo' and '24' not in shot
    with pytest.raises(AgentOSError):
        recipe_graph({'prompt': 'wave', 'frames': 64}, 'x.png', 'aigc/test')


def test_project_asset_ownership_revision_and_review(system) -> None:
    a = actor()
    asset = system.assets.register_local(a, b'synthetic', mime='image/png')['asset_id']
    service = AigcService(system.drafts, system.assets, system.workflows, system.deps.tools)
    p = payload(asset)
    assert service.save(a, 'project', p, 0)['revision'] == 1
    with pytest.raises(AgentOSError) as conflict:
        service.save(a, 'project', p, 0)
    assert conflict.value.code == ErrorCode.CONFLICT
    with pytest.raises(AgentOSError):
        service.save(actor('another'), 'project', p, 0)
    graph = FlowGraph.model_validate(service.graph(a, 'project', Project.model_validate(p), 'generate'))
    assert graph.nodes[0].config['arguments']['seed'] == 0
    with pytest.raises(AgentOSError):
        service.graph(a, 'project', Project.model_validate(p), 'compose')
    movie = system.assets.register_local(a, b'movie', mime='video/mp4')['asset_id']
    published = system.workflows.publish(a, graph.model_dump(mode='json'))
    execution = system.workflows.start(a, published['workflow_id'], published['version'], {}, 'candidate')
    run_id = execution['internal_run_id']
    with TestClient(create_app(system=system)) as c:
        headers = {'Authorization': 'Bearer '+owner_token(system)}
        assert c.get('/v1/runs', headers=headers, params={'q': 'Synthetic story'}).json()[0][
            'goal'] == 'Synthetic story · 生成候选'
        assert c.get('/v1/tasks/'+run_id, headers=headers).json()['run']['goal_text'] == (
            'Synthetic story · 生成候选')
    system.store.save_checkpoint(a.tenant_id, run_id, json.dumps({'workflow_nodes': {
        'shot_one': {'state': 'completed', 'output': {'asset_id': movie}}}}))
    p['shots'][0].update(review='accepted', selected_asset_id=movie, selected_run_id=run_id)
    assert service.graph(a, 'project', service.project(a, p), 'compose')['nodes'][0]['config']['arguments'][
        'clip_asset_ids'] == [movie]
    assert service.save(a, 'project', p, 1)['revision'] == 2
    p['shots'][0]['prompt'] = 'changed action'
    with pytest.raises(AgentOSError) as stale:
        service.save(a, 'project', p, 2)
    assert stale.value.code == ErrorCode.CONFLICT
    p['shots'][0]['prompt'] = 'a small nod'
    with pytest.raises(AgentOSError):
        service.save(a, 'other-project', p, 0)
    p['shots'][0]['selected_asset_id'] = system.assets.register_local(a, b'other', mime='video/mp4')['asset_id']
    with pytest.raises(AgentOSError):
        service.save(a, 'project', p, 2)


def test_media_upload_and_private_project_routes(system) -> None:
    with TestClient(create_app(system=system)) as c:
        headers = {'Authorization': 'Bearer '+owner_token(system), 'Content-Type': 'image/png',
                   'X-Media-Name': '%E8%A7%92%E8%89%B2.png'}
        image = io.BytesIO()
        Image.new('RGB', (32, 32)).save(image, format='PNG')
        r = c.post('/v1/aigc/materials', headers=headers, content=image.getvalue())
        assert r.status_code == 201
        asset = r.json()['asset_id']
        assert c.post('/v1/aigc/materials', headers=headers, content=image.getvalue()).json()['asset_id'] == asset
        assert c.post('/v1/aigc/materials', headers=headers, content=b'wrong').status_code == 422
        headers = {'Authorization': headers['Authorization']}
        r = c.post('/v1/aigc/projects/p', headers=headers, json={'payload': payload(asset), 'expected_revision': 0})
        assert r.status_code == 200
        token = add_subject(system, 'second', ['read', 'write', 'exec'])
        other = {'Authorization': 'Bearer '+token}
        assert c.get('/v1/aigc/projects', headers=other).json() == []
        assert c.post('/v1/aigc/projects/x', headers=other,
                      json={'payload': payload(asset), 'expected_revision': 0}).status_code == 401
        assert c.post('/v1/aigc/projects/p/start', headers=headers,
                      json={'revision': 1, 'idempotency_key': 'test', 'stage': 'generate'}).status_code == 503


def test_generation_reconciles_lost_submission_without_new_uuid(tmp_path: Path, monkeypatch) -> None:
    adapter = AigcToolAdapter(base_url='http://synthetic.invalid', sandbox_dir=tmp_path, ffmpeg='unused')
    adapter.assets.mkdir()
    Image.new('RGB', (512, 800)).save(adapter.assets/'input.png')
    args = {'recipe': 'story-shot', 'input_asset_id': 'central', 'input_asset_ref': 'input.png',
            'prompt': 'nod', 'seed': 0}
    ids = []
    uploaded = []
    ready = False
    def request(client, method, path, **kwargs):
        nonlocal ready
        if path.startswith('/.well-known'):
            return httpx.Response(200, json={'service': 'local-model-scheduler'})
        if path == '/object_info':
            graph = recipe_graph(args, 'x.png', 'aigc')
            return httpx.Response(200, json={n['class_type']: {} for n in graph.values()})
        if path == '/upload/image':
            uploaded.append(1)
            return httpx.Response(200, json={})
        if path == '/prompt':
            ids.append(kwargs['json']['prompt_id'])
            if len(ids) == 1:
                raise AgentOSError(ErrorCode.UNAVAILABLE, 'lost reply')
            ready = True
            return httpx.Response(200, json={'prompt_id': ids[-1]})
        if path.startswith('/history/'):
            pid = path.rsplit('/', 1)[-1]
            return httpx.Response(200, json={pid: {'status': {'completed': True},
                'outputs': {'3': {'images': [{'filename': 'reference.png'}]},
                            '16': {'images': [{'filename': 'raw.webm'}]}}}} if ready else {})
        raise AssertionError(path)
    monkeypatch.setattr(adapter, '_request', request)
    downloaded = []
    def download(client, item):
        downloaded.append(item['filename'])
        return b'synthetic-video'
    monkeypatch.setattr(adapter, '_download', download)
    with pytest.raises(AgentOSError):
        adapter._execute('aigc.generate', args, 'same-key', 60)
    result = adapter._execute('aigc.generate', args, 'same-key', 60)['value']
    assert ids[0] == ids[1] and len(uploaded) == 1
    assert downloaded == ['raw.webm']
    assert result['visual_review'] == 'pending'
    assert adapter._execute('aigc.generate', args, 'same-key', 60)['value']['reused']
    with pytest.raises(AgentOSError) as conflict:
        adapter._execute('aigc.generate', {**args, 'prompt': 'changed'}, 'same-key', 60)
    assert conflict.value.code == ErrorCode.CONFLICT
    out = adapter.assets/result['asset_ref']
    out.write_bytes(b'corrupt')
    with pytest.raises(AgentOSError) as corrupt:
        adapter._execute('aigc.generate', args, 'same-key', 60)
    assert corrupt.value.code == ErrorCode.UNCERTAIN


def test_alpha_pack_and_reference_padding(tmp_path: Path) -> None:
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        pytest.skip('FFmpeg not installed in this test environment')
    source = tmp_path/'reference.png'
    Image.new('RGBA', (16, 32), (255, 0, 0, 0)).save(source)
    prepared = tmp_path/'prepared.png'
    prepare_image(ffmpeg, source, prepared, 256, 256)
    assert Image.open(prepared).getpixel((128, 128)) == (128, 128, 128)
    archive = tmp_path/'frames.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        for i in range(5):
            frame = Image.new('RGBA', (32, 64), (128, 128, 128, 0))
            frame.paste((255, 0, 0, 255), (8, 8, 24, 56))
            content = io.BytesIO()
            frame.save(content, format='PNG')
            z.writestr(f'{i:04d}.png', content.getvalue())
    result = pack_alpha(ffmpeg, archive, tmp_path, 16, tmp_path/'playback.mp4')
    assert result['frames'] == 5 and result['width'] == 32
    assert result['layout'].startswith('premultiplied')
    assert (tmp_path/'playback.mp4').stat().st_size > 0


async def test_node_transfers_authorized_input_and_all_output_roles(system, monkeypatch) -> None:
    a = actor()
    source = system.assets.register_local(a, b'synthetic-image', mime='image/png')['asset_id']
    caps = [{'name': 'aigc.generate', 'version': '1', 'input_schema': {'type': 'object'},
             'risks': ['read'], 'recovery': 'idempotency_key'}]
    system.nodes.pair(a.model_copy(update={'roles': ['admin']}), 'windows', 'http://fixture.invalid',
                      os='windows', arch='x64', token='fixture')
    contents = {'raw.webm': b'raw', 'frames.zip': b'alpha', 'record.json': b'{}'}
    outputs = [{'asset_ref': ref, 'sha256': hashlib.sha256(data).hexdigest(),
                'role': role, 'mime': mime} for (ref, data), role, mime in zip(contents.items(),
                ['raw_video', 'alpha_frames', 'production_record'],
                ['video/webm', 'application/zip', 'application/json'], strict=True)]
    executions = []
    def get(url, **kw):
        assert kw['trust_env'] is False
        return httpx.Response(200, json=caps) if url.endswith('/tools') else httpx.Response(
            200, content=contents[url.rsplit('/', 1)[-1]])
    def post(url, **kw):
        assert kw['trust_env'] is False
        if url.endswith('/assets'):
            assert kw['content'] == b'synthetic-image'
            assert kw['headers']['X-Asset-Filename'].endswith('.png')
            return httpx.Response(200, json={'asset_id': 'uploaded.png',
                'sha256': hashlib.sha256(kw['content']).hexdigest()})
        executions.append(kw['json'])
        return httpx.Response(200, json={'status': 'completed', 'value': {
            **outputs[0], 'assets': outputs, 'job_id': 'fixture-job'}})
    monkeypatch.setattr(httpx, 'get', get)
    monkeypatch.setattr(httpx, 'post', post)
    ctx = CallContext(actor=a, run_id='synthetic', step_id='one', call_id='one',
                      idempotency_key='stable', fence=1, deadline_at=utc_now()+timedelta(hours=2))
    hub = system.tool_hub
    args = {'input_asset_id': source, 'recipe': 'story-shot', 'prompt': 'nod'}
    result = await hub.call('node_windows_aigc_generate', args, ctx)
    assert len(result.value['assets']) == 3
    assert executions[0]['args']['input_asset_ref'] == 'uploaded.png'
    assert executions[0]['deadline_seconds'] == 5460
    assert result.value['alpha_frames_asset_id']
    again = await hub.call('node_windows_aigc_generate', args, ctx)
    assert [x['asset_id'] for x in result.value['assets']] == [x['asset_id'] for x in again.value['assets']]
    with pytest.raises(AgentOSError):
        await hub.call('node_windows_aigc_generate', {**args, 'input_asset_ref': 'forged'}, ctx)
    with pytest.raises(AgentOSError):
        await hub.call('node_windows_aigc_generate', args, ctx.model_copy(update={'actor': actor('another')}))


async def test_long_media_activity_uses_server_tool_deadline(system, monkeypatch) -> None:
    a = await system.deps.identity.authenticate({'token': owner_token(system)})
    resolve = system.deps.tools.resolve

    async def long_contract(actor, name):
        return (await resolve(actor, name)).model_copy(update={'timeout_ms': 5460000})

    monkeypatch.setattr(system.deps.tools, 'resolve', long_contract)
    graph = {'id': 'long-media', 'nodes': [{'id': 'media', 'kind': 'tool',
        'config': {'tool': 'math.add', 'arguments': {'a': 1, 'b': 2}, 'activity_timeout_s': 1}}]}
    published = system.workflows.publish(a, graph)
    started = system.workflows.start(a, published['workflow_id'], published['version'], {}, 'long')
    bindings = []

    class Engine:
        async def start(self, handle, binding):
            bindings.append(binding)

    system.workflow_driver.engine = Engine()
    run = system.store.get_run(a.tenant_id, started['internal_run_id'])
    await system.workflow_driver.dispatch(run)
    assert bindings[0]['schedule'][0]['activity_timeout_s'] == 5580


def test_composition_keeps_last_shot_when_subtitle_ends_early(tmp_path: Path) -> None:
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg or not shutil.which('ffprobe'):
        pytest.skip('FFmpeg and FFprobe required')
    clip, voice = tmp_path/'clip.mp4', tmp_path/'voice.wav'
    ffmpeg_run(ffmpeg, ['-f', 'lavfi', '-i', 'color=gray:s=256x256:r=16', '-frames:v', '5',
                       '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(clip)])
    ffmpeg_run(ffmpeg, ['-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.1', str(voice)])
    duration = compose(ffmpeg, [clip, clip], tmp_path, tmp_path/'film.mp4', width=256, height=256,
                       fps=16, audio=voice, subtitles=[{'start': 0, 'end': .1, 'text': 'Synthetic caption'}])
    assert duration == pytest.approx(.625, abs=.002)
    assert probe_duration(ffmpeg, tmp_path/'film.mp4') == pytest.approx(duration, abs=.002)
    assert (tmp_path/'subtitles.srt').is_file()


def test_media_transfer_error_does_not_expose_connection(system, monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise httpx.ConnectError('synthetic private connection details')
    monkeypatch.setattr(httpx, 'post', fail)
    with pytest.raises(AgentOSError) as error:
        system.assets.push_to_node(actor(), node_endpoint='http://synthetic.invalid',
            node_token='synthetic', data=b'synthetic', filename='input.png')
    assert error.value.message == 'node media transfer unavailable'
