"""Candidate CNN reference preparation: synthetic PNG/API oracle, not visual certification."""
from __future__ import annotations

import hashlib
import io
import json
from datetime import timedelta

import httpx
import pytest
from PIL import Image, ImageChops, ImageDraw

from agentos.adapters.aigc import recipes
from agentos.adapters.aigc.local import LocalProduction
from agentos.adapters.aigc.tools import AigcToolAdapter
from agentos.domains.aigc.director import Director
from agentos.domains.aigc.project import Project
from agentos.domains.aigc.sampling import REFERENCE_CLEANUP_RECIPE, default_image_steps
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import CallContext, utc_now
from agentos.services.aigc import AigcService
from tests.integration.test_aigc import actor
from tests.integration.test_aigc_endframes import completed_frame, endpoint_data
from tests.integration.test_aigc_production import production

CLEANUP = REFERENCE_CLEANUP_RECIPE
PIN = '344c77bbcb158f17dd143070d1e789f38a66c04202311ae3a258ef66667a9ea9'
CONTEXT = [.225, .5, .775, 1]


def png(image):
    stream = io.BytesIO()
    image.save(stream, format='PNG')
    return stream.getvalue()


def source_png(color='navy'):
    image = Image.new('RGB', (1280, 720), color)
    draw = ImageDraw.Draw(image)
    for x in range(0, 1280, 31):
        draw.line((x, 0, 1279 - x, 719), fill=(x % 255, 100, 190), width=3)
    return png(image)


def mask_png(*, empty=False):
    mask = Image.new('L', (1280, 720))
    if not empty:
        draw = ImageDraw.Draw(mask)
        draw.rectangle((400, 630, 425, 670), fill=255)
        draw.rectangle((470, 640, 510, 660), fill=255)
    return png(mask)


def registered_adapter(tmp_path, monkeypatch, *, empty=False):
    model = tmp_path / 'models/inpaint/big-lama.pt'
    model.parent.mkdir(parents=True)
    model.write_bytes(b'synthetic CNN bytes; never loaded by a model')
    sha = hashlib.sha256(model.read_bytes()).hexdigest()
    monkeypatch.setitem(recipes.REFERENCE_CLEANUP_MODEL, 'sha256', sha)
    adapter = AigcToolAdapter(base_url='http://synthetic.invalid', sandbox_dir=tmp_path / 'runtime',
        ffmpeg='unused', recipe_models={CLEANUP: {'path': str(model), 'sha256': sha}})
    adapter.assets.mkdir()
    (adapter.assets / 'source.png').write_bytes(source_png())
    (adapter.assets / 'mask.png').write_bytes(mask_png(empty=empty))
    args = {'recipe': CLEANUP, 'prompt': 'Review context only: remove synthetic glyphs within the binary mask.',
        'reference_asset_ids': ['source', 'mask'], 'reference_asset_refs': ['source.png', 'mask.png'],
        'edit_region': CONTEXT}
    return adapter, args, model


def native_api_oracle(adapter, monkeypatch, args, *, lose_reply=False):
    """Independent PNG contract oracle; this does not execute native torch/CNN nodes."""
    seen = {'submissions': [], 'uploads': {}, 'requests': 0}
    completed = {}
    geometry = adapter._cleanup_geometry([adapter.assets / ref for ref in args['reference_asset_refs']], args)
    graph = recipes.image_graph(args, ['source.png', 'mask.png'], 'synthetic', cleanup=geometry)
    classes = {node['class_type']: {} for node in graph.values()}

    def request(client, method, path, **kwargs):
        seen['requests'] += 1
        if path.startswith('/.well-known'):
            return httpx.Response(200, json={'service': 'local-model-scheduler'})
        if path == '/object_info':
            return httpx.Response(200, json=classes)
        if path == '/upload/image':
            name, stream, mime = kwargs['files']['image']
            assert mime == 'image/png'
            seen['uploads'][name] = stream.read()
            return httpx.Response(200, json={})
        if path == '/prompt':
            submission = kwargs['json']
            seen['submissions'].append(submission)
            graph = submission['prompt']
            source = Image.open(io.BytesIO(seen['uploads'][graph['1']['inputs']['image']])).convert('RGB')
            mask = Image.open(io.BytesIO(seen['uploads'][graph['2']['inputs']['image']])).convert('L')
            if graph['16']['inputs']['images'] == ['8', 0]:
                crop = graph['5']['inputs']
                x, y, width, height = (crop[k] for k in ('x', 'y', 'width', 'height'))
                patch = Image.new('RGB', (width, height), 'magenta')
                source.paste(patch, (x, y), mask.crop((x, y, x + width, y + height)))
            completed[submission['prompt_id']] = png(source)
            if lose_reply and len(seen['submissions']) == 1:
                raise AgentOSError(ErrorCode.UNAVAILABLE, 'synthetic lost cleanup reply')
            return httpx.Response(200, json={'prompt_id': submission['prompt_id']})
        if path.startswith('/history/'):
            pid = path.rsplit('/', 1)[-1]
            return httpx.Response(200, json={pid: {'status': {'completed': True}, 'outputs': {
                '16': {'images': [{'filename': pid + '.png'}]}}}} if pid in completed else {})
        raise AssertionError(path)

    monkeypatch.setattr(adapter, '_request', request)
    monkeypatch.setattr(adapter, '_download', lambda client, item: completed[item['filename'].split('.')[0]])
    return seen


@pytest.mark.parametrize('empty', [False, True])
def test_native_candidate_graph_and_real_png_transfer_preserve_full_source_outside_glyphs(
        tmp_path, monkeypatch, empty):
    adapter, args, _ = registered_adapter(tmp_path, monkeypatch, empty=empty)
    seen = native_api_oracle(adapter, monkeypatch, args, lose_reply=True)
    originals = [(adapter.assets / ref).read_bytes() for ref in args['reference_asset_refs']]
    with pytest.raises(AgentOSError, match='lost cleanup reply'):
        adapter._execute('aigc.image', args, 'stable-cleanup', 60)
    result = adapter._execute('aigc.image', args, 'stable-cleanup', 60)['value']
    assert len(seen['submissions']) == 1 and len(seen['uploads']) == 2
    submission = seen['submissions'][0]
    assert result['prompt_id'] == submission['prompt_id']
    graph = submission['prompt']
    assert len([node for node in graph.values() if node['class_type'] == 'LoadImage']) == 2
    assert [seen['uploads'][graph[node]['inputs']['image']] for node in ('1', '2')] == originals
    assert not any('Sampler' in node['class_type'] or 'TextEncode' in node['class_type'] for node in graph.values())
    if empty:
        assert graph['16']['inputs']['images'] == ['1', 0]
        assert not any(node['class_type'].startswith('INPAINT_') for node in graph.values())
    else:
        assert graph['5']['inputs'] == {'image': ['1', 0], 'x': 288, 'y': 360, 'width': 704, 'height': 360}
        assert graph['6']['inputs']['mask'] == ['3', 0] and graph['3']['inputs']['channel'] == 'red'
        assert graph['7']['inputs'] == {'inpaint_model': ['4', 0], 'image': ['5', 0],
            'mask': ['6', 0], 'seed': 0, 'lama_size': 256}
        assert graph['8']['inputs']['destination'] == ['1', 0] and graph['8']['inputs']['mask'] == ['6', 0]
        assert graph['16']['inputs']['images'] == ['8', 0]
    before = Image.open(io.BytesIO(originals[0])).convert('RGB')
    mask = Image.open(io.BytesIO(originals[1])).convert('L')
    after = Image.open(adapter.assets / result['asset_ref']).convert('RGB')
    assert after.size == (1280, 720)
    difference = ImageChops.difference(before, after)
    protected = Image.composite(Image.new('RGB', before.size), difference, mask)
    assert protected.getbbox() is None
    if empty:
        assert difference.getbbox() is None
    else:
        assert after.getpixel((410, 650)) == (255, 0, 255)
        assert after.getpixel((450, 650)) == before.getpixel((450, 650))
    assert result['visual_review'] == 'pending'
    assert result['source_reference_only'] is True and result['text_conditioning'] is False
    record = json.loads((adapter.jobs / result['job_id'] / 'production.json').read_text())
    assert record['parameters']['operation'] == 'reference-preparation' and record['user_accepted'] is False
    assert record['parameters']['native_version'] == recipes.REFERENCE_CLEANUP_VERSION
    count = seen['requests']
    cached = adapter._execute('aigc.image', args, 'stable-cleanup', 60)['value']
    assert cached['reused'] and cached['sha256'] == result['sha256'] and seen['requests'] == count
    with pytest.raises(AgentOSError) as missing:
        adapter._execute('aigc.image', {**args, 'reference_asset_refs': ['source.png']}, 'stable-cleanup', 60)
    assert missing.value.code == ErrorCode.INVALID and seen['requests'] == count
    with pytest.raises(AgentOSError) as context_changed:
        adapter._execute('aigc.image', {**args, 'edit_region': [0, .5, 1, 1]}, 'stable-cleanup', 60)
    assert context_changed.value.code == ErrorCode.CONFLICT and seen['requests'] == count


def test_review_text_is_not_native_text_conditioning_and_new_recipe_remains_opt_in(tmp_path, monkeypatch):
    adapter, args, _ = registered_adapter(tmp_path, monkeypatch)
    geometry = adapter._cleanup_geometry([adapter.assets / ref for ref in args['reference_asset_refs']], args)
    first = recipes.image_graph(args, ['source.png', 'mask.png'], 'synthetic', cleanup=geometry)
    second = recipes.image_graph({**args, 'prompt': 'Completely different audit context.'},
                                 ['source.png', 'mask.png'], 'synthetic', cleanup=geometry)
    assert first == second
    assert recipes.IMAGE_RECIPES[CLEANUP]['kind'] == 'reference-preparation'
    assert Project.model_validate(production('image', 'audio')).production.image_recipe == 'klein-image'


@pytest.mark.parametrize('failure', ['missing', 'registry_hash', 'model_bytes', 'source_bytes', 'mask_bytes',
                                    'native_version', 'graph_version'])
def test_cleanup_resource_input_and_graph_integrity_are_checked_before_completed_cache(tmp_path, monkeypatch, failure):
    adapter, args, model = registered_adapter(tmp_path, monkeypatch)
    seen = native_api_oracle(adapter, monkeypatch, args)
    adapter._execute('aigc.image', args, 'immutable', 60)
    expected = ErrorCode.CONFLICT
    if failure == 'missing':
        model.unlink()
        expected = ErrorCode.UNAVAILABLE
    elif failure == 'registry_hash':
        adapter.recipe_models[CLEANUP]['sha256'] = '0' * 64
    elif failure == 'model_bytes':
        model.write_bytes(b'tampered CNN')
        expected = ErrorCode.UNCERTAIN
    elif failure == 'source_bytes':
        (adapter.assets / 'source.png').write_bytes(source_png('red'))
    elif failure == 'mask_bytes':
        (adapter.assets / 'mask.png').write_bytes(mask_png(empty=True))
    elif failure == 'native_version':
        monkeypatch.setattr('agentos.adapters.aigc.tools.REFERENCE_CLEANUP_VERSION', 'different-native-contract')
    else:
        original = recipes.reference_cleanup_graph
        def changed(*args):
            graph = original(*args)
            graph['7']['inputs']['lama_size'] = 512
            return graph
        monkeypatch.setattr(recipes, 'reference_cleanup_graph', changed)
    count = seen['requests']
    with pytest.raises(AgentOSError) as rejected:
        adapter._execute('aigc.image', args, 'immutable', 60)
    assert rejected.value.code == expected and seen['requests'] == count


@pytest.mark.parametrize('target', ['workflow', 'prepared_source', 'prepared_mask'])
def test_prepared_cleanup_tamper_is_rejected_before_native_recovery(tmp_path, monkeypatch, target):
    adapter, args, _ = registered_adapter(tmp_path, monkeypatch)
    seen = native_api_oracle(adapter, monkeypatch, args, lose_reply=True)
    with pytest.raises(AgentOSError, match='lost cleanup reply'):
        adapter._execute('aigc.image', args, 'prepared-tamper', 60)
    work = next(adapter.jobs.iterdir())
    if target == 'workflow':
        graph = json.loads((work / 'workflow.json').read_text())
        graph['7']['inputs']['lama_size'] = 512
        (work / 'workflow.json').write_text(json.dumps(graph))
    else:
        filename = 'prepared-0.png' if target == 'prepared_source' else 'prepared-1.png'
        (work / filename).write_bytes(mask_png(empty=True))
    requests = seen['requests']
    with pytest.raises(AgentOSError) as invalid:
        adapter._execute('aigc.image', args, 'prepared-tamper', 60)
    assert invalid.value.code == ErrorCode.UNCERTAIN and seen['requests'] == requests


@pytest.mark.parametrize('failure', ['dimensions', 'gray_mask', 'colored_mask', 'alpha_mask', 'alpha_source',
                                    'metadata_alpha', 'source_jpeg', 'outside_context', 'no_context'])
def test_cleanup_rejects_ambiguous_masks_or_uncovered_pixels_before_api(tmp_path, monkeypatch, failure):
    adapter, args, _ = registered_adapter(tmp_path, monkeypatch)
    if failure == 'dimensions':
        (adapter.assets / 'mask.png').write_bytes(png(Image.new('L', (64, 64))))
    elif failure == 'gray_mask':
        (adapter.assets / 'mask.png').write_bytes(png(Image.new('L', (1280, 720), 128)))
    elif failure == 'colored_mask':
        (adapter.assets / 'mask.png').write_bytes(png(Image.new('RGB', (1280, 720), (255, 0, 0))))
    elif failure in {'alpha_mask', 'alpha_source'}:
        field = 'mask.png' if failure == 'alpha_mask' else 'source.png'
        (adapter.assets / field).write_bytes(png(Image.new('RGBA', (1280, 720), (0, 0, 0, 0))))
    elif failure == 'metadata_alpha':
        Image.new('RGB', (1280, 720), 'white').save(adapter.assets / 'mask.png', transparency=(255, 255, 255))
    elif failure == 'source_jpeg':
        Image.new('RGB', (1280, 720)).save(adapter.assets / 'source.png', format='JPEG')
    elif failure == 'outside_context':
        args['edit_region'] = [0, 0, 1, .5]
    else:
        args.pop('edit_region')
    monkeypatch.setattr(adapter, '_request', lambda *a, **k: pytest.fail('Invalid cleanup reached the native API'))
    with pytest.raises(AgentOSError) as invalid:
        adapter._execute('aigc.image', args, 'bad-input', 60)
    assert invalid.value.code == ErrorCode.INVALID


@pytest.mark.asyncio
@pytest.mark.parametrize('extra', [{'steps': 4}, {'seed': 1}, {'negative': ''}, {'width': 1280}, {'height': 720},
    {'image_lora_id': 'anything'}, {'model_name': 'big-lama.pt'}, {'model_path': '../big-lama.pt'},
    {'recipe_models': {}}, {'nodes': {}}, {'reference_asset_ids': ['source']}, {'edit_region': None}])
async def test_public_cleanup_rejects_sampler_knobs_paths_graphs_and_ambiguous_references(tmp_path, monkeypatch, extra):
    adapter, args, _ = registered_adapter(tmp_path, monkeypatch)
    args = {k:v for k,v in args.items() if k != 'reference_asset_refs'}
    ctx = CallContext(actor=actor(), run_id='synthetic', step_id='cleanup', call_id='cleanup',
        idempotency_key='public-cleanup', fence=1, deadline_at=utc_now()+timedelta(minutes=1))
    monkeypatch.setattr(adapter, '_execute', lambda *a: pytest.fail('Invalid public cleanup reached execution'))
    with pytest.raises(AgentOSError) as invalid:
        await adapter.call('aigc.image', {**args, **extra}, ctx)
    assert invalid.value.code == ErrorCode.INVALID


def test_fixed_model_authorization_and_old_defaults(tmp_path, monkeypatch):
    assert recipes.REFERENCE_CLEANUP_MODEL['sha256'] == PIN
    assert 'eb11b7ddb5a8be81bfbaa9879659eb52ecf8776089bf20d9755eac41691b2077' in recipes.REFERENCE_CLEANUP_VERSION
    with pytest.raises(ValueError, match='no diffusion'):
        default_image_steps(CLEANUP)
    legacy = Project.model_validate(production('image', 'audio'))
    assert legacy.production.image_recipe == 'klein-image'
    assert Director.timeline_args(legacy, still=True)['plan_digest'] == (
        'c4595d84466a934b2e311cdadbda6c7079fce4cb84f735cf9b54653651276202')
    adapter, args, model = registered_adapter(tmp_path, monkeypatch)
    adapter.recipe_models = {}
    with pytest.raises(AgentOSError) as missing:
        adapter._execute('aigc.image', args, 'unregistered', 60)
    assert missing.value.code == ErrorCode.UNAVAILABLE
    wrong = model.parent.parent / 'big-lama.pt'
    wrong.write_bytes(model.read_bytes())
    adapter.recipe_models = {CLEANUP: {'path': str(wrong), 'sha256': hashlib.sha256(wrong.read_bytes()).hexdigest()}}
    with pytest.raises(AgentOSError) as outside:
        adapter._execute('aigc.image', args, 'outside-managed-root', 60)
    assert outside.value.code == ErrorCode.UNAVAILABLE


def cleanup_data(head, tail, mask, voice):
    data = endpoint_data(head, tail, voice, CLEANUP)
    data['shots'][0].update(keyframe_reference_asset_ids=[head, mask],
        end_keyframe_reference_asset_ids=[tail, mask], keyframe_edit_region=CONTEXT,
        end_keyframe_edit_region=CONTEXT,
        keyframe_reference_notes='Image 1 original start source; image 2 binary glyph mask only.',
        end_keyframe_reference_notes='Image 1 independent final source; image 2 binary glyph mask only.')
    return data


@pytest.mark.asyncio
async def test_cleanup_first_end_share_web_local_receipts_and_require_actual_review(system, tmp_path, monkeypatch):
    a = actor()
    refs = {key:system.assets.register_local(a, content, mime=mime)['asset_id'] for key, content, mime in (
        ('head', source_png(), 'image/png'), ('tail', source_png('teal'), 'image/png'),
        ('mask', mask_png(), 'image/png'), ('voice', b'synthetic voice', 'audio/wav'))}
    data = cleanup_data(refs['head'], refs['tail'], refs['mask'], refs['voice'])
    adapter, _, _ = registered_adapter(tmp_path, monkeypatch)
    for name, content in (('head.png', source_png()), ('tail.png', source_png('teal')),
                          ('glyph.png', mask_png()), ('voice.wav', b'synthetic voice')):
        (tmp_path / name).write_bytes(content)
    manifest = tmp_path / 'project.json'
    manifest.write_text(json.dumps({'project':data,'materials':{
        refs['head']:'head.png',refs['tail']:'tail.png',refs['mask']:'glyph.png',refs['voice']:'voice.wav'}}))
    runner = LocalProduction(manifest, adapter)
    service = AigcService(system.drafts, system.assets, system.workflows, system.deps.tools)
    project = service.project(a, data)
    forwarded = runner.arguments(Director.keyframe_args(runner.project, runner.project.shots[0]))
    seen = native_api_oracle(adapter, monkeypatch, forwarded)
    for stage, kind in (('keyframes', 'frame'), ('endframes', 'end_frame')):
        args = service.graph(a, 'story', project, stage)['nodes'][0]['config']['arguments']
        assert set(args) == {'recipe','reference_asset_ids','prompt','edit_region'}
        assert 'not model text conditioning' in args['prompt']
        await runner.run(stage, 'one', 'cleanup')
        assert runner.receipts[stage + ':one:cleanup']['arguments'] == args
        shot = runner.project.shots[0]
        assert (shot.keyframe_review if stage == 'keyframes' else shot.end_keyframe_review) == 'pending'
        runner.review(kind, 'one', 'Synthetic original exterior, mask and cleaned glyph area inspected.')
        count = seen['requests']
        await runner.run(stage, 'one', 'cleanup')
        assert seen['requests'] == count
        assert (shot.keyframe_review if stage == 'keyframes' else shot.end_keyframe_review) == 'accepted'
    assert len(seen['submissions']) == 2
    with pytest.raises(AgentOSError, match='not character or scene art'):
        Director.card_args(project, project.production.characters[0])
    generated, _ = completed_frame(system, service, project, 'keyframes')
    project.shots[0].input_asset_id, project.shots[0].keyframe_review = generated, 'accepted'
    service.save(a, 'story', project.model_dump(mode='json'), 0)
    project.shots[0].keyframe_edit_region = [0, .5, 1, 1]
    with pytest.raises(AgentOSError) as changed:
        service.save(a, 'story', project.model_dump(mode='json'), 1)
    assert changed.value.code == ErrorCode.CONFLICT


@pytest.mark.asyncio
async def test_independent_cleanup_end_requires_different_source_content_not_only_another_mask(
        system, tmp_path, monkeypatch):
    a = actor()
    head = system.assets.register_local(a, source_png(), mime='image/png')['asset_id']
    duplicate = system.assets.register_local(a, source_png(), mime='image/png')['asset_id']
    mask = system.assets.register_local(a, mask_png(), mime='image/png')['asset_id']
    other_mask = system.assets.register_local(a, mask_png(empty=True), mime='image/png')['asset_id']
    voice = system.assets.register_local(a, b'synthetic voice', mime='audio/wav')['asset_id']
    data = cleanup_data(head, duplicate, mask, voice)
    data['shots'][0]['end_keyframe_reference_asset_ids'][1] = other_mask
    project = Project.model_validate(data)
    project.shots[0].input_asset_id = 'generated-head'
    project.shots[0].end_keyframe_reference_asset_ids[0] = head
    with pytest.raises(AgentOSError, match='ending source'):
        Director.keyframe_args(project, project.shots[0], end=True)
    service = AigcService(system.drafts, system.assets, system.workflows, system.deps.tools)
    project = service.project(a, data)
    with pytest.raises(AgentOSError, match='source content'):
        service.graph(a, 'story', project, 'endframes')
    adapter, _, _ = registered_adapter(tmp_path, monkeypatch)
    manifest = tmp_path / 'project.json'
    for name, content in (('head.png', source_png()), ('copy.png', source_png()), ('mask.png', mask_png()),
                          ('empty.png', mask_png(empty=True)), ('voice.wav', b'synthetic voice')):
        (tmp_path / name).write_bytes(content)
    manifest.write_text(json.dumps({'project':data,'materials':{
        head:'head.png',duplicate:'copy.png',mask:'mask.png',other_mask:'empty.png',voice:'voice.wav'}}))
    runner = LocalProduction(manifest, adapter)
    monkeypatch.setattr(adapter, '_execute', lambda *a: pytest.fail('Duplicate end source reached execution'))
    with pytest.raises(AgentOSError, match='source content'):
        await runner.run('endframes', 'one', 'duplicate-end')
