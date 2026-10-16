"""Independent endpoint generation and provenance, with synthetic Comfy receipts only."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest
from PIL import Image
from pydantic import ValidationError

from agentos.adapters.aigc.local import LocalProduction
from agentos.adapters.aigc.media import register_output
from agentos.adapters.aigc.recipes import image_graph, recipe_graph
from agentos.adapters.aigc.tools import AigcToolAdapter
from agentos.domains.aigc.director import Director
from agentos.domains.aigc.project import Project
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.services.aigc import AigcService
from tests.integration.test_aigc import actor
from tests.integration.test_aigc_production import production


def png(color: str) -> bytes:
    buffer = io.BytesIO()
    Image.new('RGB', (32, 32), color).save(buffer, format='PNG')
    return buffer.getvalue()


def endpoint_data(head: str, tail: str, voice: str, recipe: str = 'qwen-edit-2511') -> dict:
    data = production(head, voice)
    data['production'].update(image_recipe=recipe, image_width=256, image_height=256)
    data['shots'][0].update(keyframe_review='pending', video_recipe='story-shot-guided',
        keyframe_reference_asset_ids=[head], keyframe_reference_notes='Figure 1 defines the starting pose and layout.',
        end_keyframe_reference_asset_ids=[tail],
        end_keyframe_reference_notes='Figure 1 defines the final pose, layout and one visible transformed body.',
        action_end='the same single body has transformed and raised its right hand',
        state_after={'door': 'closed', 'body': 'transformed'})
    return data


def endpoint_runner(tmp_path: Path, recipe: str = 'qwen-edit-2511') -> tuple[LocalProduction, AigcToolAdapter]:
    (tmp_path / 'head.png').write_bytes(png('blue'))
    (tmp_path / 'tail.png').write_bytes(png('red'))
    (tmp_path / 'voice.wav').write_bytes(b'synthetic voice material')
    manifest = tmp_path / 'project.json'
    manifest.write_text(json.dumps({'project': endpoint_data('head', 'tail', 'voice', recipe),
        'materials': {'head': 'head.png', 'tail': 'tail.png', 'voice': 'voice.wav'}}))
    adapter = AigcToolAdapter(base_url='http://synthetic.invalid', sandbox_dir=tmp_path / 'runtime', ffmpeg='unused')
    return LocalProduction(manifest, adapter), adapter


def scheduler(adapter, monkeypatch, recipe, *, lose_reply=False):
    seen = {'submissions': [], 'uploads': {}, 'requests': 0}
    completed = {}
    lost = set()
    image_nodes = image_graph({'recipe': recipe, 'prompt': 'Synthetic.'}, ['source.png'], 'synthetic')
    video_nodes = recipe_graph({'recipe': 'story-shot-guided', 'prompt': 'Synthetic.'}, 'first.png',
                               'synthetic', end_image_name='last.png')
    classes = {node['class_type']: {} for node in [*image_nodes.values(), *video_nodes.values()]}

    def request(client, method, path, **kwargs):
        seen['requests'] += 1
        if path.startswith('/.well-known'):
            return httpx.Response(200, json={'service': 'local-model-scheduler'})
        if path == '/object_info':
            return httpx.Response(200, json=classes)
        if path == '/upload/image':
            name, stream, mime = kwargs['files']['image']
            assert mime == 'image/png'
            image = Image.open(io.BytesIO(stream.read())).convert('RGB')
            seen['uploads'][name] = image.getpixel((image.width // 2, image.height // 2))
            return httpx.Response(200, json={})
        if path == '/prompt':
            submitted = kwargs['json']
            seen['submissions'].append(submitted)
            graph = submitted['prompt']
            video = any(n['class_type'].startswith('Wan') for n in graph.values())
            prompt = graph['7']['inputs'].get('prompt', graph['7']['inputs'].get('text', '')) if not video else ''
            phase = 'video' if video else 'end' if 'independent END state' in prompt else 'head'
            completed[submitted['prompt_id']] = phase
            if lose_reply and phase in {'head', 'end'} and phase not in lost:
                lost.add(phase)
                raise AgentOSError(ErrorCode.UNAVAILABLE, 'synthetic lost endpoint reply')
            return httpx.Response(200, json={'prompt_id': submitted['prompt_id']})
        if path.startswith('/history/'):
            pid = path.rsplit('/', 1)[-1]
            if pid not in completed:
                return httpx.Response(200, json={})
            video = completed[pid] == 'video'
            filename = pid + ('.mp4' if video else '.png')
            return httpx.Response(200, json={pid: {'status': {'completed': True},
                'outputs': {'16': {'videos' if video else 'images': [{'filename': filename}]}}}})
        raise AssertionError(path)

    def download(client, item):
        phase = completed[item['filename'].split('.')[0]]
        return b'synthetic video candidate' if phase == 'video' else png('orange' if phase == 'end' else 'cyan')

    monkeypatch.setattr(adapter, '_request', request)
    monkeypatch.setattr(adapter, '_download', download)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize('recipe,steps', [('qwen-edit', 40), ('qwen-edit-2511', 40), ('klein-image', 4)])
async def test_independent_endpoints_recover_same_uuid_keep_real_reviews_and_feed_guided_graph(
        system, tmp_path, monkeypatch, recipe, steps):
    runner, adapter = endpoint_runner(tmp_path, recipe)
    a = actor()
    head = system.assets.register_local(a, png('blue'), mime='image/png')['asset_id']
    tail = system.assets.register_local(a, png('red'), mime='image/png')['asset_id']
    voice = system.assets.register_local(a, b'synthetic voice', mime='audio/wav')['asset_id']
    service = AigcService(system.drafts, system.assets, system.workflows, system.deps.tools)
    web = service.project(a, endpoint_data(head, tail, voice, recipe))
    end_args = service.graph(a, 'story', web, 'endframes')['nodes'][0]['config']['arguments']
    local_args = Director.keyframe_args(runner.project, runner.project.shots[0], end=True)
    assert {k: v for k, v in local_args.items() if k != 'reference_asset_ids'} == {
        k: v for k, v in end_args.items() if k != 'reference_asset_ids'}
    assert end_args['reference_asset_ids'] == [tail]
    seen = scheduler(adapter, monkeypatch, recipe, lose_reply=True)
    for phase, review_kind in (('keyframes', 'frame'), ('endframes', 'end_frame')):
        with pytest.raises(AgentOSError, match='lost endpoint reply'):
            await runner.run(phase, 'one', 'endpoint')
        assert runner.receipts[phase + ':one:endpoint']['state'] == 'prepared'
        await runner.run(phase, 'one', 'endpoint')
        shot = runner.project.shots[0]
        assert (shot.end_keyframe_review if phase == 'endframes' else shot.keyframe_review) == 'pending'
        runner.review(review_kind, 'one', 'Synthetic pose, body count and final identity inspected.')
        runner = LocalProduction(runner.manifest, adapter)
        before = seen['requests']
        await runner.run(phase, 'one', 'endpoint')
        shot = runner.project.shots[0]
        assert seen['requests'] == before
        assert (shot.end_keyframe_review if phase == 'endframes' else shot.keyframe_review) == 'accepted'
    assert len(seen['submissions']) == 2
    assert seen['submissions'][0]['prompt_id'] != seen['submissions'][1]['prompt_id']
    for index, expected in enumerate(((0, 0, 255), (255, 0, 0))):
        graph = seen['submissions'][index]['prompt']
        sampler = '12' if recipe == 'klein-image' else '11'
        assert graph[sampler]['inputs']['steps'] == steps
        refs = [n['inputs']['image'] for n in graph.values() if n['class_type'] == 'LoadImage']
        assert len(refs) == 1 and seen['uploads'][refs[0]] == expected
    end_prompt = runner.receipts['endframes:one:endpoint']['arguments']['prompt']
    assert 'Initial action:' not in end_prompt and 'Initial object states:' not in end_prompt
    assert '; position:' not in end_prompt and '; looks toward:' not in end_prompt
    assert 'the same single body has transformed' in end_prompt and '"body": "transformed"' in end_prompt
    assert 'do not imply simultaneous visible people' in end_prompt
    assert runner.project.shots[0].input_asset_id != runner.project.shots[0].end_keyframe_asset_id
    original_call = adapter.call

    async def with_animatic(name, args, ctx):
        if name != 'aigc.animatic':
            return await original_call(name, args, ctx)
        output = tmp_path / 'synthetic-animatic.mp4'
        output.write_bytes(b'synthetic animatic, no quality claim')
        asset = register_output(output, adapter.assets, 'animatic')
        return {'value': {**asset, 'assets': [asset], 'job_id': 'synthetic-animatic'}}

    monkeypatch.setattr(adapter, 'call', with_animatic)
    await runner.run('animatic', None, 'endpoint')
    runner.review('animatic', None, 'Synthetic pacing and continuity inspected for this candidate.')
    await runner.run('generate', 'one', 'endpoint')
    native = seen['submissions'][-1]['prompt']
    loaded = [n['inputs']['image'] for n in native.values() if n['class_type'] == 'LoadImage']
    assert len(loaded) == 2
    assert [seen['uploads'][name] for name in loaded] == [(0, 255, 255), (255, 165, 0)]
    receipt = runner.receipts['generate:one:endpoint']
    assert receipt['arguments']['end_asset_id'] == runner.project.shots[0].end_keyframe_asset_id
    assert receipt['arguments']['input_asset_id'] == runner.project.shots[0].input_asset_id
    assert runner.project.shots[0].review == 'pending'
    for phase, field in (('keyframes', 'keyframe_review'), ('endframes', 'end_keyframe_review')):
        await runner.run(phase, 'one', 'new-explicit-attempt')
        assert getattr(runner.project.shots[0], field) == 'pending'


@pytest.mark.parametrize('refs', [['a'] * 2, ['a', 'b', 'c', 'd'], ['../outside'], ['.'], ['']])
def test_end_reference_ids_have_symmetric_first_frame_constraints(refs):
    data = endpoint_data('head', 'tail', 'voice')
    data['shots'][0]['end_keyframe_reference_asset_ids'] = refs
    with pytest.raises(ValidationError):
        Project.model_validate(data)


@pytest.mark.parametrize('change', ['missing_sources', 'missing_roles', 'missing_end_state',
                                   'reuse_source', 'reuse_head'])
def test_end_stage_requires_own_sources_roles_and_final_state(change):
    project = Project.model_validate(endpoint_data('head', 'tail', 'voice'))
    shot = project.shots[0]
    if change == 'missing_sources':
        shot.end_keyframe_reference_asset_ids = []
    elif change == 'missing_roles':
        shot.end_keyframe_reference_notes = ''
    elif change == 'missing_end_state':
        shot.action_end = ''
    elif change == 'reuse_source':
        shot.end_keyframe_reference_asset_ids = ['head']
    else:
        shot.input_asset_id = 'generated-head'
        shot.end_keyframe_reference_asset_ids = ['generated-head']
    with pytest.raises(AgentOSError):
        Director.keyframe_args(project, shot, end=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('phase,review_kind,source_id', [('keyframes', 'frame', 'head'),
                                                       ('endframes', 'end_frame', 'tail')])
@pytest.mark.parametrize('change', ['bytes', 'notes', 'final_state'])
async def test_changed_endpoint_sources_cannot_be_reviewed_or_recovered_under_same_attempt(
        tmp_path, monkeypatch, change, phase, review_kind, source_id):
    runner, adapter = endpoint_runner(tmp_path)
    seen = scheduler(adapter, monkeypatch, 'qwen-edit-2511')
    await runner.run(phase, 'one', 'controlled')
    shot = runner.project.shots[0]
    if change == 'bytes':
        (adapter.assets / runner.reference(source_id)).write_bytes(png('yellow'))
    elif change == 'notes':
        field = 'end_keyframe_reference_notes' if phase == 'endframes' else 'keyframe_reference_notes'
        setattr(shot, field, getattr(shot, field) + ' Changed camera and silhouette.')
    else:
        state = shot.state_after if phase == 'endframes' else shot.state_before
        state['body'] = 'different state'
    count = seen['requests']
    with pytest.raises(AgentOSError) as changed:
        runner.review(review_kind, 'one', 'Inspect synthetic pose and identity for acceptance.')
    review = shot.end_keyframe_review if phase == 'endframes' else shot.keyframe_review
    assert changed.value.code == ErrorCode.CONFLICT and review == 'pending'
    with pytest.raises(AgentOSError) as changed:
        await runner.run(phase, 'one', 'controlled')
    assert changed.value.code == ErrorCode.CONFLICT and seen['requests'] == count


@pytest.mark.asyncio
async def test_legacy_first_frame_receipt_recovers_accepted_without_new_fields(tmp_path, monkeypatch):
    runner, adapter = endpoint_runner(tmp_path, 'klein-image')
    shot = runner.project.shots[0]
    shot.end_keyframe_reference_asset_ids, shot.end_keyframe_reference_notes = [], ''
    shot.video_recipe = None
    seen = scheduler(adapter, monkeypatch, 'klein-image')
    old_args = Director.keyframe_args(runner.project, shot)
    # Captured from the pre-endframes Director using this synthetic project.
    assert hashlib.sha256(json.dumps(old_args, sort_keys=True, ensure_ascii=False).encode()).hexdigest() == (
        '51072d0c5f5d9d1a332885f17d72b2a4b8df208817865d4adcc301a517a4ed44')
    await runner.run('keyframes', 'one', 'legacy')
    runner.review('frame', 'one', 'Synthetic old frame inspected and accepted by an operator.')
    old_receipt = json.loads(json.dumps(runner.receipts['keyframes:one:legacy']))
    document = json.loads(runner.manifest.read_text())
    for row in document['project']['shots']:
        row.pop('end_keyframe_reference_asset_ids')
        row.pop('end_keyframe_reference_notes')
    runner.manifest.write_text(json.dumps(document))
    recovered = LocalProduction(runner.manifest, adapter)
    before = seen['requests']
    await recovered.run('keyframes', 'one', 'legacy')
    assert seen['requests'] == before and len(seen['submissions']) == 1
    receipt = recovered.receipts['keyframes:one:legacy']
    assert receipt['signature'] == old_receipt['signature'] and receipt['arguments'] == old_args
    assert receipt['result']['prompt_id'] == old_receipt['result']['prompt_id']
    assert recovered.project.shots[0].keyframe_review == 'accepted'
    assert receipt['result']['sha256'] == old_receipt['result']['sha256']


def completed_frame(system, service, project, stage):
    a = actor()
    graph = service.graph(a, 'story', project, stage)
    published = system.workflows.publish(a, graph)
    run = system.workflows.start(a, published['workflow_id'], published['version'], {}, 'synthetic-' + stage)
    run_id = run['internal_run_id']
    image = system.assets.register_local(a, png('cyan' if stage == 'keyframes' else 'orange'),
                                        mime='image/png', run_id=run_id)['asset_id']
    node = graph['nodes'][0]['id']
    system.store.save_checkpoint(a.tenant_id, run_id, json.dumps({'workflow_nodes': {
        node: {'state': 'completed', 'output': {'asset_id': image}}}}))
    return image, run_id


def central_project(system):
    a = actor()
    head = system.assets.register_local(a, png('blue'), mime='image/png')['asset_id']
    tail = system.assets.register_local(a, png('red'), mime='image/png')['asset_id']
    voice = system.assets.register_local(a, b'synthetic voice', mime='audio/wav')['asset_id']
    service = AigcService(system.drafts, system.assets, system.workflows, system.deps.tools)
    return service, service.project(a, endpoint_data(head, tail, voice))


def test_central_completed_frame_provenance_and_current_conditions_gate_save_and_export(system):
    service, project = central_project(system)
    shot = project.shots[0]
    source_tail = shot.end_keyframe_reference_asset_ids[0]
    shot.input_asset_id, _ = completed_frame(system, service, project, 'keyframes')
    shot.keyframe_review = 'accepted'
    shot.end_keyframe_asset_id, _ = completed_frame(system, service, project, 'endframes')
    shot.end_keyframe_review = 'accepted'
    saved = service.save(actor(), 'story', project.model_dump(mode='json'), 0)
    exported = service.export(actor(), 'story')
    with zipfile.ZipFile(io.BytesIO(system.assets.read(actor(), exported['asset_id']))) as archive:
        document = json.loads(archive.read('project.json'))
        assert source_tail in document['materials']
        assert document['project']['shots'][0]['end_keyframe_reference_asset_ids'] == [source_tail]
        assert document['project']['shots'][0]['keyframe_review'] == 'pending'
        assert document['project']['shots'][0]['end_keyframe_review'] == 'pending'
    shot.end_keyframe_reference_notes += ' Changed final visibility.'
    with pytest.raises(AgentOSError) as stale:
        service.save(actor(), 'story', project.model_dump(mode='json'), saved['revision'])
    assert stale.value.code == ErrorCode.CONFLICT


def test_central_conditioning_bytes_are_verified_again_before_accepting_generated_endpoints(system):
    service, project = central_project(system)
    shot = project.shots[0]
    shot.input_asset_id, _ = completed_frame(system, service, project, 'keyframes')
    shot.keyframe_review = 'accepted'
    shot.end_keyframe_asset_id, _ = completed_frame(system, service, project, 'endframes')
    shot.end_keyframe_review = 'accepted'
    original = system.assets.get_internal(actor(), shot.end_keyframe_reference_asset_ids[0])
    Path(original['path']).write_bytes(png('yellow'))
    with pytest.raises(AgentOSError) as corrupted:
        service.save(actor(), 'story', project.model_dump(mode='json'), 0)
    assert corrupted.value.code == ErrorCode.UNCERTAIN


def test_end_reference_roles_have_the_same_bound_and_candidates_require_actual_review(tmp_path):
    data = endpoint_data('head', 'tail', 'voice')
    data['shots'][0]['end_keyframe_reference_notes'] = 'x' * 601
    with pytest.raises(ValidationError):
        Project.model_validate(data)
    runner, _ = endpoint_runner(tmp_path)
    shot = runner.project.shots[0]
    shot.end_keyframe_asset_id = 'tail'
    with pytest.raises(AgentOSError, match='completed local production'):
        runner.review('end_frame', 'one', 'The original target reference is only conditioning material.')
    assert shot.end_keyframe_review == 'pending'


@pytest.mark.parametrize('failure', ['conditioning_as_output', 'unfinished', 'wrong_output'])
def test_central_tail_cannot_accept_conditioning_or_unfinished_or_wrong_completed_output(system, failure):
    service, project = central_project(system)
    shot = project.shots[0]
    shot.input_asset_id, _ = completed_frame(system, service, project, 'keyframes')
    shot.keyframe_review = 'accepted'
    tail, run_id = completed_frame(system, service, project, 'endframes')
    shot.end_keyframe_asset_id, shot.end_keyframe_review = tail, 'accepted'
    if failure == 'conditioning_as_output':
        shot.end_keyframe_asset_id = shot.end_keyframe_reference_asset_ids[0]
    else:
        system.store.save_checkpoint(actor().tenant_id, run_id, json.dumps({'workflow_nodes': {'endframe_one': {
            'state': 'prepared' if failure == 'unfinished' else 'completed',
            'output': {'asset_id': 'not-this-candidate' if failure == 'wrong_output' else tail}}}}))
    with pytest.raises(AgentOSError) as rejected:
        service.save(actor(), 'story', project.model_dump(mode='json'), 0)
    assert rejected.value.code == (ErrorCode.DENIED if failure == 'conditioning_as_output' else ErrorCode.INVALID)


def test_tail_reference_owner_and_mime_checks_and_unused_default_digest(system):
    service, project = central_project(system)
    data = project.model_dump(mode='json')
    foreign = system.assets.register_local(actor('other'), png('black'), mime='image/png')['asset_id']
    voice = project.production.dialogue[0].audio_asset_id
    for reference, code in ((foreign, ErrorCode.DENIED), (voice, ErrorCode.INVALID)):
        data['shots'][0]['end_keyframe_reference_asset_ids'] = [reference]
        with pytest.raises(AgentOSError) as rejected:
            service.project(actor(), data)
        assert rejected.value.code == code
    legacy = Project.model_validate(production('image', 'audio'))
    assert Director.timeline_args(legacy, still=True)['plan_digest'] == (
        'c4595d84466a934b2e311cdadbda6c7079fce4cb84f735cf9b54653651276202')
    project.shots[0].keyframe_review = 'accepted'
    initial = Director.timeline_args(project, still=True)['plan_digest']
    project.shots[0].end_keyframe_reference_notes += ' Changed source responsibility.'
    assert Director.timeline_args(project, still=True)['plan_digest'] != initial
