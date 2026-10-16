"""Director intent and timeline validation shared by the workbench and local export."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from agentos.domains.aigc.production import CharacterCard, Dialogue, SceneCard
from agentos.domains.aigc.project import Project, Shot
from agentos.domains.aigc.sampling import REFERENCE_CLEANUP_RECIPE, default_image_steps
from agentos.kernel.errors import AgentOSError, ErrorCode


class Director:
    @staticmethod
    def card_args(project: Project, card: CharacterCard | SceneCard, *, harmonize: bool = False,
                  references: list[str] | None = None) -> dict[str, Any]:
        plan = project.production
        assert plan is not None
        recipe: str
        refs = list(card.reference_asset_ids if references is None else references)
        content = (card.appearance + '; fixed traits: ' + ', '.join(card.fixed_traits) +
                   '; costume: ' + card.wardrobe if isinstance(card, CharacterCard) else
                   card.description + '; layout: ' + card.layout + '; lighting: ' + card.lighting)
        if harmonize:
            if not refs or not plan.style_reference_asset_id or not plan.style_constraints.strip():
                raise AgentOSError(ErrorCode.INVALID,
                    'style transfer needs a content image, master style image and material/proportion brief')
            if refs[0] == plan.style_reference_asset_id:
                raise AgentOSError(ErrorCode.INVALID, 'the style master already defines the target style')
            refs = [refs[0], plan.style_reference_asset_id]
            enh = plan.style_enhancers
            if plan.style_transfer_recipe == 'qwen-style-v2' and (not enh or not card.style_content.strip()):
                raise AgentOSError(ErrorCode.INVALID,
                    'supply this subjects short content caption and the master style caption')
            if enh is not None and enh.content.strip() and enh.style.strip():
                content_caption = (card.style_content or enh.content).strip().strip(',')
                prompt = ('Style Transfer the style of Figure 2 to Figure 1, and keep the content and '
                          'characteristics of Figure 1.,' + content_caption + ',' +
                          enh.style.strip().strip(',') +
                          '. Preserve identity, gender, age and costume design of Figure 1. '
                          'Subject requirements: ' + content + '. '
                          'Rendering requirements: ' + plan.style_constraints + '. '
                          'Keep this subject own hair, face, teeth and costume; do not copy Figure 2 '
                          'identity, teeth, costume or props. No text or watermark.')
            else:
                prompt = ('Style Transfer the style of Figure 2 to Figure 1, and keep the content and '
                          'characteristics of Figure 1. Figure 1 is CONTENT only: ' + content +
                          '. Preserve its identity, gender, age, pose, framing, costume design and background layout. '
                          'Figure 2 is STYLE only: ' + plan.style + '; ' + plan.style_constraints +
                          '. Transfer rendering, facial detail, material treatment and lighting coherence. '
                          'Do not copy Figure 2 character identity, hair color, costume or props. '
                          'Do not preserve Figure 1 plastic shading or simplified surface detail. '
                          'No text or watermark.')
            recipe, steps = plan.style_transfer_recipe, 4
        else:
            if plan.image_recipe == REFERENCE_CLEANUP_RECIPE:
                raise AgentOSError(ErrorCode.INVALID,
                                   'reference cleanup prepares shot sources, not character or scene art')
            prompt = ('Single character production reference, front-facing head and upper body, '
                      'neutral studio background, no contact sheet or letters. ' if isinstance(card, CharacterCard) else
                      'Single establishing production environment, no people, no text. ') + content
            if refs:
                prompt += '. Figures 1 through ' + str(len(refs)) + ' define CONTENT and identity for this subject.'
            if plan.style_reference_asset_id and plan.style_reference_asset_id not in refs:
                if len(refs) >= 3:
                    raise AgentOSError(ErrorCode.INVALID, 'leave one reference slot for the master style image')
                refs.append(plan.style_reference_asset_id)
                prompt += ('. Figure ' + str(len(refs)) + ' defines STYLE only. Match its rendering and materials; '
                           'create the specified subject, do not copy its character or costume.')
            prompt += ('; visual style: ' + plan.style +
                       '; material and proportion requirements: ' + plan.style_constraints)
            recipe = plan.image_recipe if refs or plan.image_recipe == 'klein-image' else 'qwen-image'
            steps = default_image_steps(recipe)
        if len(prompt) > 4000:
            raise AgentOSError(ErrorCode.INVALID, 'complete card instruction exceeds prompt limit')
        return {'recipe': recipe, 'reference_asset_ids': refs, 'prompt': prompt,
                'width': plan.image_width, 'height': plan.image_height, 'steps': steps, 'seed': 1}

    @staticmethod
    def keyframe_args(project: Project, shot: Shot, *, end: bool = False) -> dict[str, Any]:
        plan = project.production
        if not plan:
            raise AgentOSError(ErrorCode.INVALID, 'structured production plan required')
        cleanup = plan.image_recipe == REFERENCE_CLEANUP_RECIPE
        if cleanup and len(shot.keyframe_reference_asset_ids) != 2:
            raise AgentOSError(ErrorCode.INVALID,
                               'reference cleanup requires explicit ordered source and mask references')
        if shot.keyframe_reference_asset_ids:
            if not end and not shot.keyframe_reference_notes.strip():
                raise AgentOSError(ErrorCode.INVALID, 'explicit keyframe references require their roles')
            refs = list(shot.keyframe_reference_asset_ids)
        else:
            refs = [next(c for c in plan.characters if c.id == b.character_id).reference_asset_ids[
                    b.reference_index] for b in shot.cast]
            if len(refs) < 3:
                refs.append(next(s for s in plan.scenes if s.id == shot.scene_id).reference_asset_ids[0])
        if end:
            if (not shot.end_keyframe_reference_asset_ids or not shot.end_keyframe_reference_notes.strip()
                    or not shot.action_end.strip()):
                raise AgentOSError(ErrorCode.INVALID,
                                   'end keyframes require independent source references, roles and final action')
            end_refs = list(shot.end_keyframe_reference_asset_ids)
            if shot.input_asset_id in end_refs or not set(end_refs).difference(refs):
                raise AgentOSError(ErrorCode.INVALID,
                                   'end keyframes require an independent ending source, not the starting image')
            if cleanup and (len(end_refs) != 2 or end_refs[0] == refs[0]):
                raise AgentOSError(ErrorCode.INVALID, 'cleanup ending source must differ from the starting source')
            refs = end_refs
        region = shot.end_keyframe_edit_region if end else shot.keyframe_edit_region
        if cleanup:
            if region is None or shot.image_lora_id:
                raise AgentOSError(ErrorCode.INVALID,
                                   'reference cleanup needs an explicit context region and no image LoRA')
            notes = shot.end_keyframe_reference_notes if end else shot.keyframe_reference_notes
            return {'recipe': plan.image_recipe, 'reference_asset_ids': refs, 'edit_region': list(region),
                'prompt': ('Reference preparation only, using a non-diffusion CNN. Image 1 is the original source; '
                           'image 2 is its binary glyph mask (white edits, black preserves). The region supplies '
                           'context; only white mask pixels may change. This text is review context, not model '
                           'text conditioning. ' + shot.id + ': ' + notes)}
        args: dict[str, Any] = {'recipe': plan.image_recipe, 'reference_asset_ids': refs,
                'prompt': Director.shot_prompt(project, shot, still=True, end=end), 'negative': shot.negative,
                'seed': shot.seed, 'steps': default_image_steps(plan.image_recipe),
                'width': plan.image_width, 'height': plan.image_height}
        if region is not None:
            if len(refs) != 1:
                raise AgentOSError(ErrorCode.INVALID, 'regional image editing requires exactly one source reference')
            args['edit_region'] = list(region)
        if shot.image_lora_id:
            if plan.image_recipe != 'klein-image' or len(shot.cast) != 1:
                raise AgentOSError(ErrorCode.INVALID, 'identity image LoRA requires a single-subject Klein shot')
            args.update(image_lora_id=shot.image_lora_id, image_lora_strength=shot.image_lora_strength)
        return args

    @staticmethod
    def requires_keyframe_receipt(project: Project, shot: Shot, *, end: bool = False) -> bool:
        if project.production and project.production.image_recipe == REFERENCE_CLEANUP_RECIPE:
            return True
        if shot.end_keyframe_reference_asset_ids:
            return True
        if end:
            return shot.end_keyframe_edit_region is not None
        return (shot.keyframe_edit_region is not None or
                bool(project.production and project.production.image_recipe == 'qwen-edit-2511-lightning'))

    @staticmethod
    def video_args(project: Project, shot: Shot) -> dict[str, Any]:
        plan = project.production
        if project.kind == 'pet':
            if shot.video_recipe or shot.end_keyframe_asset_id:
                raise AgentOSError(ErrorCode.INVALID, 'story video overrides require a story project')
            recipe = 'pet-motion'
        else:
            recipe = shot.video_recipe or (plan.video_recipe if plan else 'story-shot')
        guided = recipe in {'story-shot-guided', 'story-shot-guided-quality'}
        if guided and not shot.end_keyframe_asset_id:
            raise AgentOSError(ErrorCode.INVALID, 'guided video requires an end keyframe')
        if shot.end_keyframe_asset_id and not guided:
            raise AgentOSError(ErrorCode.INVALID, 'end keyframes require a guided video recipe')
        if shot.end_keyframe_asset_id and shot.end_keyframe_review != 'accepted':
            raise AgentOSError(ErrorCode.INVALID, 'review the end keyframe before generating video')
        if not shot.input_asset_id:
            raise AgentOSError(ErrorCode.INVALID, 'approved shot reference required')
        args: dict[str, Any] = {'recipe': recipe, 'input_asset_id': shot.input_asset_id,
            'prompt': Director.shot_prompt(project, shot), 'negative': shot.negative,
            'seed': shot.seed, 'frames': shot.frames, 'fps': project.fps,
            'width': project.width, 'height': project.height}
        if shot.end_keyframe_asset_id:
            args['end_asset_id'] = shot.end_keyframe_asset_id
        return args

    @staticmethod
    def speech_args(project: Project, line: Dialogue) -> dict[str, Any]:
        plan = project.production
        if not plan:
            raise AgentOSError(ErrorCode.INVALID, 'structured production plan required')
        char = next(c for c in plan.characters if c.id == line.character_id)
        if not char.voice_asset_id:
            raise AgentOSError(ErrorCode.INVALID, 'every speaking character needs a local voice reference')
        args: dict[str, Any] = {'text': line.text, 'input_asset_id': char.voice_asset_id}
        if line.emotion is not None:
            args['emotion'] = list(line.emotion)
        return args

    @staticmethod
    def lipsync_args(project: Project, shot: Shot) -> dict[str, Any]:
        plan, lip = project.production, shot.lipsync
        if not plan or not lip or not lip.face_box or not shot.selected_asset_id or shot.review != 'accepted':
            raise AgentOSError(ErrorCode.INVALID, 'approved native candidate and speaker face region required')
        start = sum(s.duration or 0 for s in project.shots[:project.shots.index(shot)])
        end = start + (shot.duration or 0)
        lines = [d for d in plan.dialogue if d.character_id == lip.character_id and d.start < end and d.end > start]
        if not lines or any(not d.audio_asset_id for d in lines):
            raise AgentOSError(ErrorCode.INVALID, 'bind audio for overlapping speaking lines before lip sync')
        timing = []
        for line in lines:
            begin, finish = max(line.start, start), min(line.end, end)
            cue = {'start': begin - start, 'end': finish - start}
            # Omit the optional field for whole lines so existing requests and
            # completed receipt signatures stay exactly the same.
            if line.start < start or line.end > end:
                cue['source_start'] = begin - line.start
            timing.append(cue)
        args = {'input_asset_id': shot.selected_asset_id, 'start': shot.trim_in, 'duration': shot.duration,
                'fps': (project.output_fps or project.fps) if shot.retime_native else project.fps,
                'face_box': lip.face_box,
                'dialogue_asset_ids': [d.audio_asset_id for d in lines],
                'dialogue_timing': timing}
        if shot.retime_native:
            args['retime_native'] = True
        return args

    @staticmethod
    def shot_prompt(project: Project, shot: Shot, *, still: bool = False, end: bool = False) -> str:
        pieces = [('Narrative context: ' if end else '') + shot.prompt]
        if shot.role:
            pieces.append('Character notes: ' + shot.role)
        if shot.scene:
            pieces.append('Scene notes: ' + shot.scene)
        plan = project.production
        if plan:
            pieces += ['Visual style: ' + plan.style +
                       '; required materials and proportions: ' + plan.style_constraints,
                       'Camera: ' + shot.camera,
                       'Narrative purpose: ' + shot.purpose]
            explicit_refs = bool(shot.end_keyframe_reference_asset_ids if end else shot.keyframe_reference_asset_ids)
            if still and explicit_refs:
                notes = shot.end_keyframe_reference_notes if end else shot.keyframe_reference_notes
                pieces.append('Reference image roles: ' + notes)
            if end:
                pieces.append('Render the independent END state from its reference roles and final action. '
                              'Cast identity notes do not imply simultaneous visible people; follow the specified '
                              'final visibility and transformation state. Do not reuse the starting image.')
            for index, blocking in enumerate(shot.cast):
                char = next(c for c in plan.characters if c.id == blocking.character_id)
                identity_label = 'Cast identity' if end else 'Character'
                pieces.append(f'{identity_label} {index + 1} ({char.name})' +
                    (f', from reference image {index + 1}' if still and not explicit_refs else '') +
                    ': ' + char.appearance +
                    '; fixed traits: ' + ', '.join(char.fixed_traits) + '; costume: ' + char.wardrobe +
                    ('' if end else '; position: ' + blocking.position + '; looks toward: ' + blocking.eyeline +
                     '; expression: ' + blocking.expression))
            scene = next((s for s in plan.scenes if s.id == shot.scene_id), None)
            if scene:
                scene_ref = (f' (reference image {len(shot.cast) + 1})'
                             if still and not explicit_refs and len(shot.cast) < 3 else '')
                pieces.append('Scene' + scene_ref + ': ' + scene.description + '; layout: ' + scene.layout +
                              '; lighting: ' + scene.lighting)
            if not end:
                pieces.append('Initial action: ' + shot.action_start)
                pieces.append('Initial object states: ' + json.dumps(shot.state_before, ensure_ascii=False))
            if end or not still:
                pieces += ['End action: ' + shot.action_end,
                           'Final object states: ' + json.dumps(shot.state_after, ensure_ascii=False)]
        prompt = '\n'.join(pieces)
        if len(prompt) > 4000:
            raise AgentOSError(ErrorCode.INVALID, 'combined director and reference notes exceed prompt limit')
        return prompt

    @staticmethod
    def ready(project: Project, *, video: bool) -> None:
        plan = project.production
        assert plan is not None
        cards: list[CharacterCard | SceneCard] = [*plan.characters, *plan.scenes]
        if any(c.review != 'accepted' for c in cards):
            raise AgentOSError(ErrorCode.INVALID, 'review character and scene cards before production')
        previous: dict[str, dict[str, str]] = {}
        for shot in project.shots:
            for blocking in shot.cast:
                card = next(c for c in plan.characters if c.id == blocking.character_id)
                if blocking.reference_index >= len(card.reference_asset_ids):
                    raise AgentOSError(ErrorCode.INVALID, 'shot character view is missing from its approved card')
            if not shot.scene_id or not shot.camera or not shot.purpose or not shot.duration:
                raise AgentOSError(ErrorCode.INVALID, 'complete cast, scene, camera, purpose and timing for every shot')
            if not shot.action_start or not shot.action_end:
                raise AgentOSError(ErrorCode.INVALID, 'every shot needs start and end actions')
            last = previous.get(shot.scene_id, {})
            if any(key in last and last[key] != value for key, value in shot.state_before.items()):
                raise AgentOSError(ErrorCode.INVALID, 'object state changes between shots without an action')
            previous[shot.scene_id] = {**last, **shot.state_after}
            if video and (shot.keyframe_review != 'accepted' or
                          not shot.retime_native and shot.trim_in + shot.duration > shot.frames / project.fps):
                raise AgentOSError(ErrorCode.INVALID, 'review keyframes and generate enough native frames')
            if video:
                Director.video_args(project, shot)
        if video and plan.animatic_review != 'accepted':
            raise AgentOSError(ErrorCode.INVALID, 'review the animatic before generating final motion')

    @staticmethod
    def timeline_args(project: Project, *, still: bool) -> dict[str, Any]:
        args: dict[str, Any] = {'clip_asset_ids': [s.input_asset_id if still else
                                                 s.lipsync.asset_id if s.lipsync else s.selected_asset_id
                                                 for s in project.shots],
            'width': project.output_width or project.width, 'height': project.output_height or project.height,
            'fps': project.output_fps or project.fps,
            'subtitles': [s.model_dump(mode='json') for s in project.subtitles]}
        if project.audio_asset_id:
            args['audio_asset_id'] = project.audio_asset_id
        if project.production:
            plan = project.production
            if any(not d.audio_asset_id for d in plan.dialogue):
                raise AgentOSError(ErrorCode.INVALID, 'generate and bind every dialogue line before editing')
            if any(s.keyframe_review != 'accepted' for s in project.shots):
                raise AgentOSError(ErrorCode.INVALID, 'review keyframes before editing')
            if not still and any(s.lipsync and s.lipsync.review != 'accepted' for s in project.shots):
                raise AgentOSError(ErrorCode.INVALID, 'review every requested lip-sync candidate before composing')
            args['clip_edits'] = [{'start': 0 if still or s.lipsync else s.trim_in, 'duration': s.duration}
                                  for s in project.shots]
            if not still:
                for shot, edit in zip(project.shots, args['clip_edits'], strict=True):
                    if shot.retime_native:
                        edit['retime_native'] = True
            args['dialogue_asset_ids'] = [d.audio_asset_id for d in plan.dialogue]
            args['dialogue_timing'] = [{'start': d.start, 'end': d.end} for d in plan.dialogue]
            plan_data = plan.model_dump(mode='json', exclude={'animatic_asset_id', 'animatic_review',
                                                             'animatic_run_id', 'quality_notes'})
            # New optional controls must invalidate a used plan, while their
            # unused defaults preserve the signatures of existing productions.
            for line in plan_data['dialogue']:
                if line.get('emotion') is None:
                    line.pop('emotion', None)
            shot_data = []
            for shot in project.shots:
                data = shot.model_dump(mode='json', exclude={'review', 'selected_asset_id', 'selected_run_id'})
                for key, default in (('keyframe_reference_asset_ids', []), ('keyframe_reference_notes', ''),
                                     ('end_keyframe_reference_asset_ids', []), ('end_keyframe_reference_notes', ''),
                                     ('keyframe_edit_region', None), ('end_keyframe_edit_region', None),
                                     ('retime_native', False),
                                     ('video_recipe', None), ('end_keyframe_asset_id', None), ('image_lora_id', None)):
                    if data.get(key) == default:
                        data.pop(key, None)
                if not shot.image_lora_id and shot.image_lora_strength == 1:
                    data.pop('image_lora_strength', None)
                if not shot.end_keyframe_asset_id and shot.end_keyframe_review == 'pending':
                    data.pop('end_keyframe_review', None)
                if data.get('lipsync'):
                    data['lipsync'] = {k: v for k, v in data['lipsync'].items()
                                       if k not in {'review', 'asset_id', 'run_id'}}
                shot_data.append(data)
            args['plan_digest'] = hashlib.sha256(json.dumps([plan_data, shot_data], sort_keys=True,
                                                            ensure_ascii=False).encode()).hexdigest()
        return args
