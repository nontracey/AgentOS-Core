"""Offline project export runner: existing ComfyUI queue, TTS worker and FFmpeg.

This operator entry point executes one reviewed production stage at a time. It does
not launch backends or introduce a second model queue. The web workbench continues
to use its existing Temporal engine and authorization/asset services.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import httpx

from agentos.adapters.aigc.media import asset_file, register_output, save_json
from agentos.adapters.aigc.tools import AigcToolAdapter
from agentos.domains.aigc.director import Director
from agentos.domains.aigc.production import CharacterCard, SceneCard
from agentos.domains.aigc.project import Project
from agentos.domains.aigc.sampling import REFERENCE_CLEANUP_RECIPE
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import AuthContext, CallContext, utc_now


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class LocalProduction:
    def __init__(self, manifest: Path, adapter: AigcToolAdapter) -> None:
        self.manifest = manifest.resolve()
        self.root = self.manifest.parent
        self.adapter = adapter
        self.document = json.loads(self.manifest.read_text(encoding='utf-8-sig'))
        self.project = Project.model_validate(self.document['project'])
        self.materials: dict[str, str] = self.document.setdefault('materials', {})
        self.receipts: dict[str, Any] = self.document.setdefault('receipts', {})
        for id, ref in list(self.materials.items()):
            path = (self.root / ref).resolve()
            if not path.is_relative_to(self.root) or not path.is_file():
                raise AgentOSError(ErrorCode.INVALID, 'project material is outside the export or missing')
            registered = register_output(path, adapter.assets, 'material')
            # Files are content addressed. Editing a source changes the next request signature.
            self.materials[id] = str((adapter.assets / registered['asset_ref']).relative_to(self.root))
        self.persist()

    def persist(self) -> None:
        self.document['project'] = self.project.model_dump(mode='json')
        save_json(self.manifest, self.document)
        if os.name != 'nt':
            self.manifest.chmod(0o600)

    def arguments(self, args: dict[str, Any]) -> dict[str, Any]:
        forwarded = dict(args)
        for field in ('input_asset', 'end_asset', 'audio_asset'):
            if args.get(field + '_id'):
                forwarded[field + '_ref'] = self.reference(str(args[field + '_id']))
        for field in ('clip_asset', 'reference_asset', 'dialogue_asset'):
            if field + '_ids' in args:
                forwarded[field + '_refs'] = [self.reference(str(x)) for x in args[field + '_ids']]
        return forwarded

    def reference(self, id: str) -> str:
        ref = self.materials.get(id)
        if not ref:
            raise AgentOSError(ErrorCode.INVALID, 'export asset is not registered')
        return asset_file(self.adapter.assets, Path(ref).name).name

    async def execute(self, stage: str, item: str, args: dict[str, Any], attempt: str) -> dict[str, Any]:
        tool = {'cards': 'image', 'harmonize': 'image', 'keyframes': 'image', 'endframes': 'image',
                'generate': 'generate', 'speak': 'speak',
                'animatic': 'animatic', 'lipsync': 'lipsync', 'compose': 'compose'}[stage]
        key = f'{stage}:{item}:{attempt}'
        forwarded = self.arguments(args)
        refs = [self.adapter.assets / value for field, value in forwarded.items() if field.endswith('_asset_ref')]
        refs += [self.adapter.assets / value for field, values in forwarded.items()
                 if field.endswith('_asset_refs') for value in values]
        signature = digest([args, [hashlib.sha256(p.read_bytes()).hexdigest() for p in refs]])
        previous = self.receipts.get(key)
        if previous and previous['signature'] != signature:
            raise AgentOSError(ErrorCode.CONFLICT, 'stage inputs changed; use an explicit new attempt')
        self.receipts.setdefault(key, {'signature': signature, 'arguments': args, 'state': 'prepared'})
        self.persist()
        actor = AuthContext(subject='local-export-owner', tenant_id='local-export', roles=['admin'],
            scopes=['read', 'write', 'exec'], policy_version='local-export', expires_at=utc_now()+timedelta(hours=2))
        ctx = CallContext(actor=actor, run_id='local-export', step_id=key, call_id=key,
            idempotency_key=digest([str(self.manifest), key]), fence=1,
            deadline_at=utc_now()+timedelta(seconds=self.adapter.timeout))
        result = await self.adapter.call('aigc.' + tool, forwarded, ctx)
        output = cast(dict[str, Any], result['value'])
        for asset in output['assets']:
            self.materials[asset['asset_ref']] = str((self.adapter.assets / asset['asset_ref']).relative_to(self.root))
        self.receipts[key].update(state='completed', result=output)
        self.persist()
        print(json.dumps({'stage': stage, 'item': item, 'completed': True,
                          'job_id': output['job_id'], 'media': output['asset_ref']}, ensure_ascii=False), flush=True)
        return output

    def verified_receipt(self, stage: str, item: str, id: str) -> dict[str, Any]:
        matches = [r for key, r in self.receipts.items() if key.startswith(stage + ':' + item + ':')
                   and r.get('state') == 'completed' and r['result']['asset_ref'] == id]
        if not matches:
            raise AgentOSError(ErrorCode.INVALID, 'selected result is not a completed local production')
        result = cast(dict[str, Any], matches[-1])
        forwarded = self.arguments(result['arguments'])
        refs = [self.adapter.assets / value for field, value in forwarded.items() if field.endswith('_asset_ref')]
        refs += [self.adapter.assets / value for field, values in forwarded.items()
                 if field.endswith('_asset_refs') for value in values]
        if result['signature'] != digest([result['arguments'], [hashlib.sha256(p.read_bytes()).hexdigest()
                                                               for p in refs]]):
            raise AgentOSError(ErrorCode.CONFLICT, 'production source files changed since this result')
        path = asset_file(self.adapter.assets, self.reference(id))
        if hashlib.sha256(path.read_bytes()).hexdigest() != result['result']['sha256']:
            raise AgentOSError(ErrorCode.UNCERTAIN, 'selected result failed integrity verification')
        return result

    async def run(self, stage: str, item: str | None, attempt: str) -> None:
        p, plan = self.project, self.project.production
        if not plan:
            raise AgentOSError(ErrorCode.INVALID, 'full story production export required')
        if stage in {'keyframes', 'endframes', 'animatic', 'generate', 'lipsync', 'compose'}:
            Director.ready(p, video=stage in {'generate', 'lipsync', 'compose'})
        if stage in {'animatic', 'generate', 'lipsync', 'compose'}:
            for shot in p.shots:
                if self.must_verify_keyframe(shot.id) and shot.keyframe_review == 'accepted':
                    self.verify_keyframe(shot.id)
                if self.must_verify_keyframe(shot.id, end=True) and shot.end_keyframe_review == 'accepted':
                    self.verify_keyframe(shot.id, end=True)
        if stage in {'generate', 'lipsync', 'compose'}:
            assert plan.animatic_asset_id
            receipt = self.verified_receipt('animatic', 'timeline', plan.animatic_asset_id)
            if receipt['arguments'] != Director.timeline_args(p, still=True):
                raise AgentOSError(ErrorCode.CONFLICT, 'production changed; rebuild and review the animatic')
        shots = [s for s in p.shots if not item or s.id == item]
        if stage in {'cards', 'harmonize'}:
            cards: list[CharacterCard | SceneCard] = [*plan.characters, *plan.scenes]
            if item and not any(c.id == item for c in cards):
                raise AgentOSError(ErrorCode.INVALID, 'card not found')
            for card in cards:
                if item and card.id != item:
                    continue
                arguments = Director.card_args(p, card, harmonize=stage == 'harmonize')
                previous = self.receipts.get(f'{stage}:{card.id}:{attempt}')
                if previous and previous.get('state') == 'completed':
                    selected = previous['result']['asset_ref']
                    if card.reference_asset_ids == [selected]:
                        # Binding our own output must not turn a recovered text-to-image
                        # request into a new image-edit request or invalidate its review.
                        original_refs = list(previous.get('card_reference_asset_ids',
                                                          previous['arguments']['reference_asset_ids']))
                        arguments = Director.card_args(p, card, harmonize=stage == 'harmonize',
                                                       references=original_refs)
                        if arguments != previous['arguments']:
                            raise AgentOSError(ErrorCode.CONFLICT, 'card changed; use an explicit new attempt')
                        self.verified_receipt(stage, card.id, selected)
                        print(json.dumps({'stage': stage, 'item': card.id, 'reused': True}), flush=True)
                        continue
                original_refs = list(card.reference_asset_ids)
                result = await self.execute(stage, card.id, arguments, attempt)
                self.receipts[f'{stage}:{card.id}:{attempt}']['card_reference_asset_ids'] = original_refs
                card.reference_asset_ids = [result['asset_ref']]
                card.review = 'pending'
                plan.animatic_review = 'pending'
                self.persist()
        elif stage == 'speak':
            if not plan.dialogue or item and not any(d.id == item for d in plan.dialogue):
                raise AgentOSError(ErrorCode.INVALID, 'dialogue not found')
            for line in plan.dialogue:
                if item and line.id != item:
                    continue
                result = await self.execute(stage, line.id, Director.speech_args(p, line), attempt)
                line.audio_asset_id = result['asset_ref']
                plan.animatic_review = 'pending'
                self.persist()
        elif stage in {'keyframes', 'endframes', 'generate'}:
            if not shots:
                raise AgentOSError(ErrorCode.INVALID, 'shot not found')
            for shot in shots:
                args = (Director.keyframe_args(p, shot, end=stage == 'endframes') if stage != 'generate'
                        else Director.video_args(p, shot))
                if stage == 'endframes':
                    self.verify_cleanup_sources(shot.id)
                selected = shot.end_keyframe_asset_id if stage == 'endframes' else shot.input_asset_id
                previous = self.receipts.get(f'{stage}:{shot.id}:{attempt}')
                recovered = (stage != 'generate' and previous and previous.get('state') == 'completed'
                             and previous['result']['asset_ref'] == selected)
                result = await self.execute(stage, shot.id, args, attempt)
                if stage != 'generate':
                    end = stage == 'endframes'
                    if recovered and result['asset_ref'] == selected:
                        # Both stages preserve only a genuine review of this same
                        # completed attempt, with unchanged arguments and source bytes.
                        self.verify_keyframe(shot.id, end=end)
                        continue
                    if end:
                        shot.end_keyframe_asset_id = result['asset_ref']
                        shot.end_keyframe_review = 'pending'
                    else:
                        shot.input_asset_id = result['asset_ref']
                        shot.keyframe_review = 'pending'
                    plan.animatic_review = 'pending'
                else:
                    shot.selected_asset_id = result['asset_ref']
                    shot.selected_run_id = result['job_id']
                shot.review = 'pending'
                self.persist()
        elif stage == 'lipsync':
            if not any(s.lipsync for s in shots):
                raise AgentOSError(ErrorCode.INVALID, 'configure a speaking character and face region')
            for shot in shots:
                if shot.lipsync:
                    self.verify_native(shot.id)
                    result = await self.execute(stage, shot.id, Director.lipsync_args(p, shot), attempt)
                    shot.lipsync.asset_id = result['asset_ref']
                    shot.lipsync.run_id = result['job_id']
                    shot.lipsync.review = 'pending'
                    self.persist()
        elif stage in {'animatic', 'compose'}:
            if stage == 'compose':
                if any(s.review != 'accepted' or not s.selected_asset_id for s in p.shots):
                    raise AgentOSError(ErrorCode.INVALID, 'review every native video candidate before composing')
                for shot in p.shots:
                    self.verify_native(shot.id)
                    if shot.lipsync:
                        receipt = self.verified_receipt('lipsync', shot.id, str(shot.lipsync.asset_id))
                        if receipt['arguments'] != Director.lipsync_args(p, shot):
                            raise AgentOSError(ErrorCode.CONFLICT, 'lip-sync source, speaker or timing changed')
            result = await self.execute(stage, 'timeline',
                                        Director.timeline_args(p, still=stage == 'animatic'), attempt)
            if stage == 'animatic':
                plan.animatic_asset_id = result['asset_ref']
                plan.animatic_run_id = result['job_id']
                plan.animatic_review = 'pending'
            else:
                self.document['film'] = {'asset_ref': result['asset_ref'], 'quality_review': 'pending',
                                         'market_quality_verified': False}
            self.persist()
        else:
            raise AgentOSError(ErrorCode.INVALID, 'unknown local production stage')

    def verify_native(self, item: str) -> None:
        p, plan = self.project, self.project.production
        assert plan is not None
        shot = next(s for s in p.shots if s.id == item)
        receipt = self.verified_receipt('generate', shot.id, str(shot.selected_asset_id))
        wanted = Director.video_args(p, shot)
        if receipt['arguments'] != wanted:
            raise AgentOSError(ErrorCode.CONFLICT, 'selected video has obsolete director or source inputs')

    def verify_keyframe(self, item: str, *, end: bool = False) -> None:
        shot = next(s for s in self.project.shots if s.id == item)
        stage = 'endframes' if end else 'keyframes'
        selected = shot.end_keyframe_asset_id if end else shot.input_asset_id
        receipt = self.verified_receipt(stage, shot.id, str(selected))
        if receipt['arguments'] != Director.keyframe_args(self.project, shot, end=end):
            raise AgentOSError(ErrorCode.CONFLICT, 'keyframe director or source conditions changed')
        if end:
            self.verify_cleanup_sources(item)
        if end and selected == shot.input_asset_id:
            raise AgentOSError(ErrorCode.INVALID, 'an ending candidate must be independent from the starting image')

    def verify_cleanup_sources(self, item: str) -> None:
        plan = self.project.production
        if not plan or plan.image_recipe != REFERENCE_CLEANUP_RECIPE:
            return
        shot = next(s for s in self.project.shots if s.id == item)
        Director.keyframe_args(self.project, shot, end=True)
        paths = [asset_file(self.adapter.assets, self.reference(refs[0])) for refs in
                 (shot.keyframe_reference_asset_ids, shot.end_keyframe_reference_asset_ids)]
        if hashlib.sha256(paths[0].read_bytes()).digest() == hashlib.sha256(paths[1].read_bytes()).digest():
            raise AgentOSError(ErrorCode.INVALID, 'cleanup ending source repeats the starting source content')

    def must_verify_keyframe(self, item: str, *, end: bool = False) -> bool:
        shot = next(s for s in self.project.shots if s.id == item)
        if Director.requires_keyframe_receipt(self.project, shot, end=end):
            return True
        selected = shot.end_keyframe_asset_id if end else shot.input_asset_id
        stage = 'endframes' if end else 'keyframes'
        return any(key.startswith(stage + ':' + item + ':') and receipt.get('state') == 'completed'
                   and receipt['result']['asset_ref'] == selected for key, receipt in self.receipts.items())

    def review(self, kind: str, item: str | None, note: str) -> None:
        if len(note.strip()) < 10:
            raise AgentOSError(ErrorCode.INVALID, 'a concrete visual or pacing review note is required')
        plan = self.project.production
        assert plan is not None
        if kind in {'character', 'scene'}:
            cards = plan.characters if kind == 'character' else plan.scenes
            card = next((c for c in cards if c.id == item), None)
            if card is None or not card.reference_asset_ids:
                raise AgentOSError(ErrorCode.INVALID, 'card is missing or has no references')
            for ref in card.reference_asset_ids:
                self.reference(ref)
            card.review = 'accepted'
        elif kind in {'frame', 'end_frame', 'video'}:
            shot = next((s for s in self.project.shots if s.id == item), None)
            if shot is None:
                raise AgentOSError(ErrorCode.INVALID, 'shot not found')
            if kind == 'frame':
                self.reference(shot.input_asset_id)
                if self.must_verify_keyframe(shot.id):
                    self.verify_keyframe(shot.id)
                shot.keyframe_review = 'accepted'
            elif kind == 'end_frame':
                if not shot.end_keyframe_asset_id:
                    raise AgentOSError(ErrorCode.INVALID, 'end keyframe is missing')
                self.reference(str(shot.end_keyframe_asset_id))
                if self.must_verify_keyframe(shot.id, end=True):
                    self.verify_keyframe(shot.id, end=True)
                shot.end_keyframe_review = 'accepted'
            else:
                self.verified_receipt('generate', shot.id, str(shot.selected_asset_id))
                shot.review = 'accepted'
        elif kind == 'animatic':
            self.verified_receipt('animatic', 'timeline', str(plan.animatic_asset_id))
            plan.animatic_review = 'accepted'
        elif kind == 'lipsync':
            shot = next((s for s in self.project.shots if s.id == item), None)
            if not shot or not shot.lipsync:
                raise AgentOSError(ErrorCode.INVALID, 'lip-sync candidate missing')
            self.verify_native(shot.id)
            receipt = self.verified_receipt('lipsync', shot.id, str(shot.lipsync.asset_id))
            if receipt['arguments'] != Director.lipsync_args(self.project, shot):
                raise AgentOSError(ErrorCode.CONFLICT, 'lip-sync inputs changed')
            shot.lipsync.review = 'accepted'
        self.document.setdefault('reviews', []).append({'kind': kind, 'item': item, 'note': note,
                                                       'created_at': utc_now().isoformat()})
        self.persist()


def draft_script(root: Path, brief: Path, model: str, attempt: str) -> None:
    text = brief.read_text(encoding='utf-8-sig')
    request = {'model': model, 'stream': False, 'think': False, 'options': {'num_ctx': 4096, 'num_predict': 2000},
               'messages': [{'role': 'system', 'content': '你是短剧编剧。写完整可拍摄的中文短场景：角色目标、冲突、'
                    '对白、动作、反转和结尾钩子。用人物互动推进故事。不要写生成模型参数。'},
                            {'role': 'user', 'content': text}]}
    receipt = root / ('script-' + hashlib.sha256(attempt.encode()).hexdigest()[:16] + '.json')
    signature = digest(request)
    if receipt.exists():
        saved = json.loads(receipt.read_text())
        if saved['signature'] != signature:
            raise AgentOSError(ErrorCode.CONFLICT, 'script attempt inputs changed')
        if saved['state'] == 'completed':
            return
        raise AgentOSError(ErrorCode.UNCERTAIN, 'local script request was interrupted; inspect before a new attempt')
    save_json(receipt, {'signature': signature, 'state': 'submitting'})
    with httpx.Client(trust_env=False, timeout=5460) as client:
        discovery = client.get('http://127.0.0.1:11434/.well-known/local-model-scheduler')
        discovery.raise_for_status()
        if discovery.json().get('service') != 'local-model-scheduler':
            raise AgentOSError(ErrorCode.DENIED, 'local screenplay requires managed Ollama')
        result = client.post('http://127.0.0.1:11434/api/chat', json=request)
        result.raise_for_status()
        value = result.json()
    save_json(receipt, {'signature': signature, 'state': 'completed', 'response': value})
    (root / '剧本草稿.md').write_text(value['message']['content'], encoding='utf-8')
    print('Local screenplay draft saved for story review', flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description='Windows offline short-drama production export')
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--node-home', type=Path, required=True)
    parser.add_argument('--stage',
                        choices=['cards', 'harmonize', 'keyframes', 'endframes', 'speak', 'animatic', 'generate',
                                 'lipsync', 'compose'])
    parser.add_argument('--item')
    parser.add_argument('--attempt', default='1')
    parser.add_argument('--review',
                        choices=['character', 'scene', 'frame', 'end_frame', 'animatic', 'video', 'lipsync'])
    parser.add_argument('--note', default='')
    parser.add_argument('--draft-script', type=Path)
    parser.add_argument('--model', default='qwen2.5:7b-instruct')
    args = parser.parse_args()
    root = args.manifest.resolve().parent
    if os.name == 'nt' and root.drive.upper() == 'C:':
        raise AgentOSError(ErrorCode.INVALID, 'keep production files on the existing model/data drive')
    if args.draft_script:
        draft_script(root, args.draft_script, args.model, args.attempt)
        return
    cfg = json.loads((args.node_home / 'comfyui.json').read_text(encoding='utf-8-sig'))
    if cfg.get('managed_by') != 'local-model-scheduler':
        raise AgentOSError(ErrorCode.DENIED, 'existing managed node configuration required')
    production = json.loads((args.node_home / 'aigc.json').read_text(encoding='utf-8-sig'))
    adapter = AigcToolAdapter(base_url=cfg['base_url'], sandbox_dir=root / 'runtime',
        ffmpeg=production['ffmpeg'], speech_command=production.get('speech_command'),
        lipsync_command=production.get('lipsync_command'), image_loras=production.get('image_loras', {}),
        recipe_loras=production.get('recipe_loras', {}), recipe_models=production.get('recipe_models', {}))
    runner = LocalProduction(args.manifest, adapter)
    if args.review:
        runner.review(args.review, args.item, args.note)
    elif args.stage:
        asyncio.run(runner.run(args.stage, args.item, args.attempt))
    else:
        print(json.dumps({'valid': True, 'characters': len(runner.project.production.characters)
            if runner.project.production else 0, 'shots': len(runner.project.shots)}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (AgentOSError, httpx.HTTPError, ValueError, OSError) as error:
        print(json.dumps({'failed': True, 'category': error.code.value if isinstance(error, AgentOSError)
                          else type(error).__name__, 'message': str(error) if isinstance(error, AgentOSError)
                          else 'Inspect the private production receipt before retrying'}))
        raise SystemExit(1) from None
