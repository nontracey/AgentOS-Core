"""Production orchestration reusing drafts, assets, capabilities and Temporal."""
from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import zipfile
from typing import Any

from pydantic import ValidationError

from agentos.domains.aigc.director import Director
from agentos.domains.aigc.production import CharacterCard, SceneCard
from agentos.domains.aigc.project import Project, Shot
from agentos.domains.aigc.sampling import REFERENCE_CLEANUP_RECIPE
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import AuthContext
from agentos.services.assets import AssetService
from agentos.services.capabilities import CapabilityCatalog
from agentos.services.drafts import DraftService
from agentos.services.workflow import WorkflowService

LABELS = {'generate': '生成候选', 'pack': '桌宠封装', 'speak': '本地配音',
          'compose': '合成成片', 'image': '本地分镜图', 'keyframes': '本地分镜图', 'endframes': '独立尾帧候选',
          'animatic': '动态分镜粗剪', 'cards': '角色与场景定妆', 'harmonize': '统一画风候选', 'lipsync': '本地口型候选'}


class AigcService:
    def __init__(self, drafts: DraftService, assets: AssetService,
                 workflows: WorkflowService, catalog: CapabilityCatalog) -> None:
        self.drafts, self.assets, self.workflows, self.catalog = drafts, assets, workflows, catalog

    def project(self, actor: AuthContext, payload: dict[str, Any]) -> Project:
        try:
            project = Project.model_validate(payload)
        except ValidationError as exc:
            raise AgentOSError(ErrorCode.INVALID, 'invalid production project') from exc

        def media(asset_id: str | None, kind: str) -> None:
            if asset_id and not self.assets.get(actor, asset_id)['mime'].startswith(kind + '/'):
                raise AgentOSError(ErrorCode.INVALID, 'production asset has the wrong media type')

        for shot in project.shots:
            media(shot.input_asset_id, 'image')
            for ref in shot.keyframe_reference_asset_ids:
                media(ref, 'image')
            for ref in shot.end_keyframe_reference_asset_ids:
                media(ref, 'image')
            media(shot.end_keyframe_asset_id, 'image')
            media(shot.selected_asset_id, 'video')
            if shot.lipsync:
                media(shot.lipsync.asset_id, 'video')
        for asset_id in (project.audio_asset_id, project.voice_asset_id):
            media(asset_id, 'audio')
        if project.production:
            media(project.production.style_reference_asset_id, 'image')
            cards: list[CharacterCard | SceneCard] = [*project.production.characters, *project.production.scenes]
            for card in cards:
                for ref in card.reference_asset_ids:
                    media(ref, 'image')
            for char in project.production.characters:
                media(char.voice_asset_id, 'audio')
            for line in project.production.dialogue:
                media(line.audio_asset_id, 'audio')
            media(project.production.animatic_asset_id, 'video')
        return project

    def save(self, actor: AuthContext, id: str, payload: dict[str, Any], revision: int) -> dict[str, Any]:
        project = self.project(actor, payload)
        self._validate_keyframes(actor, id, project)
        self._validate_selections(actor, id, project)
        self._validate_lipsync(actor, id, project)
        return self.drafts.save(actor, 'aigc', id, project.model_dump(mode='json'), revision)

    def _validate_selections(self, actor: AuthContext, id: str, project: Project) -> None:
        if not any(s.review == 'accepted' for s in project.shots):
            return
        expected = self.graph(actor, id, project, 'generate', enforce_plan=False)
        for shot in project.shots:
            if shot.review != 'accepted':
                continue
            run = self.drafts.store.get_run(actor.tenant_id, str(shot.selected_run_id))
            if run is None or run.subject != actor.subject:
                raise AgentOSError(ErrorCode.DENIED, 'selected production is unavailable')
            original = json.loads(run.envelope_json).get('payload', {}).get('workflow_graph', {})
            if original.get('id') != self.workflow_prefix(actor, id) + 'generate':
                raise AgentOSError(ErrorCode.INVALID, 'selected shot belongs to another project')
            name = 'shot_' + shot.id
            saved = next((n for n in original.get('nodes', []) if n['id'] == name), None)
            wanted = next(n for n in expected['nodes'] if n['id'] == name)
            if not saved or any(saved['config'].get(k) != wanted['config'][k]
                                for k in ('tool', 'arguments')):
                raise AgentOSError(ErrorCode.CONFLICT, 'candidate parameters differ from the current shot')
            output = json.loads(run.checkpoint_json).get('workflow_nodes', {}).get(name, {})
            if (output.get('state') != 'completed'
                    or output.get('output', {}).get('asset_id') != shot.selected_asset_id):
                raise AgentOSError(ErrorCode.INVALID, 'selected asset is not this completed candidate')

    @staticmethod
    def workflow_prefix(actor: AuthContext, id: str) -> str:
        return 'aigc_' + hashlib.sha256((actor.subject + '\0' + id).encode()).hexdigest()[:24] + '_'

    def _validate_keyframes(self, actor: AuthContext, id: str, project: Project) -> None:
        # Legacy imported frames stay compatible. Formal endpoint conditioning
        # requires completed generation provenance for both accepted candidates.
        for shot in project.shots:
            for end, selected, review in ((False, shot.input_asset_id, shot.keyframe_review),
                                         (True, shot.end_keyframe_asset_id, shot.end_keyframe_review)):
                if review != 'accepted':
                    continue
                stage, prefix = ('endframes', 'endframe_') if end else ('keyframes', 'frame_')
                meta = self.assets.get(actor, str(selected))
                run = self.drafts.store.get_run(actor.tenant_id, str(meta.get('run_id')))
                required = Director.requires_keyframe_receipt(project, shot, end=end)
                original = json.loads(run.envelope_json).get('payload', {}).get('workflow_graph', {}) if run else {}
                if not required and original.get('id') != self.workflow_prefix(actor, id) + stage:
                    continue
                if run is None or run.subject != actor.subject:
                    raise AgentOSError(ErrorCode.DENIED, 'keyframe generation provenance is unavailable')
                graph = original
                name = prefix + shot.id
                node = next((n for n in graph.get('nodes', []) if n['id'] == name), None)
                wanted = Director.keyframe_args(project, shot, end=end)
                if end:
                    self._validate_cleanup_sources(actor, project, shot)
                if (graph.get('id') != self.workflow_prefix(actor, id) + stage or not node
                        or node['config'].get('tool') != f'node_{project.node_id}_aigc_image'
                        or node['config'].get('arguments') != wanted):
                    raise AgentOSError(ErrorCode.CONFLICT, 'keyframe director or source conditions changed')
                output = json.loads(run.checkpoint_json).get('workflow_nodes', {}).get(name, {})
                if output.get('state') != 'completed' or output.get('output', {}).get('asset_id') != selected:
                    raise AgentOSError(ErrorCode.INVALID, 'keyframe is not this completed generation')
                for reference in [str(selected), *wanted['reference_asset_ids']]:
                    self.assets.read(actor, reference)
                if end and selected == shot.input_asset_id:
                    raise AgentOSError(ErrorCode.INVALID,
                                       'an ending candidate must be independent from the starting image')

    def _validate_cleanup_sources(self, actor: AuthContext, project: Project, shot: Shot) -> None:
        if not project.production or project.production.image_recipe != REFERENCE_CLEANUP_RECIPE:
            return
        Director.keyframe_args(project, shot, end=True)
        contents = [self.assets.read(actor, refs[0]) for refs in
                    (shot.keyframe_reference_asset_ids, shot.end_keyframe_reference_asset_ids)]
        if hashlib.sha256(contents[0]).digest() == hashlib.sha256(contents[1]).digest():
            raise AgentOSError(ErrorCode.INVALID, 'cleanup ending source repeats the starting source content')

    def _validate_lipsync(self, actor: AuthContext, id: str, project: Project) -> None:
        for shot in project.shots:
            lip = shot.lipsync
            if not lip or lip.review != 'accepted':
                continue
            run = self.drafts.store.get_run(actor.tenant_id, str(lip.run_id))
            if run is None or run.subject != actor.subject:
                raise AgentOSError(ErrorCode.DENIED, 'lip-sync production is unavailable')
            graph = json.loads(run.envelope_json).get('payload', {}).get('workflow_graph', {})
            name = 'lip_' + shot.id
            node = next((n for n in graph.get('nodes', []) if n['id'] == name), None)
            output = json.loads(run.checkpoint_json).get('workflow_nodes', {}).get(name, {})
            if (graph.get('id') != self.workflow_prefix(actor, id) + 'lipsync' or not node or
                node['config'].get('tool') != f'node_{project.node_id}_aigc_lipsync' or
                node['config'].get('arguments') != Director.lipsync_args(project, shot)):
                raise AgentOSError(ErrorCode.CONFLICT, 'lip-sync source, speaker or timing changed')
            if output.get('state') != 'completed' or output.get('output', {}).get('asset_id') != lip.asset_id:
                raise AgentOSError(ErrorCode.INVALID, 'lip-sync asset is not this completed result')

    def graph(self, actor: AuthContext, id: str, project: Project, stage: str,
              shot_id: str | None = None, *, enforce_plan: bool = True) -> dict[str, Any]:
        prefix = f'node_{project.node_id}_aigc_'
        nodes: list[dict[str, Any]] = []

        def add(name: str, tool: str, args: dict[str, Any], deps: list[str]) -> None:
            label = LABELS[tool]
            if tool in {'generate', 'pack', 'image'} and name.startswith(('shot_', 'pack_', 'frame_', 'endframe_')):
                shot = next(s for s in project.shots if s.id == name.split('_', 1)[1])
                label += ' · ' + (shot.label or shot.id)
            nodes.append({'id': name, 'kind': 'tool', 'label': label, 'depends_on': deps,
                          'config': {'tool': prefix + tool, 'arguments': args}})

        plan = project.production
        if stage in {'keyframes', 'endframes', 'animatic', 'generate', 'lipsync', 'compose'} and plan and enforce_plan:
            self.ready(project, video=stage in {'generate', 'lipsync', 'compose'})
            if stage in {'animatic', 'generate', 'lipsync', 'compose'}:
                self._validate_keyframes(actor, id, project)
            if stage in {'generate', 'lipsync', 'compose'}:
                self._validate_animatic(actor, id, project)
        shots = [s for s in project.shots if not shot_id or s.id == shot_id]
        if stage in {'keyframes', 'endframes', 'generate'} and not shots:
            raise AgentOSError(ErrorCode.INVALID, 'shot not found')
        previous: str | None = None
        if stage in {'cards', 'harmonize'}:
            if not plan:
                raise AgentOSError(ErrorCode.INVALID, 'structured production plan required')
            cards: list[CharacterCard | SceneCard] = [*plan.characters, *plan.scenes]
            if shot_id:
                cards = [c for c in cards if c.id == shot_id]
            if not cards:
                raise AgentOSError(ErrorCode.INVALID, 'card not found')
            for card in cards:
                name = ('character_' if isinstance(card, CharacterCard) else 'scene_') + card.id
                add(name, 'image', Director.card_args(project, card, harmonize=stage == 'harmonize'),
                    [previous] if previous else [])
                previous = name
        elif stage in {'keyframes', 'endframes'}:
            if not plan:
                raise AgentOSError(ErrorCode.INVALID, 'structured production plan required')
            for shot in shots:
                name = ('endframe_' if stage == 'endframes' else 'frame_') + shot.id
                image_args = Director.keyframe_args(project, shot, end=stage == 'endframes')
                if stage == 'endframes':
                    self._validate_cleanup_sources(actor, project, shot)
                add(name, 'image', image_args, [previous] if previous else [])
                previous = name
        elif stage == 'animatic':
            if not plan:
                raise AgentOSError(ErrorCode.INVALID, 'structured production plan required')
            add('animatic', 'animatic', self.timeline_args(project, still=True), [])
        elif stage == 'generate':
            for shot in shots:
                name = 'shot_' + shot.id
                add(name, 'generate', Director.video_args(project, shot), [previous] if previous else [])
                if project.kind == 'pet':
                    pack = 'pack_' + shot.id
                    args: dict[str, Any] = {
                        'input_asset_id': {'$ref': f'nodes.{name}.alpha_frames_asset_id'}, 'fps': project.fps}
                    if project.pet_layout:
                        args['layout'] = project.pet_layout
                    add(pack, 'pack', args, [name])
                    previous = pack
                else:
                    previous = name
        elif stage == 'speak':
            if plan:
                for line in plan.dialogue:
                    name = 'speech_' + line.id
                    add(name, 'speak', Director.speech_args(project, line),
                        [previous] if previous else [])
                    previous = name
                if not nodes:
                    raise AgentOSError(ErrorCode.INVALID, 'production has no dialogue')
            else:
                if not project.narration.strip() or not project.voice_asset_id:
                    raise AgentOSError(ErrorCode.INVALID, 'narration and reference voice required')
                add('speech', 'speak', {'text': project.narration, 'input_asset_id': project.voice_asset_id}, [])
        elif stage == 'lipsync':
            self._validate_selections(actor, id, project)
            for shot in shots:
                if shot.lipsync:
                    name = 'lip_' + shot.id
                    add(name, 'lipsync', Director.lipsync_args(project, shot), [previous] if previous else [])
                    previous = name
            if not nodes:
                raise AgentOSError(ErrorCode.INVALID, 'configure a speaking character and face region')
        elif stage == 'compose':
            if project.kind != 'story' or any(s.review != 'accepted' for s in project.shots):
                raise AgentOSError(ErrorCode.INVALID, 'review and select every shot before composing')
            self._validate_selections(actor, id, project)
            self._validate_lipsync(actor, id, project)
            add('film', 'compose', self.timeline_args(project, still=False), [])
        else:
            raise AgentOSError(ErrorCode.INVALID, 'unknown production stage')
        return {'id': self.workflow_prefix(actor, id) + stage,
                'name': project.name + ' · ' + LABELS[stage], 'nodes': nodes, 'input_schema': {}}

    shot_prompt = staticmethod(Director.shot_prompt)
    ready = staticmethod(Director.ready)
    timeline_args = staticmethod(Director.timeline_args)

    def _validate_animatic(self, actor: AuthContext, id: str, project: Project) -> None:
        plan = project.production
        assert plan is not None
        run = self.drafts.store.get_run(actor.tenant_id, str(plan.animatic_run_id))
        if run is None or run.subject != actor.subject:
            raise AgentOSError(ErrorCode.DENIED, 'approved animatic production is unavailable')
        original = json.loads(run.envelope_json).get('payload', {}).get('workflow_graph', {})
        if original.get('id') != self.workflow_prefix(actor, id) + 'animatic':
            raise AgentOSError(ErrorCode.INVALID, 'animatic belongs to another project')
        saved = next((n for n in original.get('nodes', []) if n['id'] == 'animatic'), None)
        if not saved or saved['config'].get('arguments') != self.timeline_args(project, still=True):
            raise AgentOSError(ErrorCode.CONFLICT, 'production plan changed; rebuild and review its animatic')
        output = json.loads(run.checkpoint_json).get('workflow_nodes', {}).get('animatic', {})
        if (output.get('state') != 'completed' or
                output.get('output', {}).get('asset_id') != plan.animatic_asset_id):
            raise AgentOSError(ErrorCode.INVALID, 'animatic is not this completed production')

    async def start(self, actor: AuthContext, id: str, body: dict[str, Any],
                    *, engine_enabled: bool) -> dict[str, Any]:
        snapshot = self.drafts.get(actor, 'aigc', id)
        if body.get('revision') != snapshot['revision']:
            raise AgentOSError(ErrorCode.CONFLICT, 'project changed; reload before generating')
        if not engine_enabled:
            raise AgentOSError(ErrorCode.UNAVAILABLE, 'local production requires an available workflow engine')
        key = body.get('idempotency_key')
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            raise AgentOSError(ErrorCode.INVALID, 'stable production key required')
        project = self.project(actor, snapshot['payload'])
        graph = self.graph(actor, id, project, str(body.get('stage', 'generate')), body.get('shot_id'))
        graph = await self.catalog.bind_graph(actor, graph)
        published = self.workflows.publish(actor, graph)
        return {**self.workflows.start(actor, published['workflow_id'], published['version'], {},
            'aigc:' + self.workflow_prefix(actor, id) + key), **published,
            'project_revision': snapshot['revision']}

    def export(self, actor: AuthContext, id: str) -> dict[str, Any]:
        snapshot = self.drafts.get(actor, 'aigc', id)
        project = self.project(actor, snapshot['payload'])
        if not project.production:
            raise AgentOSError(ErrorCode.INVALID, 'structured story production required for offline export')
        plan = project.production
        plan.animatic_asset_id = None
        plan.animatic_run_id = None
        plan.animatic_review = 'pending'
        refs = {s.input_asset_id for s in project.shots if s.input_asset_id}
        for shot in project.shots:
            refs.update(shot.keyframe_reference_asset_ids)
            refs.update(shot.end_keyframe_reference_asset_ids)
            if shot.end_keyframe_asset_id:
                refs.add(shot.end_keyframe_asset_id)
            if Director.requires_keyframe_receipt(project, shot):
                shot.keyframe_review = 'pending'
            if Director.requires_keyframe_receipt(project, shot, end=True):
                shot.end_keyframe_review = 'pending'
            shot.selected_asset_id = None
            shot.selected_run_id = None
            shot.review = 'pending'
            if shot.lipsync:
                shot.lipsync.asset_id = None
                shot.lipsync.run_id = None
                shot.lipsync.review = 'pending'
        cards: list[CharacterCard | SceneCard] = [*plan.characters, *plan.scenes]
        if plan.style_reference_asset_id:
            refs.add(plan.style_reference_asset_id)
        for card in cards:
            refs.update(card.reference_asset_ids)
        refs.update(c.voice_asset_id for c in plan.characters if c.voice_asset_id)
        refs.update(d.audio_asset_id for d in plan.dialogue if d.audio_asset_id)
        refs.update(x for x in (project.audio_asset_id, project.voice_asset_id) if x)
        entries = [(ref, self.assets.get(actor, ref)) for ref in sorted(refs)]
        if sum(int(meta['size']) for _, meta in entries) > 512 * 1024**2:
            raise AgentOSError(ErrorCode.INVALID, 'offline export material budget exceeded')
        document: dict[str, Any] = {'schema_version': 1, 'project': project.model_dump(mode='json'),
                                  'materials': {}, 'quality_verified': False}
        output = io.BytesIO()
        written = set()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            for ref, meta in entries:
                suffix = mimetypes.guess_extension(str(meta['mime'])) or '.bin'
                filename = 'materials/' + meta['sha256'] + suffix
                document['materials'][ref] = filename
                if filename not in written:
                    data = self.assets.read(actor, ref)
                    if hashlib.sha256(data).hexdigest() != meta['sha256']:
                        raise AgentOSError(ErrorCode.UNCERTAIN, 'export material integrity failed')
                    archive.writestr(filename, data)
                    written.add(filename)
            archive.writestr('project.json', json.dumps(document, ensure_ascii=False, indent=2))
            archive.writestr('使用说明.txt', '解压到 Windows 模型/数据盘上的项目文件夹。使用已有节点目录中的 '
                'local_drama.py，指定 project.json 与 --node-home 节点目录。'
                '按 cards、keyframes、endframes、speak、animatic、'
                'generate、lipsync、compose 分阶段制作。先审查角色和分镜图，再审查带配音的粗剪。'
                '已有角色图和对白可复用；'
                '正式视频与粗剪需在本地生成并绑定本地回执。任务中断时核对同一回执，修改输入后使用明确的新 attempt。'
                '本包不包含密码、节点配置、模型权重或运行环境；质量验收不会随导出自动通过。')
        return self.assets.register_local(actor, output.getvalue(), mime='application/zip',
            name=project.name + ' · Win 本地制作项目.zip', meta={'kind': 'aigc_export'})

    def runs(self, actor: AuthContext, id: str) -> list[dict[str, Any]]:
        self.drafts.get(actor, 'aigc', id)
        prefix = self.workflow_prefix(actor, id)
        result = []
        for row in self.drafts.store.list_workflow_runs(actor.tenant_id):
            if str(row['workflow_id']).startswith(prefix):
                run = self.drafts.store.get_run(actor.tenant_id, str(row['internal_run_id']))
                if run is not None and run.subject == actor.subject:
                    outputs = json.loads(run.checkpoint_json or '{}').get('workflow_nodes', {})
                    result.append({'run_id': run.id, 'state': run.state.value,
                        'version': row['definition_version'], 'workflow_id': row['workflow_id'],
                        'created_at': run.created_at.isoformat() if run.created_at else None,
                        'outputs': {k: v.get('output', {}) for k, v in outputs.items()
                                    if v.get('state') == 'completed'}})
        return result
