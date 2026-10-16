"""Controlled identity LoRA wiring and recovery, with synthetic files and no GPU."""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from PIL import Image
from pydantic import ValidationError

from agentos.adapters.aigc.local import LocalProduction
from agentos.adapters.aigc.recipes import image_graph
from agentos.adapters.aigc.tools import AigcToolAdapter
from agentos.domains.aigc.director import Director
from agentos.domains.aigc.project import Project
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import CallContext, utc_now
from agentos.services.aigc import AigcService
from tests.integration.test_aigc import actor
from tests.integration.test_aigc_production import production


def registry(tmp_path: Path) -> tuple[dict, Path]:
    folder = tmp_path / 'ComfyUI' / 'models' / 'loras'
    folder.mkdir(parents=True)
    model = folder / 'synthetic_identity.safetensors'
    model.write_bytes(b'synthetic LoRA payload, never loaded by a model')
    return {'identity-fixture': {'path': str(model), 'sha256': hashlib.sha256(model.read_bytes()).hexdigest()}}, model


def image_args() -> dict:
    return {'recipe': 'klein-image', 'prompt': 'Synthetic single character.', 'steps': 4,
            'reference_asset_ids': ['image'], 'reference_asset_refs': ['source.png'],
            'width': 256, 'height': 256, 'image_lora_id': 'identity-fixture', 'image_lora_strength': .75}


def adapter_with_lora(tmp_path: Path) -> tuple[AigcToolAdapter, Path]:
    registered, model = registry(tmp_path)
    adapter = AigcToolAdapter(base_url='http://synthetic.invalid', sandbox_dir=tmp_path / 'runtime',
                              ffmpeg='unused', image_loras=registered)
    adapter.assets.mkdir()
    Image.new('RGB', (32, 32), 'green').save(adapter.assets / 'source.png')
    return adapter, model


def image_scheduler(adapter, monkeypatch, *, lose_reply=False):
    seen = {'uploads': [], 'submissions': [], 'requests': 0}
    ready = False

    def request(client, method, path, **kwargs):
        nonlocal ready
        seen['requests'] += 1
        if path.startswith('/.well-known'):
            return httpx.Response(200, json={'service': 'local-model-scheduler'})
        if path == '/object_info':
            plain = image_graph({'recipe': 'klein-image', 'prompt': 'Synthetic.'}, ['source.png'], 'synthetic')
            nodes = {node['class_type']: {} for node in plain.values()}
            nodes['LoraLoaderModelOnly'] = {}
            return httpx.Response(200, json=nodes)
        if path == '/upload/image':
            name, stream, mime = kwargs['files']['image']
            assert mime == 'image/png' and stream.read()
            seen['uploads'].append(name)
            return httpx.Response(200, json={})
        if path == '/prompt':
            seen['submissions'].append(kwargs['json'])
            ready = True
            if lose_reply and len(seen['submissions']) == 1:
                raise AgentOSError(ErrorCode.UNAVAILABLE, 'synthetic lost submission reply')
            return httpx.Response(200, json={'prompt_id': kwargs['json']['prompt_id']})
        if path.startswith('/history/'):
            pid = path.rsplit('/', 1)[-1]
            return httpx.Response(200, json={pid: {'status': {'completed': True},
                'outputs': {'16': {'images': [{'filename': 'result.png'}]}}}} if ready else {})
        raise AssertionError(path)

    monkeypatch.setattr(adapter, '_request', request)
    monkeypatch.setattr(adapter, '_download', lambda *args: b'synthetic image output')
    return seen


@pytest.mark.asyncio
async def test_single_subject_lora_flows_from_domain_and_web_through_offline_to_actual_graph(
        system, tmp_path, monkeypatch):
    a = actor()
    source = tmp_path / 'source.png'
    Image.new('RGB', (32, 32), 'green').save(source)
    image = system.assets.register_local(a, source.read_bytes(), mime='image/png')['asset_id']
    voice = system.assets.register_local(a, b'synthetic voice', mime='audio/wav')['asset_id']
    data = production(image, voice)
    data['production'].update(image_width=256, image_height=256)
    data['shots'][0]['cast'] = data['shots'][0]['cast'][:1]
    data['shots'][0].update(image_lora_id='identity-fixture', image_lora_strength=.75)
    service = AigcService(system.drafts, system.assets, system.workflows, system.deps.tools)
    project = service.project(a, data)
    web_args = service.graph(a, 'story', project, 'keyframes')['nodes'][0]['config']['arguments']
    assert web_args == Director.keyframe_args(project, project.shots[0])
    assert web_args['image_lora_id'] == 'identity-fixture' and web_args['image_lora_strength'] == .75
    assert web_args['steps'] == 4
    (tmp_path / 'voice.wav').write_bytes(b'synthetic voice')
    manifest = tmp_path / 'project.json'
    manifest.write_text(json.dumps({'project': data,
        'materials': {image: 'source.png', voice: 'voice.wav'}}))
    registered, model = registry(tmp_path)
    adapter = AigcToolAdapter(base_url='http://synthetic.invalid', sandbox_dir=tmp_path / 'runtime',
                              ffmpeg='unused', image_loras=registered)
    seen = image_scheduler(adapter, monkeypatch, lose_reply=True)
    runner = LocalProduction(manifest, adapter)
    with pytest.raises(AgentOSError, match='lost submission reply'):
        await runner.run('keyframes', 'one', 'identity')
    await runner.run('keyframes', 'one', 'identity')
    assert len(seen['submissions']) == 1
    graph = seen['submissions'][0]['prompt']
    assert graph['18']['class_type'] == 'LoraLoaderModelOnly'
    assert graph['18']['inputs'] == {'model': ['3', 0], 'lora_name': model.name, 'strength_model': .75}
    assert graph['13']['inputs']['model'] == ['18', 0]
    assert graph['12']['inputs']['steps'] == 4 and graph['13']['inputs']['cfg'] == 1
    assert str(model.parent) not in json.dumps(graph)
    assert runner.project.shots[0].keyframe_review == 'pending'
    assert runner.receipts['keyframes:one:identity']['arguments'] == web_args
    count = seen['requests']
    await runner.run('keyframes', 'one', 'identity')
    assert len(seen['submissions']) == 1 and seen['requests'] == count


@pytest.mark.parametrize('cast_count', [0, 2])
def test_identity_lora_rejects_missing_or_multiple_subjects(cast_count):
    data = production('image', 'voice')
    data['shots'][0].update(image_lora_id='identity-fixture')
    data['shots'][0]['cast'] = data['shots'][0]['cast'][:cast_count]
    with pytest.raises(ValidationError, match='single-subject'):
        Project.model_validate(data)


@pytest.mark.parametrize('id,strength', [('../outside', 1), ('', 1), ('x' * 41, 1),
                                       ('identity-fixture', -.1), ('identity-fixture', 1.6),
                                       ('identity-fixture', float('nan')), ('identity-fixture', True),
                                       (None, .75)])
def test_lora_domain_controls_require_safe_id_and_finite_bounded_strength(id, strength):
    data = production('image', 'voice')
    data['shots'][0]['cast'] = data['shots'][0]['cast'][:1]
    data['shots'][0].update(image_lora_id=id, image_lora_strength=strength)
    with pytest.raises(ValidationError):
        Project.model_validate(data)


def test_lora_requires_klein_and_unused_defaults_keep_existing_timeline_and_graph():
    project = Project.model_validate(production('image', 'voice'))
    plain = Director.keyframe_args(project, project.shots[0])
    assert 'image_lora_id' not in plain and 'image_lora_strength' not in plain
    graph = image_graph(plain, ['image.png'] * len(plain['reference_asset_ids']), 'synthetic')
    assert not any(node['class_type'] == 'LoraLoaderModelOnly' for node in graph.values())
    baseline = Director.timeline_args(project, still=True)['plan_digest']
    shot = project.shots[0]
    shot.cast = shot.cast[:1]
    shot.image_lora_id = 'identity-fixture'
    assert Director.timeline_args(project, still=True)['plan_digest'] != baseline
    project.production.image_recipe = 'qwen-edit'
    with pytest.raises(AgentOSError, match='single-subject Klein'):
        Director.keyframe_args(project, shot)


@pytest.mark.asyncio
@pytest.mark.parametrize('extra', [
    {'image_lora_id': 'unknown'}, {'image_lora_strength': 1},
    {'image_lora_id': 'identity-fixture', 'recipe': 'qwen-edit'},
    {'image_lora_id': 'identity-fixture', 'image_lora_strength': float('nan')},
    {'image_lora_id': 'identity-fixture', 'image_lora_strength': 1.6},
    {'image_lora_id': 'identity-fixture', 'image_lora_strength': True},
    {'image_lora_id': 'identity-fixture', 'lora_name': 'arbitrary.safetensors'},
    {'image_lora_id': 'identity-fixture', 'image_lora_path': 'synthetic-outside/model.safetensors'},
    {'image_lora_id': 'identity-fixture', 'image_lora_sha256': '0' * 64},
])
async def test_public_image_lora_contract_rejects_unregistered_or_arbitrary_model_controls(
        tmp_path, monkeypatch, extra):
    adapter, _ = adapter_with_lora(tmp_path)
    ctx = CallContext(actor=actor(), run_id='synthetic', step_id='image', call_id='image',
                      idempotency_key='public-lora', fence=1, deadline_at=utc_now() + timedelta(minutes=1))

    def must_not_execute(*args):
        raise AssertionError('Invalid public LoRA arguments reached execution')

    monkeypatch.setattr(adapter, '_execute', must_not_execute)
    with pytest.raises(AgentOSError) as rejected:
        await adapter.call('aigc.image', {'recipe': 'klein-image', 'prompt': 'Synthetic.', **extra}, ctx)
    assert rejected.value.code == ErrorCode.INVALID


@pytest.mark.parametrize('change', ['tamper', 'registry_hash', 'valid_new_version', 'missing_file'])
def test_lora_integrity_is_checked_before_completed_cache_reuse(tmp_path, monkeypatch, change):
    adapter, model = adapter_with_lora(tmp_path)
    seen = image_scheduler(adapter, monkeypatch)
    args = image_args()
    result = adapter._execute('aigc.image', args, 'stable-identity', 60)['value']
    assert result['visual_review'] == 'pending'
    assert adapter._execute('aigc.image', args, 'stable-identity', 60)['value']['reused']
    count = seen['requests']
    if change == 'tamper':
        model.write_bytes(b'tampered synthetic model')
        expected = ErrorCode.UNCERTAIN
    elif change == 'registry_hash':
        adapter.image_loras['identity-fixture']['sha256'] = '0' * 64
        expected = ErrorCode.UNCERTAIN
    elif change == 'missing_file':
        model.unlink()
        expected = ErrorCode.UNAVAILABLE
    else:
        model.write_bytes(b'new approved synthetic model version')
        adapter.image_loras['identity-fixture']['sha256'] = hashlib.sha256(model.read_bytes()).hexdigest()
        expected = ErrorCode.CONFLICT
    with pytest.raises(AgentOSError) as rejected:
        adapter._execute('aigc.image', args, 'stable-identity', 60)
    assert rejected.value.code == expected and seen['requests'] == count


def test_lora_graph_requires_internal_basename_and_keeps_strength_boundaries():
    for strength in (0, 1.5):
        graph = image_graph({**image_args(), 'image_lora_strength': strength}, ['source.png'], 'synthetic',
                            lora_name='synthetic_identity.safetensors')
        assert graph['18']['inputs']['strength_model'] == strength
        assert graph['12']['inputs']['steps'] == 4 and graph['13']['inputs']['cfg'] == 1
    for filename in ('../model.safetensors', 'C:\\model.safetensors'):
        with pytest.raises(AgentOSError, match='basename'):
            image_graph(image_args(), ['source.png'], 'synthetic', lora_name=filename)
    with pytest.raises(AgentOSError, match='must be resolved'):
        image_graph(image_args(), ['source.png'], 'synthetic')
    with pytest.raises(AgentOSError, match='strength requires a registered ID'):
        image_graph({'recipe': 'klein-image', 'prompt': 'Synthetic.', 'image_lora_strength': 1}, [], 'synthetic')
