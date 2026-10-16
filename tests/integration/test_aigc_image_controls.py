"""Regional native graphs and pinned accelerators; synthetic files and API receipts only."""
from __future__ import annotations

import hashlib
import io
import json
import shutil
from datetime import timedelta

import httpx
import pytest
from PIL import Image, ImageChops
from pydantic import ValidationError

from agentos.adapters.aigc import recipes
from agentos.adapters.aigc.local import LocalProduction
from agentos.adapters.aigc.tools import AigcToolAdapter
from agentos.domains.aigc.director import Director
from agentos.domains.aigc.project import Project
from agentos.domains.aigc.sampling import default_image_steps
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import CallContext, utc_now
from agentos.services.aigc import AigcService
from tests.integration.test_aigc import actor
from tests.integration.test_aigc_endframes import completed_frame, endpoint_data
from tests.integration.test_aigc_production import production

LIGHTNING = 'qwen-edit-2511-lightning'
OFFICIAL_HASH = '22226e8d05d354bb356627d428809f5afd7819399b077238a2b70a82883a904f'
REGION = [0, .82, 1, 1]


def png(image: Image.Image) -> bytes:
    stream = io.BytesIO()
    image.save(stream, format='PNG')
    return stream.getvalue()


def source_png() -> bytes:
    image = Image.new('RGB', (96, 64))
    image.putdata([(x * 2, y * 3, (x + y) % 255) for y in range(64) for x in range(96)])
    return png(image)


def synthetic_registry(tmp_path, monkeypatch):
    folder = tmp_path / 'ComfyUI/models/loras'
    folder.mkdir(parents=True)
    filename = recipes.RECIPE_LORAS[LIGHTNING]['filename']
    file = folder / filename
    file.write_bytes(b'synthetic accelerator bytes; never loaded by a model')
    sha = hashlib.sha256(file.read_bytes()).hexdigest()
    # The real pin is separately asserted. Synthetic model fixtures cannot have
    # the official file's cryptographic hash, so only this test fixture substitutes it.
    monkeypatch.setitem(recipes.RECIPE_LORAS, LIGHTNING, {'filename': filename, 'sha256': sha})
    return {LIGHTNING: {'path': str(file), 'sha256': sha}}, file


def adapter_fixture(tmp_path, monkeypatch, recipe, *, region=None):
    registered, file = synthetic_registry(tmp_path, monkeypatch) if recipe == LIGHTNING else ({}, None)
    adapter = AigcToolAdapter(base_url='http://synthetic.invalid', sandbox_dir=tmp_path / 'runtime',
                              ffmpeg='unused', recipe_loras=registered)
    adapter.assets.mkdir()
    (adapter.assets / 'source.png').write_bytes(source_png())
    args = {'recipe': recipe, 'prompt': 'Remove only synthetic text inside the specified region.',
            'reference_asset_ids': ['source'], 'reference_asset_refs': ['source.png'],
            'width': 256, 'height': 320, 'steps': default_image_steps(recipe), 'seed': 17}
    if region is not None:
        args['edit_region'] = region
    return adapter, args, file


def regional_png_oracle(graph, uploads):
    """Independent PNG oracle for the API fixture, not a production image backend.

    Native-node execution is checked separately against the existing Win probe;
    these tests verify graph bindings, real PNG delivery, provenance and caching.
    """
    dimensions = graph['70']['inputs'] if '70' in graph else graph['10']['inputs']
    donor = Image.new('RGB', (dimensions['width'], dimensions['height']), 'magenta')
    if graph['16']['inputs']['images'] != ['75', 0]:
        return png(donor), None
    scale = graph['70']['inputs']
    load = graph[scale['image'][0]]['inputs']['image']
    base = Image.open(io.BytesIO(uploads[load])).convert('RGB').resize(
        (scale['width'], scale['height']), Image.Resampling.LANCZOS)
    solid, feather, placement = (graph[key]['inputs'] for key in ('72', '73', '74'))
    mask = Image.new('L', base.size)
    rectangle = Image.new('L', (solid['width'], solid['height']))
    alpha = rectangle.load()
    for y in range(rectangle.height):
        for x in range(rectangle.width):
            weight = 1.0
            for distance, edge in ((x + 1, feather['left']), (y + 1, feather['top']),
                                   (rectangle.width - x, feather['right']),
                                   (rectangle.height - y, feather['bottom'])):
                if edge and distance <= edge:
                    weight *= distance / edge
            alpha[x, y] = int(weight * 255)
    mask.paste(rectangle, (placement['x'], placement['y']))
    return png(Image.composite(donor, base, mask)), base


def scheduler(adapter, monkeypatch, args, *, lose_reply=False, render=None):
    seen = {'submissions': [], 'uploads': {}, 'standard': {}, 'requests': 0}
    completed = {}
    graph = recipes.image_graph(args, ['source.png'], 'synthetic',
        lora_name=recipes.RECIPE_LORAS[LIGHTNING]['filename'] if args['recipe'] == LIGHTNING else None)
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
            output, standard = render(submission['prompt'], seen['uploads']) if render else (
                png(Image.new('RGB', (256, 320), 'cyan')), None)
            pid = submission['prompt_id']
            completed[pid] = output
            seen['standard'][pid] = standard
            if lose_reply and len(seen['submissions']) == 1:
                raise AgentOSError(ErrorCode.UNAVAILABLE, 'synthetic lost image reply')
            return httpx.Response(200, json={'prompt_id': pid})
        if path.startswith('/history/'):
            pid = path.rsplit('/', 1)[-1]
            return httpx.Response(200, json={pid: {'status': {'completed': True},
                'outputs': {'16': {'images': [{'filename': pid + '.png'}]}}}} if pid in completed else {})
        raise AssertionError(path)

    monkeypatch.setattr(adapter, '_request', request)
    monkeypatch.setattr(adapter, '_download', lambda client, item: completed[item['filename'].split('.')[0]])
    return seen


@pytest.mark.parametrize('recipe', ['qwen-edit', 'qwen-edit-2511', 'klein-image', LIGHTNING])
def test_region_graph_encodes_source_with_noise_mask_and_keeps_native_primary(recipe):
    args = {'recipe': recipe, 'prompt': 'Synthetic edit.', 'width': 1024, 'height': 576,
            'edit_region': [0, .85416667, 1, 1]}
    graph = recipes.image_graph(args, ['source.png'], 'synthetic',
        lora_name=recipes.RECIPE_LORAS[LIGHTNING]['filename'] if recipe == LIGHTNING else None)
    assert graph['16']['inputs']['images'] == ['75', 0]
    assert len([n for n in graph.values() if n['class_type'] == 'LoadImage']) == 1
    assert graph['70']['inputs']['upscale_method'] == 'lanczos' and graph['70']['inputs']['crop'] == 'disabled'
    assert graph['72']['inputs'] == {'value': 1.0, 'width': 1024, 'height': 84}
    assert graph['73']['inputs'] == {'mask': ['72', 0], 'left': 0, 'top': 12, 'right': 0, 'bottom': 0}
    assert graph['74']['inputs']['y'] == 492
    assert graph['76']['inputs']['pixels'] == ['70', 0]
    assert graph['77']['inputs'] == {'samples': ['76', 0], 'mask': ['74', 0]}
    sampler = '26' if recipe == 'klein-image' else '11'
    assert graph[sampler]['inputs']['latent_image'] == ['77', 0]
    assert graph['76']['inputs']['vae'] == (['5', 0] if recipe == 'klein-image' else ['6', 0])
    if recipe != 'klein-image':
        assert graph['11']['inputs']['denoise'] == 1.0
        assert graph['11']['inputs']['steps'] == default_image_steps(recipe)
    assert graph['75']['inputs']['mask'] == ['74', 0] and graph['75']['inputs']['destination'] == ['70', 0]


@pytest.mark.parametrize('region', [[0, 0, 1, 1], [.1, .2, .8, .9], [.5, .5, .51, .51]])
def test_mask_stays_inside_output_rectangle_and_feather_never_exceeds_twelve_pixels(region):
    graph = recipes.image_graph({'recipe': 'qwen-edit-2511', 'prompt': 'Synthetic.',
        'width': 256, 'height': 320, 'edit_region': region}, ['source.png'], 'synthetic')
    rect, place, feather = (graph[key]['inputs'] for key in ('72', '74', '73'))
    assert 0 <= place['x'] < place['x'] + rect['width'] <= 256
    assert 0 <= place['y'] < place['y'] + rect['height'] <= 320
    assert all(0 <= feather[key] <= 12 for key in ('left', 'top', 'right', 'bottom'))


@pytest.mark.parametrize('region', [[0, 0, 1], [0, float('nan'), 1, 1], [0, 0, float('inf'), 1],
    [False, 0, 1, 1], ['0', 0, 1, 1], [-.1, 0, 1, 1], [0, 0, 1.1, 1], [.5, 0, .5, 1], [0, .9, 1, .8]])
def test_region_domain_and_graph_reject_nonfinite_unordered_or_coerced_coordinates(region):
    for field in ('keyframe_edit_region', 'end_keyframe_edit_region'):
        data = production('source', 'voice')
        data['shots'][0][field] = region
        with pytest.raises(ValidationError):
            Project.model_validate(data)
    with pytest.raises(AgentOSError):
        recipes.image_graph({'recipe': 'qwen-edit-2511', 'prompt': 'Synthetic.', 'edit_region': region},
                            ['source.png'], 'synthetic')


@pytest.mark.parametrize('images', [[], ['one.png', 'two.png']])
def test_region_rejects_non_single_reference(images):
    with pytest.raises(AgentOSError):
        recipes.image_graph({'recipe': 'klein-image', 'prompt': 'Synthetic.', 'edit_region': REGION},
                            images, 'synthetic')


def test_region_rejects_positive_subpixel_rectangle_that_contains_no_pixel():
    with pytest.raises(AgentOSError, match='no output pixels'):
        recipes.image_graph({'recipe': 'qwen-edit-2511', 'prompt': 'Synthetic.', 'edit_region': [0, 0, .000001, 1]},
                            ['source.png'], 'synthetic')


@pytest.mark.parametrize('recipe', ['klein-image', 'qwen-edit-2511', LIGHTNING])
def test_real_png_region_primary_and_lost_reply_recovery_keep_source_and_uuid(tmp_path, monkeypatch, recipe):
    adapter, args, _ = adapter_fixture(tmp_path, monkeypatch, recipe, region=REGION)
    seen = scheduler(adapter, monkeypatch, args, lose_reply=True, render=regional_png_oracle)
    with pytest.raises(AgentOSError, match='lost image reply'):
        adapter._execute('aigc.image', args, 'stable-region', 60)
    result = adapter._execute('aigc.image', args, 'stable-region', 60)['value']
    assert len(seen['submissions']) == 1 and len(seen['uploads']) == 1
    assert result['prompt_id'] == seen['submissions'][0]['prompt_id']
    image = Image.open(adapter.assets / result['asset_ref']).convert('RGB')
    standard = seen['standard'][result['prompt_id']]
    top = round(REGION[1] * image.height)
    assert ImageChops.difference(image.crop((0, 0, image.width, top)),
        standard.crop((0, 0, standard.width, top))).getbbox() is None
    assert image.getpixel((image.width // 2, top + 12)) == (255, 0, 255)
    assert hashlib.sha256((adapter.assets / result['asset_ref']).read_bytes()).hexdigest() == result['sha256']
    count = seen['requests']
    restored = adapter._execute('aigc.image', args, 'stable-region', 60)['value']
    assert restored['reused'] and restored['sha256'] == result['sha256'] and seen['requests'] == count
    with pytest.raises(AgentOSError) as conflict:
        adapter._execute('aigc.image', {**args, 'edit_region': [0, .8, 1, 1]}, 'stable-region', 60)
    assert conflict.value.code == ErrorCode.CONFLICT and len(seen['submissions']) == 1
    with pytest.raises(AgentOSError, match='transfer is incomplete'):
        adapter._execute('aigc.image', {**args, 'reference_asset_refs': []}, 'stable-region', 60)


@pytest.mark.parametrize('change', ['file_tamper', 'registry_pin', 'missing', 'authorized_new_pin', 'base_graph'])
def test_lightning_registry_and_recipe_integrity_precede_completed_cache(tmp_path, monkeypatch, change):
    adapter, args, file = adapter_fixture(tmp_path, monkeypatch, LIGHTNING)
    seen = scheduler(adapter, monkeypatch, args)
    adapter._execute('aigc.image', args, 'pinned', 60)
    count = seen['requests']
    expected = ErrorCode.CONFLICT
    if change == 'file_tamper':
        file.write_bytes(b'tampered model')
        expected = ErrorCode.UNCERTAIN
    elif change == 'registry_pin':
        adapter.recipe_loras[LIGHTNING]['sha256'] = '0' * 64
    elif change == 'missing':
        file.unlink()
        expected = ErrorCode.UNAVAILABLE
    elif change == 'authorized_new_pin':
        file.write_bytes(b'synthetic newly authorized preset version')
        sha = hashlib.sha256(file.read_bytes()).hexdigest()
        monkeypatch.setitem(recipes.RECIPE_LORAS, LIGHTNING, {**recipes.RECIPE_LORAS[LIGHTNING], 'sha256': sha})
        adapter.recipe_loras[LIGHTNING]['sha256'] = sha
    else:
        changed = tmp_path / 'versioned-recipes'
        shutil.copytree(recipes.RECIPE_DIR, changed)
        data = json.loads((changed / 'qwen-edit-2511.json').read_text())
        data['9']['inputs']['shift'] = 3.1
        (changed / 'qwen-edit-2511.json').write_text(json.dumps(data))
        monkeypatch.setattr(recipes, 'RECIPE_DIR', changed)
    with pytest.raises(AgentOSError) as rejected:
        adapter._execute('aigc.image', args, 'pinned', 60)
    assert rejected.value.code == expected and seen['requests'] == count


def test_region_derived_graph_change_also_invalidates_completed_cache(tmp_path, monkeypatch):
    adapter, args, _ = adapter_fixture(tmp_path, monkeypatch, 'qwen-edit-2511', region=REGION)
    seen = scheduler(adapter, monkeypatch, args)
    adapter._execute('aigc.image', args, 'graph-version', 60)
    original = recipes.regional_image_graph

    def changed(*args):
        graph = original(*args)
        graph['73']['inputs']['top'] = 8
        return graph

    monkeypatch.setattr(recipes, 'regional_image_graph', changed)
    count = seen['requests']
    with pytest.raises(AgentOSError) as conflict:
        adapter._execute('aigc.image', args, 'graph-version', 60)
    assert conflict.value.code == ErrorCode.CONFLICT and seen['requests'] == count


@pytest.mark.parametrize('recipe', ['qwen-edit-2511', LIGHTNING])
def test_prepared_controlled_graph_tamper_is_rejected_before_uuid_recovery(tmp_path, monkeypatch, recipe):
    adapter, args, _ = adapter_fixture(tmp_path, monkeypatch, recipe, region=REGION)
    seen = scheduler(adapter, monkeypatch, args, lose_reply=True)
    with pytest.raises(AgentOSError, match='lost image reply'):
        adapter._execute('aigc.image', args, 'graph-tamper', 60)
    graph_file = next(adapter.jobs.glob('*/workflow.json'))
    graph = json.loads(graph_file.read_text())
    graph['77']['inputs']['samples'] = ['10', 0]
    graph_file.write_text(json.dumps(graph))
    requests = seen['requests']
    with pytest.raises(AgentOSError) as rejected:
        adapter._execute('aigc.image', args, 'graph-tamper', 60)
    assert rejected.value.code == ErrorCode.UNCERTAIN and seen['requests'] == requests
    assert len(seen['submissions']) == 1


def test_official_lightning_pin_and_native_loader_do_not_change_q5_encoder_or_vae():
    assert recipes.RECIPE_LORAS[LIGHTNING]['sha256'] == OFFICIAL_HASH
    filename = recipes.RECIPE_LORAS[LIGHTNING]['filename']
    graph = recipes.image_graph({'recipe': LIGHTNING, 'prompt': 'Synthetic.'}, ['source.png'], 'synthetic',
                               lora_name=filename)
    base = recipes.image_graph({'recipe': 'qwen-edit-2511', 'prompt': 'Synthetic.'}, ['source.png'], 'synthetic')
    assert graph['4'] == base['4'] and graph['5'] == base['5'] and graph['6'] == base['6']
    assert graph['18'] == {'class_type': 'LoraLoaderModelOnly', 'inputs': {
        'model': ['4', 0], 'lora_name': filename, 'strength_model': 1.0}}
    assert graph['9']['inputs']['model'] == ['18', 0] and graph['11']['inputs']['model'] == ['9', 0]
    assert graph['11']['inputs']['steps'] == 4 and graph['11']['inputs']['cfg'] == 1
    assert graph['11']['inputs']['denoise'] == base['11']['inputs']['denoise']
    assert not any(n['class_type'] == 'LoadImage' and n['inputs']['image'] != 'source.png' for n in graph.values())
    assert Project.model_validate(production('source', 'voice')).production.image_recipe == 'klein-image'


@pytest.mark.parametrize('args,filename', [({'steps': 40}, 'official'), ({}, 'arbitrary.safetensors'),
    ({'image_lora_id': 'identity'}, 'official'), ({'image_lora_strength': 1}, 'official')])
def test_lightning_rejects_arbitrary_steps_models_and_identity_controls(args, filename):
    name = recipes.RECIPE_LORAS[LIGHTNING]['filename'] if filename == 'official' else filename
    with pytest.raises(AgentOSError):
        recipes.image_graph({'recipe': LIGHTNING, 'prompt': 'Synthetic.', **args}, ['source.png'], 'synthetic',
                            lora_name=name)


@pytest.mark.asyncio
@pytest.mark.parametrize('extra', [
    {'edit_region': [0, float('nan'), 1, 1]}, {'edit_region': [0, .9, 1, .8]},
    {'edit_region': REGION, 'reference_asset_ids': ['source', 'other']},
    {'edit_region': {'path': '../mask.png'}}, {'edit_region': REGION, 'nodes': {'evil': {}}},
    {'recipe': LIGHTNING, 'steps': 40}, {'recipe': LIGHTNING, 'recipe_lora_path': '../model.safetensors'},
    {'recipe': LIGHTNING, 'recipe_lora_sha256': OFFICIAL_HASH},
    {'recipe': LIGHTNING, 'lora_name': 'arbitrary.safetensors'},
    {'recipe': LIGHTNING, 'recipe_lora_strength': .5}, {'recipe': LIGHTNING, 'image_lora_id': LIGHTNING},
])
async def test_public_image_controls_reject_nonfinite_paths_nodes_or_unregistered_model_knobs(
        tmp_path, monkeypatch, extra):
    adapter = AigcToolAdapter(base_url='http://synthetic.invalid', sandbox_dir=tmp_path, ffmpeg='unused')
    ctx = CallContext(actor=actor(), run_id='synthetic', step_id='image', call_id='image',
        idempotency_key='invalid-public', fence=1, deadline_at=utc_now() + timedelta(minutes=1))

    def never(*args):
        raise AssertionError('Invalid image controls reached execution')

    monkeypatch.setattr(adapter, '_execute', never)
    with pytest.raises(AgentOSError) as invalid:
        await adapter.call('aigc.image', {'recipe': 'qwen-edit-2511', 'prompt': 'Synthetic.',
            'reference_asset_ids': ['source'], **extra}, ctx)
    assert invalid.value.code == ErrorCode.INVALID


def test_accelerator_requires_registry_and_stays_out_of_identity_enum(tmp_path, monkeypatch):
    adapter, args, _ = adapter_fixture(tmp_path, monkeypatch, LIGHTNING)
    schema = next(spec.input_schema for spec in adapter.list_tools(actor()) if spec.name == 'aigc.image')
    assert schema['properties']['image_lora_id']['enum'] == []
    adapter.recipe_loras = {}
    with pytest.raises(AgentOSError) as unavailable:
        adapter._execute('aigc.image', args, 'missing-preset', 60)
    assert unavailable.value.code == ErrorCode.UNAVAILABLE


@pytest.mark.asyncio
@pytest.mark.parametrize('recipe', ['qwen-edit-2511', LIGHTNING])
async def test_first_and_end_regions_share_web_local_arguments_and_changed_review_sources_fail(
        system, tmp_path, monkeypatch, recipe):
    a = actor()
    head = system.assets.register_local(a, source_png(), mime='image/png')['asset_id']
    tail = system.assets.register_local(a, png(Image.new('RGB', (96, 64), 'red')), mime='image/png')['asset_id']
    voice = system.assets.register_local(a, b'synthetic voice', mime='audio/wav')['asset_id']
    data = endpoint_data(head, tail, voice, recipe)
    data['shots'][0].update(keyframe_edit_region=REGION, end_keyframe_edit_region=REGION)
    service = AigcService(system.drafts, system.assets, system.workflows, system.deps.tools)
    project = service.project(a, data)
    (tmp_path / 'head.png').write_bytes(source_png())
    (tmp_path / 'tail.png').write_bytes(png(Image.new('RGB', (96, 64), 'red')))
    (tmp_path / 'voice.wav').write_bytes(b'synthetic voice')
    manifest = tmp_path / 'project.json'
    manifest.write_text(json.dumps({'project': data,
        'materials': {head: 'head.png', tail: 'tail.png', voice: 'voice.wav'}}))
    registered, _ = synthetic_registry(tmp_path, monkeypatch) if recipe == LIGHTNING else ({}, None)
    adapter = AigcToolAdapter(base_url='http://synthetic.invalid', sandbox_dir=tmp_path / 'runtime',
                              ffmpeg='unused', recipe_loras=registered)
    runner = LocalProduction(manifest, adapter)
    args = Director.keyframe_args(project, project.shots[0])
    seen = scheduler(adapter, monkeypatch, args, render=regional_png_oracle)
    for stage in ('keyframes', 'endframes'):
        web_args = service.graph(a, 'story', project, stage)['nodes'][0]['config']['arguments']
        assert web_args['edit_region'] == REGION
        await runner.run(stage, 'one', 'controlled-region')
        assert runner.receipts[stage + ':one:controlled-region']['arguments'] == web_args
    shot = runner.project.shots[0]
    runner.review('frame', 'one', 'Synthetic unchanged exterior and intended edit region inspected.')
    shot.keyframe_edit_region = [0, .8, 1, 1]
    with pytest.raises(AgentOSError) as changed:
        runner.review('frame', 'one', 'Synthetic region changed after generation and requires a new attempt.')
    assert changed.value.code == ErrorCode.CONFLICT
    assert len(seen['submissions']) == 2
    central_head, _ = completed_frame(system, service, project, 'keyframes')
    project.shots[0].input_asset_id, project.shots[0].keyframe_review = central_head, 'accepted'
    service.save(a, 'story', project.model_dump(mode='json'), 0)
    project.shots[0].keyframe_edit_region = [0, .8, 1, 1]
    with pytest.raises(AgentOSError) as changed:
        service.save(a, 'story', project.model_dump(mode='json'), 1)
    assert changed.value.code == ErrorCode.CONFLICT


@pytest.mark.asyncio
async def test_disabling_region_does_not_bypass_completed_first_frame_provenance(system, tmp_path, monkeypatch):
    a = actor()
    head = system.assets.register_local(a, source_png(), mime='image/png')['asset_id']
    voice = system.assets.register_local(a, b'synthetic voice', mime='audio/wav')['asset_id']
    data = production(head, voice)
    data['production'].update(image_recipe='qwen-edit-2511', image_width=256, image_height=320)
    data['shots'][0].update(keyframe_review='pending', keyframe_reference_asset_ids=[head],
        keyframe_reference_notes='Figure 1 supplies the synthetic pose and exact exterior.',
        keyframe_edit_region=REGION)
    service = AigcService(system.drafts, system.assets, system.workflows, system.deps.tools)
    project = service.project(a, data)
    (tmp_path / 'source.png').write_bytes(source_png())
    (tmp_path / 'voice.wav').write_bytes(b'synthetic voice')
    manifest = tmp_path / 'project.json'
    manifest.write_text(json.dumps({'project': data, 'materials': {head: 'source.png', voice: 'voice.wav'}}))
    adapter = AigcToolAdapter(base_url='http://synthetic.invalid', sandbox_dir=tmp_path / 'runtime', ffmpeg='unused')
    runner = LocalProduction(manifest, adapter)
    scheduler(adapter, monkeypatch, Director.keyframe_args(project, project.shots[0]))
    await runner.run('keyframes', 'one', 'controlled')
    runner.review('frame', 'one', 'Synthetic source exterior and local edit inspected.')
    runner.project.shots[0].keyframe_edit_region = None
    assert not Director.requires_keyframe_receipt(runner.project, runner.project.shots[0])
    assert runner.must_verify_keyframe('one')
    with pytest.raises(AgentOSError) as local_changed:
        runner.review('frame', 'one', 'Removing the region requires a new generation attempt.')
    assert local_changed.value.code == ErrorCode.CONFLICT
    result, _ = completed_frame(system, service, project, 'keyframes')
    project.shots[0].input_asset_id, project.shots[0].keyframe_review = result, 'accepted'
    service.save(a, 'story', project.model_dump(mode='json'), 0)
    project.shots[0].keyframe_edit_region = None
    with pytest.raises(AgentOSError) as central_changed:
        service.save(a, 'story', project.model_dump(mode='json'), 1)
    assert central_changed.value.code == ErrorCode.CONFLICT


def test_unused_image_controls_keep_old_plan_digest_and_active_controls_invalidate_it():
    project = Project.model_validate(production('image', 'audio'))
    initial = Director.timeline_args(project, still=True)['plan_digest']
    assert initial == 'c4595d84466a934b2e311cdadbda6c7079fce4cb84f735cf9b54653651276202'
    project.shots[0].keyframe_edit_region = REGION
    assert Director.timeline_args(project, still=True)['plan_digest'] != initial
    project.shots[0].keyframe_edit_region = None
    project.shots[0].end_keyframe_edit_region = REGION
    assert Director.timeline_args(project, still=True)['plan_digest'] != initial
    project.shots[0].end_keyframe_edit_region = None
    project.production.image_recipe = LIGHTNING
    assert Director.timeline_args(project, still=True)['plan_digest'] != initial
