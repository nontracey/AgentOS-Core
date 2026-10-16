"""Production gates and local export integrity; GPU quality is verified separately."""
from __future__ import annotations

import json
import shutil

import httpx
import pytest
from pydantic import ValidationError

from agentos.adapters.aigc.local import LocalProduction
from agentos.adapters.aigc.media import compose, ffmpeg_run, probe_duration, register_output
from agentos.adapters.aigc.recipes import image_graph, recipe_graph
from agentos.adapters.aigc.tools import AigcToolAdapter
from agentos.domains.aigc.director import Director
from agentos.domains.aigc.project import Project
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.services.aigc import AigcService
from tests.integration.test_aigc import actor


def production(image: str, voice: str) -> dict:
    return {'name': 'Synthetic three-character drama', 'kind': 'story', 'node_id': 'windows', 'fps': 16,
        'production': {'script': 'An apprentice challenges an examiner.', 'style': 'stylized 3D',
            'characters': [{'id': 'hero', 'name': 'Hero', 'appearance': 'silver hair',
                'fixed_traits': ['small canine tips'], 'reference_asset_ids': [image],
                'voice_asset_id': voice, 'review': 'accepted'},
                {'id': 'examiner', 'name': 'Examiner', 'appearance': 'navy uniform',
                 'reference_asset_ids': [image], 'voice_asset_id': voice, 'review': 'accepted'}],
            'scenes': [{'id': 'hall', 'name': 'Hall', 'description': 'academy hall',
                       'layout': 'sealed door behind examiner', 'reference_asset_ids': [image], 'review': 'accepted'}],
            'dialogue': [{'id': 'line1', 'character_id': 'examiner', 'text': 'Stop.',
                          'start': 0, 'end': 2, 'audio_asset_id': voice}]},
        'shots': [{'id': 'one', 'input_asset_id': image, 'prompt': 'The apprentice looks at the examiner.',
            'cast': [{'character_id': 'hero', 'position': 'left', 'eyeline': 'right'},
                     {'character_id': 'examiner', 'position': 'right', 'eyeline': 'left'}],
            'scene_id': 'hall', 'purpose': 'show conflict', 'camera': 'two-shot',
            'action_start': 'hero stands still', 'action_end': 'hero looks up',
            'state_before': {'door': 'closed'}, 'state_after': {'door': 'closed'},
            'duration': 3, 'keyframe_review': 'accepted'}]}


def publish_completed(system, service, p, stage, output):
    a = actor()
    graph = service.graph(a, 'story', p, stage)
    published = system.workflows.publish(a, graph)
    run = system.workflows.start(a, published['workflow_id'], published['version'], {}, 'test-' + stage)
    rid = run['internal_run_id']
    node = graph['nodes'][0]['id']
    system.store.save_checkpoint(a.tenant_id, rid, json.dumps({'workflow_nodes': {
        node: {'state': 'completed', 'output': {'asset_id': output}}}}))
    return rid


def test_director_binds_multiple_reference_images_and_rejects_wrong_links(system):
    a = actor()
    image = system.assets.register_local(a, b'image', mime='image/png')['asset_id']
    voice = system.assets.register_local(a, b'voice', mime='audio/wav')['asset_id']
    service = AigcService(system.drafts, system.assets, system.workflows, system.deps.tools)
    data = production(image, voice)
    p = service.project(a, data)
    args = service.graph(a, 'story', p, 'keyframes')['nodes'][0]['config']['arguments']
    assert args['reference_asset_ids'] == [image, image, image]
    assert 'reference image 1' in args['prompt'] and 'reference image 2' in args['prompt']
    assert 'position: left' in args['prompt'] and 'small canine tips' in args['prompt']
    assert args['steps'] == 4 and args['recipe'] == 'klein-image' and args['width'] == 640
    with pytest.raises(AgentOSError):
        service.project(actor('other'), data)
    data['shots'][0]['cast'][0]['character_id'] = 'absent'
    with pytest.raises(ValidationError):
        Project.model_validate(data)


@pytest.mark.asyncio
@pytest.mark.parametrize('recipe,steps', [('klein-image', 4), ('qwen-edit', 40), ('qwen-edit-2511', 40)])
async def test_offline_keyframes_match_web_native_sampling(system, tmp_path, monkeypatch, recipe, steps):
    a = actor()
    image = system.assets.register_local(a, b'image', mime='image/png')['asset_id']
    voice = system.assets.register_local(a, b'voice', mime='audio/wav')['asset_id']
    service = AigcService(system.drafts, system.assets, system.workflows, system.deps.tools)
    data = production(image, voice)
    data['production']['image_recipe'] = recipe
    web_args = service.graph(a, 'story', service.project(a, data), 'keyframes')[
        'nodes'][0]['config']['arguments']

    (tmp_path / 'source.png').write_bytes(b'image')
    (tmp_path / 'voice.wav').write_bytes(b'voice')
    manifest = tmp_path / 'project.json'
    manifest.write_text(json.dumps({'project': data,
        'materials': {image: 'source.png', voice: 'voice.wav'}}))
    adapter = AigcToolAdapter(base_url='http://synthetic.invalid', sandbox_dir=tmp_path / 'runtime', ffmpeg='unused')
    runner = LocalProduction(manifest, adapter)
    calls = []

    async def call(name, args, ctx):
        calls.append(name)
        assert {key: args[key] for key in web_args} == web_args
        # Validate the actual offline request against the real recipe contract,
        # without submitting a model job or substituting a permissive fake graph.
        graph = image_graph(args, args['reference_asset_refs'], 'synthetic')
        sampler = '12' if recipe == 'klein-image' else '11'
        assert graph[sampler]['inputs']['steps'] == steps
        output = tmp_path / 'keyframe.png'
        output.write_bytes(b'generated synthetic frame')
        primary = register_output(output, adapter.assets, 'keyframe')
        return {'value': {**primary, 'assets': [primary], 'job_id': 'synthetic'}}

    monkeypatch.setattr(adapter, 'call', call)
    await runner.run('keyframes', 'one', 'native-sampling')
    assert calls == ['aigc.image']
    receipt = runner.receipts['keyframes:one:native-sampling']
    assert receipt['arguments'] == web_args and receipt['state'] == 'completed'
    assert runner.project.shots[0].keyframe_review == 'pending'
    assert runner.project.production.animatic_review == 'pending'


def test_reviewed_animatic_is_bound_to_current_plan_and_subject(system):
    a = actor()
    image = system.assets.register_local(a, b'image', mime='image/png')['asset_id']
    voice = system.assets.register_local(a, b'voice', mime='audio/wav')['asset_id']
    movie = system.assets.register_local(a, b'video', mime='video/mp4')['asset_id']
    service = AigcService(system.drafts, system.assets, system.workflows, system.deps.tools)
    p = service.project(a, production(image, voice))
    with pytest.raises(AgentOSError):
        service.graph(a, 'story', p, 'generate')
    rid = publish_completed(system, service, p, 'animatic', movie)
    p.production.animatic_asset_id = movie
    p.production.animatic_run_id = rid
    p.production.animatic_review = 'accepted'
    args = service.graph(a, 'story', p, 'generate')['nodes'][0]['config']['arguments']
    assert args['recipe'] == 'story-shot-quality'
    assert 'Final object states' in args['prompt']
    p.shots[0].camera = 'close-up'
    with pytest.raises(AgentOSError) as stale:
        service.graph(a, 'story', p, 'generate')
    assert stale.value.code == ErrorCode.CONFLICT
    p.shots[0].camera = 'two-shot'
    with pytest.raises(AgentOSError):
        service.graph(actor('other'), 'story', p, 'generate')
    with pytest.raises(AgentOSError):
        service.graph(a, 'other-project', p, 'generate')


def test_continuity_and_native_length_gate():
    p = Project.model_validate(production('image', 'audio'))
    second = p.shots[0].model_copy(deep=True, update={'id': 'two', 'state_before': {'door': 'open'}})
    p.shots.append(second)
    with pytest.raises(AgentOSError, match='object state'):
        Director.ready(p, video=False)
    p.shots.pop()
    p.shots[0].duration = 6
    with pytest.raises(AgentOSError, match='native frames'):
        Director.ready(p, video=True)


@pytest.mark.parametrize('refs', [
    ['one', 'one'], ['', 'two'], ['../outside'], ['.'], ['..'], ['x' * 129],
    ['one', 'two', 'three', 'four'],
])
def test_explicit_keyframe_reference_ids_are_bounded_and_unambiguous(refs):
    data = production('image', 'audio')
    data['shots'][0]['keyframe_reference_asset_ids'] = refs
    with pytest.raises(ValidationError):
        Project.model_validate(data)


@pytest.mark.asyncio
async def test_explicit_keyframe_roles_match_offline_and_web(system, tmp_path, monkeypatch):
    a = actor()
    image = system.assets.register_local(a, b'image', mime='image/png')['asset_id']
    layout = system.assets.register_local(a, b'layout', mime='image/png')['asset_id']
    style = system.assets.register_local(a, b'style', mime='image/png')['asset_id']
    voice = system.assets.register_local(a, b'voice', mime='audio/wav')['asset_id']
    data = production(image, voice)
    data['shots'][0].update(keyframe_reference_asset_ids=[layout, image, style],
        keyframe_reference_notes='Figure 1: layout; Figure 2: hero identity; Figure 3: materials only.')
    service = AigcService(system.drafts, system.assets, system.workflows, system.deps.tools)
    project = service.project(a, data)
    args = service.graph(a, 'story', project, 'keyframes')['nodes'][0]['config']['arguments']
    assert args == Director.keyframe_args(project, project.shots[0])
    assert args['reference_asset_ids'] == [layout, image, style]
    assert 'Figure 1: layout' in args['prompt'] and 'Figure 3: materials only' in args['prompt']
    assert 'from reference image 1' not in args['prompt'] and 'Scene (reference image 3)' not in args['prompt']

    for filename, content in [('image.png', b'image'), ('layout.png', b'layout'),
                              ('style.png', b'style'), ('voice.wav', b'voice')]:
        (tmp_path / filename).write_bytes(content)
    manifest = tmp_path / 'project.json'
    manifest.write_text(json.dumps({'project': data, 'materials': {
        image: 'image.png', layout: 'layout.png', style: 'style.png', voice: 'voice.wav'}}))
    adapter = AigcToolAdapter(base_url='http://synthetic.invalid', sandbox_dir=tmp_path / 'runtime', ffmpeg='unused')
    runner = LocalProduction(manifest, adapter)
    calls = []

    async def call(name, forwarded, ctx):
        calls.append(name)
        assert {key: forwarded[key] for key in args} == args
        image_graph(forwarded, forwarded['reference_asset_refs'], 'synthetic')
        output = tmp_path / 'frame.png'
        output.write_bytes(b'new frame')
        primary = register_output(output, adapter.assets, 'keyframe')
        return {'value': {**primary, 'assets': [primary], 'job_id': 'synthetic'}}

    monkeypatch.setattr(adapter, 'call', call)
    await runner.run('keyframes', 'one', 'roles')
    runner.project.shots[0].keyframe_reference_notes = 'Figure 1: new layout; Figure 2: identity; Figure 3: style.'
    with pytest.raises(AgentOSError, match='inputs changed'):
        await runner.run('keyframes', 'one', 'roles')
    assert calls == ['aigc.image']
    project.shots[0].keyframe_reference_notes = ''
    with pytest.raises(AgentOSError, match='require their roles'):
        Director.keyframe_args(project, project.shots[0])
    project.shots[0].keyframe_reference_notes = 'x' * 601
    with pytest.raises(ValidationError):
        Project.model_validate(project.model_dump(mode='json'))


@pytest.mark.parametrize('field', ['keyframe_reference_asset_ids', 'end_keyframe_asset_id'])
def test_new_reference_assets_require_owner_and_image_media(system, field):
    a = actor()
    image = system.assets.register_local(a, b'image', mime='image/png')['asset_id']
    voice = system.assets.register_local(a, b'voice', mime='audio/wav')['asset_id']
    foreign = system.assets.register_local(actor('other'), b'foreign', mime='image/png')['asset_id']
    service = AigcService(system.drafts, system.assets, system.workflows, system.deps.tools)
    for ref, code in [(foreign, ErrorCode.DENIED), (voice, ErrorCode.INVALID)]:
        data = production(image, voice)
        data['shots'][0][field] = [ref] if field == 'keyframe_reference_asset_ids' else ref
        with pytest.raises(AgentOSError) as rejected:
            service.project(a, data)
        assert rejected.value.code == code


@pytest.mark.parametrize('recipe', ['story-shot-guided', 'story-shot-guided-quality'])
def test_guided_video_requires_independently_reviewed_head_and_tail(recipe):
    project = Project.model_validate(production('image', 'audio'))
    project.production.animatic_review = 'accepted'
    shot = project.shots[0]
    shot.video_recipe = recipe
    with pytest.raises(AgentOSError, match='requires an end keyframe'):
        Director.video_args(project, shot)
    shot.end_keyframe_asset_id = 'tail'
    for review in ('pending', 'rejected'):
        shot.end_keyframe_review = review
        with pytest.raises(AgentOSError, match='review the end keyframe'):
            Director.ready(project, video=True)
    shot.end_keyframe_review = 'accepted'
    args = Director.video_args(project, shot)
    assert args['recipe'] == recipe and args['end_asset_id'] == 'tail'
    Director.ready(project, video=True)
    shot.keyframe_review = 'pending'
    with pytest.raises(AgentOSError, match='review keyframes'):
        Director.ready(project, video=True)
    shot.keyframe_review = 'accepted'
    shot.end_keyframe_asset_id = None
    with pytest.raises(ValidationError, match='approved end keyframe'):
        Project.model_validate(project.model_dump(mode='json'))


@pytest.mark.parametrize('recipe', ['story-shot', 'story-shot-quality'])
def test_ordinary_video_rejects_tail_instead_of_ignoring_it(recipe):
    project = Project.model_validate(production('image', 'audio'))
    shot = project.shots[0]
    shot.video_recipe = recipe
    assert 'end_asset_id' not in Director.video_args(project, shot)
    shot.end_keyframe_asset_id, shot.end_keyframe_review = 'tail', 'accepted'
    with pytest.raises(AgentOSError, match='require a guided video recipe'):
        Director.video_args(project, shot)


@pytest.mark.asyncio
async def test_guided_offline_source_signature_and_native_selection_include_tail(system, tmp_path, monkeypatch):
    a = actor()
    image = system.assets.register_local(a, b'image', mime='image/png')['asset_id']
    tail = system.assets.register_local(a, b'tail', mime='image/png')['asset_id']
    voice = system.assets.register_local(a, b'voice', mime='audio/wav')['asset_id']
    data = production(image, voice)
    data['shots'][0].update(video_recipe='story-shot-guided-quality',
                           end_keyframe_asset_id=tail, end_keyframe_review='accepted')
    service = AigcService(system.drafts, system.assets, system.workflows, system.deps.tools)
    web_args = service.graph(a, 'story', service.project(a, data), 'generate', enforce_plan=False)[
        'nodes'][0]['config']['arguments']
    for filename, content in [('image.png', b'image'), ('tail.png', b'tail'), ('voice.wav', b'voice')]:
        (tmp_path / filename).write_bytes(content)
    manifest = tmp_path / 'project.json'
    manifest.write_text(json.dumps({'project': data,
        'materials': {image: 'image.png', tail: 'tail.png', voice: 'voice.wav'}}))
    adapter = AigcToolAdapter(base_url='http://synthetic.invalid', sandbox_dir=tmp_path / 'runtime', ffmpeg='unused')
    runner = LocalProduction(manifest, adapter)

    async def call(name, forwarded, ctx):
        if name == 'aigc.generate':
            assert {key: forwarded[key] for key in web_args} == web_args
            assert forwarded['end_asset_ref'] == runner.reference(tail)
        output = tmp_path / (name + '.mp4')
        output.write_bytes(name.encode())
        primary = register_output(output, adapter.assets, 'raw_video')
        return {'value': {**primary, 'assets': [primary], 'job_id': name}}

    monkeypatch.setattr(adapter, 'call', call)
    await runner.run('animatic', None, 'guided')
    runner.review('animatic', None, 'Synthetic layout and dialogue timing verified.')
    await runner.run('generate', 'one', 'guided')
    runner.verify_native('one')
    shot = runner.project.shots[0]
    shot.end_keyframe_asset_id = image
    with pytest.raises(AgentOSError, match='obsolete director'):
        runner.verify_native('one')
    shot.end_keyframe_asset_id = tail
    (adapter.assets / runner.reference(tail)).write_bytes(b'changed tail')
    with pytest.raises(AgentOSError, match='source files changed'):
        runner.verify_native('one')


@pytest.mark.parametrize('emotion', [
    [0] * 7, [0] * 9, [-.1] + [0] * 7, [1.1] + [0] * 7,
    [float('nan')] + [0] * 7, [float('inf')] + [0] * 7, [True] + [0] * 7, ['.2'] + [0] * 7,
])
def test_dialogue_emotion_requires_eight_finite_numeric_intensities(emotion):
    data = production('image', 'audio')
    data['production']['dialogue'][0]['emotion'] = emotion
    with pytest.raises(ValidationError):
        Project.model_validate(data)


@pytest.mark.asyncio
async def test_emotional_speech_matches_web_and_offline_and_changed_emotion_needs_new_attempt(
        system, tmp_path, monkeypatch):
    a = actor()
    image = system.assets.register_local(a, b'image', mime='image/png')['asset_id']
    voice = system.assets.register_local(a, b'voice', mime='audio/wav')['asset_id']
    data = production(image, voice)
    data['production']['dialogue'][0]['emotion'] = [0, .2, 0, 0, 0, 0, 0, 0]
    service = AigcService(system.drafts, system.assets, system.workflows, system.deps.tools)
    project = service.project(a, data)
    web_args = service.graph(a, 'story', project, 'speak')['nodes'][0]['config']['arguments']
    assert web_args == Director.speech_args(project, project.production.dialogue[0])
    assert web_args['emotion'] == [0, .2, 0, 0, 0, 0, 0, 0]
    (tmp_path / 'image.png').write_bytes(b'image')
    (tmp_path / 'voice.wav').write_bytes(b'voice')
    manifest = tmp_path / 'project.json'
    manifest.write_text(json.dumps({'project': data,
        'materials': {image: 'image.png', voice: 'voice.wav'}}))
    adapter = AigcToolAdapter(base_url='http://synthetic.invalid', sandbox_dir=tmp_path / 'runtime', ffmpeg='unused')
    runner = LocalProduction(manifest, adapter)
    calls = []

    async def call(name, forwarded, ctx):
        calls.append(name)
        assert {key: forwarded[key] for key in web_args} == web_args
        output = tmp_path / 'speech.wav'
        output.write_bytes(b'emotional speech')
        primary = register_output(output, adapter.assets, 'speech')
        return {'value': {**primary, 'assets': [primary], 'job_id': 'speech'}}

    monkeypatch.setattr(adapter, 'call', call)
    await runner.run('speak', 'line1', 'emotion')
    runner.project.production.dialogue[0].emotion = [.3, 0, 0, 0, 0, 0, 0, 0]
    with pytest.raises(AgentOSError, match='inputs changed'):
        await runner.run('speak', 'line1', 'emotion')
    assert calls == ['aigc.speak']
    project.production.dialogue[0].emotion = None
    assert Director.speech_args(project, project.production.dialogue[0]) == {
        'text': 'Stop.', 'input_asset_id': voice}


def test_new_controls_preserve_old_unused_plan_digest_and_invalidate_when_used():
    project = Project.model_validate(production('image', 'audio'))
    baseline = Director.timeline_args(project, still=True)['plan_digest']
    # Captured before optional tail, reference-role and emotion fields existed.
    assert baseline == 'c4595d84466a934b2e311cdadbda6c7079fce4cb84f735cf9b54653651276202'
    for field, value in [('keyframe_reference_asset_ids', ['layout']),
                         ('keyframe_reference_notes', 'Figure 1 defines layout.'),
                         ('video_recipe', 'story-shot-guided-quality'),
                         ('end_keyframe_asset_id', 'tail'), ('end_keyframe_review', 'rejected')]:
        changed = project.model_copy(deep=True)
        setattr(changed.shots[0], field, value)
        assert Director.timeline_args(changed, still=True)['plan_digest'] != baseline
    project.production.dialogue[0].emotion = [0] * 8
    assert Director.timeline_args(project, still=True)['plan_digest'] != baseline


@pytest.mark.parametrize('width,height,fps', [
    (1280, None, None), (None, 720, None), (255, 720, None), (1281, 720, None),
    (1280, 721, None), (1922, 720, None), (1280, 1922, None), (1280, 720, 0),
    (1280, 720, 31), (1280, 720, 1.5),
])
def test_output_geometry_and_frame_rate_require_a_valid_export_canvas(width, height, fps):
    data = production('image', 'audio')
    data.update(output_width=width, output_height=height, output_fps=fps)
    with pytest.raises(ValidationError):
        Project.model_validate(data)


def test_output_overrides_change_timeline_only_and_preserve_native_length():
    project = Project.model_validate(production('image', 'audio'))
    shot = project.shots[0]
    native = Director.video_args(project, shot)
    baseline = Director.timeline_args(project, still=True)
    project.output_width, project.output_height, project.output_fps = 1280, 720, 30
    timeline = Director.timeline_args(project, still=True)
    assert (timeline['width'], timeline['height'], timeline['fps']) == (1280, 720, 30)
    assert timeline['clip_edits'] == baseline['clip_edits']
    assert timeline['dialogue_timing'] == baseline['dialogue_timing']
    assert Director.video_args(project, shot) == native
    assert native['fps'] == 16 and native['frames'] == 65
    project.production.animatic_review = 'accepted'
    # 3 seconds fits 65 native frames at 16fps, irrespective of the 30fps export.
    Director.ready(project, video=True)
    project.output_width = project.output_height = project.output_fps = None
    assert Director.timeline_args(project, still=True) == baseline


def test_output_change_invalidates_reviewed_animatic(system):
    a = actor()
    image = system.assets.register_local(a, b'image', mime='image/png')['asset_id']
    voice = system.assets.register_local(a, b'voice', mime='audio/wav')['asset_id']
    movie = system.assets.register_local(a, b'animatic', mime='video/mp4')['asset_id']
    service = AigcService(system.drafts, system.assets, system.workflows, system.deps.tools)
    project = service.project(a, production(image, voice))
    rid = publish_completed(system, service, project, 'animatic', movie)
    project.production.animatic_asset_id = movie
    project.production.animatic_run_id = rid
    project.production.animatic_review = 'accepted'
    service.graph(a, 'story', project, 'generate')
    project.output_width, project.output_height, project.output_fps = 1280, 720, 30
    with pytest.raises(AgentOSError, match='production plan changed'):
        service.graph(a, 'story', project, 'generate')


def test_local_tail_review_requires_registered_image_and_a_note_without_approving_head(tmp_path):
    data = production('image', 'voice')
    data['shots'][0].update(video_recipe='story-shot-guided-quality',
                           end_keyframe_asset_id='tail', keyframe_review='pending')
    for filename, content in [('image.png', b'image'), ('tail.png', b'tail'), ('voice.wav', b'voice')]:
        (tmp_path / filename).write_bytes(content)
    manifest = tmp_path / 'project.json'
    manifest.write_text(json.dumps({'project': data,
        'materials': {'image': 'image.png', 'tail': 'tail.png', 'voice': 'voice.wav'}}))
    adapter = AigcToolAdapter(base_url='http://synthetic.invalid', sandbox_dir=tmp_path / 'runtime', ffmpeg='unused')
    runner = LocalProduction(manifest, adapter)
    shot = runner.project.shots[0]
    with pytest.raises(AgentOSError, match='concrete visual'):
        runner.review('end_frame', 'one', 'ok')
    assert shot.end_keyframe_review == 'pending'
    runner.review('end_frame', 'one', 'Synthetic end pose, identity and prop placement checked.')
    assert shot.end_keyframe_review == 'accepted' and shot.keyframe_review == 'pending'
    assert runner.document['reviews'][-1]['kind'] == 'end_frame'
    shot.end_keyframe_asset_id = None
    with pytest.raises(AgentOSError, match='end keyframe is missing'):
        runner.review('end_frame', 'one', 'A missing end image must not be accepted.')
    shot.end_keyframe_asset_id = 'not_registered'
    with pytest.raises(AgentOSError, match='not registered'):
        runner.review('end_frame', 'one', 'An unregistered image must not be accepted.')


def test_guided_web_selection_rejects_changed_tail_or_recipe(system):
    a = actor()
    image = system.assets.register_local(a, b'image', mime='image/png')['asset_id']
    tail = system.assets.register_local(a, b'tail', mime='image/png')['asset_id']
    voice = system.assets.register_local(a, b'voice', mime='audio/wav')['asset_id']
    movie = system.assets.register_local(a, b'video', mime='video/mp4')['asset_id']
    data = production(image, voice)
    data['shots'][0].update(video_recipe='story-shot-guided-quality',
                           end_keyframe_asset_id=tail, end_keyframe_review='accepted')
    service = AigcService(system.drafts, system.assets, system.workflows, system.deps.tools)
    project = service.project(a, data)
    plan, shot = project.production, project.shots[0]
    plan.animatic_run_id = publish_completed(system, service, project, 'animatic', movie)
    plan.animatic_asset_id, plan.animatic_review = movie, 'accepted'
    shot.selected_run_id = publish_completed(system, service, project, 'generate', movie)
    shot.selected_asset_id, shot.review = movie, 'accepted'
    service._validate_selections(a, 'story', project)
    shot.end_keyframe_asset_id = image
    with pytest.raises(AgentOSError, match='parameters differ'):
        service._validate_selections(a, 'story', project)
    shot.end_keyframe_asset_id = tail
    shot.video_recipe = 'story-shot-guided'
    with pytest.raises(AgentOSError, match='parameters differ'):
        service._validate_selections(a, 'story', project)


def test_offline_export_includes_explicit_keyframe_sources_and_tail(system):
    import io
    import zipfile

    a = actor()
    refs = [system.assets.register_local(a, value, mime='image/png')['asset_id']
            for value in (b'identity', b'layout', b'style', b'tail')]
    voice = system.assets.register_local(a, b'voice', mime='audio/wav')['asset_id']
    data = production(refs[0], voice)
    data['shots'][0].update(keyframe_reference_asset_ids=refs[:3],
        keyframe_reference_notes='Figure 1: identity; Figure 2: layout; Figure 3: materials.',
        video_recipe='story-shot-guided-quality', end_keyframe_asset_id=refs[3])
    service = AigcService(system.drafts, system.assets, system.workflows, system.deps.tools)
    service.save(a, 'export', data, 0)
    output = service.export(a, 'export')
    with zipfile.ZipFile(io.BytesIO(system.assets.read(a, output['asset_id']))) as archive:
        document = json.loads(archive.read('project.json'))
        assert set(document['materials']) == {*refs, voice}
        shot = document['project']['shots'][0]
        assert shot['keyframe_reference_asset_ids'] == refs[:3]
        assert shot['end_keyframe_asset_id'] == refs[3] and shot['end_keyframe_review'] == 'pending'
        assert document['quality_verified'] is False


def test_quality_video_uses_base_models_and_image_graph_uses_separate_references():
    graph = recipe_graph({'recipe': 'story-shot-quality', 'prompt': 'nod'}, 'one.png', 'aigc/test')
    assert graph['11']['inputs']['steps'] == 20 and graph['14']['inputs']['start_at_step'] == 10
    assert not any(n['class_type'] == 'LoraLoaderModelOnly' for n in graph.values())
    image = image_graph({'recipe': 'qwen-edit-2511', 'prompt': 'two people', 'seed': 0},
                        ['hero.png', 'examiner.png', 'hall.png'], 'aigc/test')
    assert image['21']['inputs']['image'] == 'examiner.png'
    assert image['22']['inputs']['image'] == 'hall.png'
    assert image['11']['inputs']['steps'] == 40 and image['11']['inputs']['seed'] == 0
    assert image['12']['inputs']['reference_latents_method'] == 'index_timestep_zero'
    with pytest.raises(AgentOSError):
        image_graph({'recipe': 'qwen-edit', 'prompt': 'edit'}, [], 'aigc/test')


def test_image_adapter_reconciles_three_uploads_without_another_job(tmp_path, monkeypatch):
    from PIL import Image

    adapter = AigcToolAdapter(base_url='http://synthetic.invalid', sandbox_dir=tmp_path, ffmpeg='unused')
    adapter.assets.mkdir()
    for i in range(3):
        Image.new('RGB', (32, 32), (i * 60, 0, 0)).save(adapter.assets / f'{i}.png')
    args = {'recipe': 'qwen-edit', 'prompt': 'two characters',
            'reference_asset_ids': ['hero', 'examiner', 'scene'], 'reference_asset_refs': ['0.png', '1.png', '2.png']}
    ids, uploaded = [], []
    def request(client, method, path, **kw):
        if path.startswith('/.well-known'):
            return httpx.Response(200, json={'service': 'local-model-scheduler'})
        if path == '/object_info':
            return httpx.Response(200, json={n['class_type']: {} for n in image_graph(
                args, ['0.png', '1.png', '2.png'], 'test').values()})
        if path == '/upload/image':
            uploaded.append(kw['files']['image'][0])
            return httpx.Response(200, json={})
        if path == '/prompt':
            ids.append(kw['json']['prompt_id'])
            if len(ids) == 1:
                raise AgentOSError(ErrorCode.UNAVAILABLE, 'lost reply')
            return httpx.Response(200, json={'prompt_id': ids[-1]})
        if path.startswith('/history/'):
            return httpx.Response(200, json={ids[-1]: {'status': {'completed': True},
                'outputs': {'16': {'images': [{'filename': 'result.png'}]}}}} if len(ids) > 1 else {})
        raise AssertionError(path)
    monkeypatch.setattr(adapter, '_request', request)
    monkeypatch.setattr(adapter, '_download', lambda *args: b'new-image')
    with pytest.raises(AgentOSError):
        adapter._execute('aigc.image', args, 'stable', 60)
    result = adapter._execute('aigc.image', args, 'stable', 60)['value']
    assert len(uploaded) == 3 and len(set(uploaded)) == 3 and ids[0] == ids[1]
    assert result['role'] == 'keyframe' and result['mime'] == 'image/png'


def test_animatic_uses_dialogue_timing_and_rejects_clipped_speech(tmp_path):
    from PIL import Image

    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        pytest.skip('FFmpeg unavailable')
    still = tmp_path / 'frame.png'
    Image.new('RGB', (256, 256)).save(still)
    voice = tmp_path / 'line.wav'
    ffmpeg_run(ffmpeg, ['-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.3', str(voice)])
    args = dict(width=256, height=256, fps=16, still=True,
                edits=[{'start': 0, 'duration': 1}], dialogue=[voice], timing=[{'start': .2, 'end': .8}])
    assert compose(ffmpeg, [still], tmp_path, tmp_path / 'animatic.mp4', **args) == pytest.approx(1)
    assert probe_duration(ffmpeg, tmp_path / 'dialogue-master.wav') == pytest.approx(1)
    args['timing'] = [{'start': .2, 'end': .3}]
    with pytest.raises(AgentOSError, match='speech exceeds'):
        compose(ffmpeg, [still], tmp_path, tmp_path / 'bad.mp4', **args)


def test_local_export_checks_changed_source_and_corrupt_output(tmp_path):
    adapter = AigcToolAdapter(base_url='http://synthetic.invalid', sandbox_dir=tmp_path / 'runtime', ffmpeg='unused')
    src = tmp_path / 'source.png'
    src.write_bytes(b'input')
    voice = tmp_path / 'voice.wav'
    voice.write_bytes(b'voice')
    manifest = tmp_path / 'project.json'
    manifest.write_text(json.dumps({'project': production('image', 'audio'),
                                    'materials': {'image': 'source.png', 'audio': 'voice.wav'}}))
    runner = LocalProduction(manifest, adapter)
    output = tmp_path / 'result.mp4'
    output.write_bytes(b'video')
    result = register_output(output, adapter.assets, 'raw_video')
    runner.materials[result['asset_ref']] = str((adapter.assets / result['asset_ref']).relative_to(tmp_path))
    args = {'input_asset_id': 'image', 'prompt': 'nod'}
    import hashlib

    from agentos.adapters.aigc.local import digest
    runner.receipts['generate:one:1'] = {'state': 'completed', 'arguments': args,
        'signature': digest([args, [hashlib.sha256(b'input').hexdigest()]]), 'result': result}
    assert runner.verified_receipt('generate', 'one', result['asset_ref'])
    (adapter.assets / runner.reference('image')).write_bytes(b'changed')
    with pytest.raises(AgentOSError, match='source files changed'):
        runner.verified_receipt('generate', 'one', result['asset_ref'])


@pytest.mark.asyncio
async def test_completed_card_recovery_keeps_review_and_does_not_submit_an_edit(tmp_path, monkeypatch):
    adapter = AigcToolAdapter(base_url='http://synthetic.invalid', sandbox_dir=tmp_path / 'runtime', ffmpeg='unused')
    (tmp_path / 'source.png').write_bytes(b'input')
    (tmp_path / 'voice.wav').write_bytes(b'voice')
    manifest = tmp_path / 'project.json'
    manifest.write_text(json.dumps({'project': production('image', 'audio'),
                                    'materials': {'image': 'source.png', 'audio': 'voice.wav'}}))
    runner = LocalProduction(manifest, adapter)
    calls = []

    async def call(name, args, ctx):
        calls.append(name)
        output = tmp_path / 'card.png'
        output.write_bytes(b'new card')
        primary = register_output(output, adapter.assets, 'keyframe')
        return {'value': {**primary, 'assets': [primary], 'job_id': 'synthetic'}}

    monkeypatch.setattr(adapter, 'call', call)
    await runner.run('cards', 'hero', 'one')
    runner.project.production.characters[0].review = 'accepted'
    runner.persist()
    recovered = LocalProduction(manifest, adapter)
    await recovered.run('cards', 'hero', 'one')
    assert calls == ['aigc.image']
    assert recovered.project.production.characters[0].review == 'accepted'
    recovered.project.production.characters[0].appearance = 'different character'
    with pytest.raises(AgentOSError, match='card changed'):
        await recovered.run('cards', 'hero', 'one')


def test_offline_export_is_private_and_contains_no_runtime_configuration(system):
    import io
    import zipfile

    a = actor()
    image = system.assets.register_local(a, b'image', mime='image/png')['asset_id']
    voice = system.assets.register_local(a, b'voice', mime='audio/wav')['asset_id']
    service = AigcService(system.drafts, system.assets, system.workflows, system.deps.tools)
    service.save(a, 'export', production(image, voice), 0)
    result = service.export(a, 'export')
    with zipfile.ZipFile(io.BytesIO(system.assets.read(a, result['asset_id']))) as archive:
        manifest = json.loads(archive.read('project.json'))
        assert len(manifest['materials']) == 2
        assert manifest['project']['production']['animatic_review'] == 'pending'
        assert manifest['quality_verified'] is False
        assert not any(x.endswith(('comfyui.json', 'aigc.json', 'identity.json')) for x in archive.namelist())
    with pytest.raises(AgentOSError):
        service.export(actor('other'), 'export')


def test_style_master_is_authorized_and_shared_card_brief_separates_image_roles(system):
    a = actor()
    content = system.assets.register_local(a, b'content', mime='image/png')['asset_id']
    master = system.assets.register_local(a, b'master', mime='image/png')['asset_id']
    voice = system.assets.register_local(a, b'voice', mime='audio/wav')['asset_id']
    service = AigcService(system.drafts, system.assets, system.workflows, system.deps.tools)
    data = production(content, voice)
    data['production']['style_transfer_recipe'] = 'qwen-style'
    data['production'].update(style_reference_asset_id=master,
                              style_constraints='fine individual hair strands, translucent skin, woven fabric')
    p = service.project(a, data)
    card = p.production.characters[1]
    args = Director.card_args(p, card, harmonize=True)
    remote = service.graph(a, 'style', p, 'harmonize', 'examiner')['nodes'][0]['config']['arguments']
    assert remote == args
    assert args['reference_asset_ids'] == [content, master]
    assert 'Figure 1 is CONTENT only' in args['prompt'] and 'Figure 2 is STYLE only' in args['prompt']
    g = image_graph(args, ['content.png', 'style.png'], 'synthetic')
    assert g['11']['inputs']['cfg'] == 1 and g['11']['inputs']['steps'] == 4
    assert g['17']['inputs']['strength_model'] == g['18']['inputs']['strength_model'] == 1
    with pytest.raises(AgentOSError):
        image_graph(args, ['content.png'], 'synthetic')
    card.reference_asset_ids = []
    creation = Director.card_args(p, card)
    assert creation['recipe'] == p.production.image_recipe and creation['reference_asset_ids'] == [master]
    assert 'STYLE only' in creation['prompt']
    with pytest.raises(AgentOSError):
        Director.card_args(p, card, harmonize=True)
    foreign = system.assets.register_local(actor('other'), b'private', mime='image/png')['asset_id']
    data['production']['style_reference_asset_id'] = foreign
    with pytest.raises(AgentOSError):
        service.project(a, data)


def test_lipsync_draft_face_region_is_optional_but_execution_and_review_require_it():
    p = Project.model_validate(production('image', 'audio'))
    from agentos.domains.aigc.production import LipSync

    s = p.shots[0]
    s.selected_asset_id, s.review = 'video', 'accepted'
    s.lipsync = LipSync(character_id='examiner')
    with pytest.raises(AgentOSError):
        Director.lipsync_args(p, s)
    s.lipsync.face_box = [.2, .1, .7, .6]
    assert Director.lipsync_args(p, s)['dialogue_asset_ids'] == ['audio']
    with pytest.raises(ValidationError):
        LipSync(character_id='examiner', face_box=[0, float('nan'), .7, .6])
    with pytest.raises(ValidationError):
        LipSync(character_id='examiner', review='accepted', asset_id='output', run_id='run')


def test_style_enhancers_switch_harmonize_prompt_to_official_short_structure(system):
    from agentos.domains.aigc.production import StyleEnhancers

    a = actor()
    content = system.assets.register_local(a, b'content', mime='image/png')['asset_id']
    master = system.assets.register_local(a, b'master', mime='image/png')['asset_id']
    voice = system.assets.register_local(a, b'voice', mime='audio/wav')['asset_id']
    service = AigcService(system.drafts, system.assets, system.workflows, system.deps.tools)
    data = production(content, voice)
    data['production']['style_transfer_recipe'] = 'qwen-style'
    data['production'].update(style_reference_asset_id=master,
                              style_constraints='fine individual hair strands, translucent skin, woven fabric')
    p = service.project(a, data)
    card = p.production.characters[1]
    long_prompt = Director.card_args(p, card, harmonize=True)['prompt']
    assert 'Figure 1 is CONTENT only' in long_prompt

    card.reference_asset_ids = [content]
    p.production.style_enhancers = StyleEnhancers(
        content='young man, navy uniform',
        style='semi-realistic fantasy cg, fine hair strands, translucent skin, woven fabric, cinematic lighting')
    args = Director.card_args(p, card, harmonize=True)
    assert args['reference_asset_ids'] == [content, master]
    assert args['prompt'].startswith('Style Transfer the style of Figure 2 to Figure 1,')
    assert 'young man, navy uniform' in args['prompt']
    assert 'semi-realistic fantasy cg' in args['prompt']
    assert 'Figure 1 is CONTENT only' not in args['prompt']
    assert 'fine individual hair strands' in args['prompt']
    assert 'do not copy Figure 2' in args['prompt']
    assert len(args['prompt']) < 1000
    g = image_graph(args, ['content.png', 'style.png'], 'synthetic')
    assert g['11']['inputs']['cfg'] == 1 and g['11']['inputs']['steps'] == 4
    with pytest.raises(ValidationError):
        StyleEnhancers(content='', style='flat shading')



def test_upstream_style_recipe_preserves_reference_dimensions_and_role_specific_caption(tmp_path):
    from PIL import Image

    from agentos.adapters.aigc.media import prepare_reference
    from agentos.domains.aigc.production import StyleEnhancers

    p = Project.model_validate(production('content', 'voice'))
    p.production.style_reference_asset_id = 'master'
    p.production.style_constraints = 'fine hair and translucent skin'
    p.production.style_transfer_recipe = 'qwen-style-v2'
    p.production.style_enhancers = StyleEnhancers(content='young man', style='detailed fantasy CG')
    card = p.production.characters[0]
    card.style_content = 'young woman, silver hair'
    args = Director.card_args(p, card, harmonize=True)
    assert 'young woman, silver hair' in args['prompt'] and 'young man' not in args['prompt']
    graph = image_graph(args, ['content.png', 'master.png'], 'synthetic')
    assert graph['9']['class_type'] == 'ModelSamplingFlux'
    assert graph['9']['inputs']['height'] == args['height']
    assert 'vae' not in graph['7']['inputs'] and '8' not in graph
    assert graph['25']['inputs']['latent'] == ['19', 0]
    assert graph['26']['inputs']['latent'] == ['23', 0]
    # The pinned acceleration file is rank 64 / alpha 8. Diffusers 0.38
    # drops alpha for its A/B keys, whereas Comfy applies alpha / rank.
    # Compensate only this upstream recipe to reproduce PEFT's unit scale.
    assert graph['18']['inputs']['strength_model'] * 8 / 64 == 1
    assert graph['17']['inputs']['strength_model'] == 1
    legacy_args = {**args, 'recipe': 'qwen-style'}
    legacy = image_graph(legacy_args, ['content.png', 'master.png'], 'synthetic')
    assert legacy['18']['inputs']['strength_model'] == 1
    source = tmp_path / 'source.png'
    target = tmp_path / 'prepared.png'
    Image.new('RGB', (512, 1024), (255, 0, 0)).save(source)
    prepare_reference(source, target, 832, 1024)
    with Image.open(target) as im:
        assert im.height / im.width == pytest.approx(2, abs=.05)
        assert im.width * im.height == pytest.approx(832 * 1024, rel=.03)
        assert im.getpixel((0, 0)) == (255, 0, 0)


def test_fixed_klein_native_sampling_and_three_distinct_references():
    args = {'recipe': 'klein-image', 'prompt': 'three distinct people', 'seed': 7, 'width': 768, 'height': 1024}
    graph = image_graph(args, ['one.png', 'two.png', 'three.png'], 'synthetic')
    assert graph['3']['inputs'] == {'unet_name': 'flux-2-klein-4b.safetensors', 'weight_dtype': 'default'}
    assert graph['12']['inputs']['steps'] == 4 and graph['13']['inputs']['cfg'] == 1
    assert graph['14']['inputs']['noise_seed'] == 7
    assert graph['16']['class_type'] == 'SaveImage'
    assert [node['inputs']['image'] for node in graph.values() if node['class_type'] == 'LoadImage'] == [
        'one.png', 'two.png', 'three.png']
    positive, negative = graph['13']['inputs']['positive'], graph['13']['inputs']['negative']
    encoded = []
    while graph[positive[0]]['class_type'] == 'ReferenceLatent':
        pos, neg = graph[positive[0]]['inputs'], graph[negative[0]]['inputs']
        assert pos['latent'] == neg['latent']
        encoded.append(graph[graph[pos['latent'][0]]['inputs']['pixels'][0]]['inputs']['image'])
        positive, negative = pos['conditioning'], neg['conditioning']
    assert encoded == ['three.png', 'two.png', 'one.png']
    assert positive == ['7', 0] and negative == ['8', 0]
    with pytest.raises(AgentOSError, match='four native steps'):
        image_graph({**args, 'steps': 40}, ['one.png'], 'synthetic')
    with pytest.raises(AgentOSError):
        image_graph(args, ['one.png'] * 4, 'synthetic')


def test_new_production_stays_klein_for_cards_with_or_without_references():
    project = Project.model_validate(production('image', 'audio'))
    plan = project.production
    assert plan.image_recipe == plan.style_transfer_recipe == 'klein-image'
    card = plan.characters[0]
    for refs in ([], ['image']):
        args = Director.card_args(project, card, references=refs)
        assert args['recipe'] == 'klein-image' and args['steps'] == 4
        graph = image_graph(args, ['one.png'] if refs else [], 'synthetic')
        assert bool(refs) == any(n['class_type'] == 'LoadImage' for n in graph.values())
    plan.style_reference_asset_id = 'master'
    plan.style_constraints = 'same light and materials'
    args = Director.card_args(project, card, harmonize=True)
    assert args['reference_asset_ids'] == ['image', 'master']
    assert args['recipe'] == 'klein-image' and args['steps'] == 4


@pytest.mark.parametrize('short_caption', [False, True])
def test_harmonize_preserves_each_subjects_teeth_and_full_style_requirements(short_caption):
    from agentos.domains.aigc.production import StyleEnhancers

    project = Project.model_validate(production('image', 'audio'))
    plan = project.production
    plan.style_reference_asset_id = 'master'
    plan.style_constraints = 'translucent skin, fine individual hair, woven cloth'
    if short_caption:
        plan.style_enhancers = StyleEnhancers(content='portrait', style='fantasy CG')
    hero, examiner = plan.characters
    hero.fixed_traits = ['two tiny upper canine tips']
    hero.wardrobe = 'black lace collar and silver pendant'
    examiner.fixed_traits = ['ordinary human teeth, no fangs']
    examiner.wardrobe = 'navy uniform with gold trim'
    for card in (hero, examiner):
        prompt = Director.card_args(project, card, harmonize=True)['prompt']
        assert all(trait in prompt for trait in card.fixed_traits)
        assert card.wardrobe in prompt and plan.style_constraints in prompt
        assert 'never add fangs to this subject' not in prompt
    assert 'ordinary human teeth' not in Director.card_args(project, hero, harmonize=True)['prompt']
    assert 'two tiny upper canine tips' not in Director.card_args(project, examiner, harmonize=True)['prompt']


def test_klein_reference_retains_all_edges_and_fits_resource_bound(tmp_path):
    from PIL import Image

    from agentos.adapters.aigc.media import prepare_reference

    source, target = tmp_path / 'synthetic.png', tmp_path / 'prepared.png'
    image = Image.new('RGB', (400, 1600), 'white')
    for x0, y0, color in ((0, 0, 'red'), (200, 0, 'green'), (0, 1400, 'blue'), (200, 1400, 'black')):
        image.paste(color, (x0, y0, x0 + 200, y0 + 200))
    image.save(source)
    prepare_reference(source, target, 768, 1024, max_side=1024)
    with Image.open(target) as result:
        assert result.size == (256, 1024)
        assert result.getpixel((5, 5)) == (255, 0, 0)
        assert result.getpixel((250, 5)) == (0, 128, 0)
        assert result.getpixel((5, 1018)) == (0, 0, 255)
        assert result.getpixel((250, 1018)) == (0, 0, 0)
