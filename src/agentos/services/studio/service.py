"""Studio: story projects whose media are produced by governed workflow runs.

- A project is one private, revisioned draft (``StudioDoc``) in the existing DraftService.
- Every generation request publishes a small workflow and starts it through WorkflowService.
  Media nodes are the generation node's tools (policy, approvals or owner grants, ledger and
  audit apply as for any tool step); writing, storyboarding and the final cut are
  server-registered custom nodes. One-click production chains one run per stage.
- Candidates ("takes") are read back from run outputs and uploads; nothing else records them.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from agentos.adapters.aigc.compositor import find_ffmpeg
from agentos.adapters.aigc.preview import h264_preview, media_duration, thumbnail
from agentos.adapters.tools.node_hub import sanitize_tool_name
from agentos.domains.aigc.studio import director, plan
from agentos.domains.aigc.studio.engines import ENGINES, KIND_NAMES, availability
from agentos.domains.aigc.studio.models import (
    EMOTIONS,
    SLOTS,
    STYLES,
    Project,
    StudioDoc,
    Upload,
    slot_key,
)
from agentos.domains.aigc.studio.plan import NodeSpec, Pick, PlanError
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import AuthContext, utc_now
from agentos.services.studio.media import VARIANTS, MediaSigner
from agentos.services.studio.runs import NodeView, RunView, StudioRuns, workflow_prefix

log = logging.getLogger(__name__)


@dataclass
class Snapshot:
    """A project as it stands: the saved document, its runs, candidates and the takes in use."""
    doc: StudioDoc
    views: list[RunView]
    takes: list[dict[str, Any]]
    picks: dict[str, Pick]
    updated_at: str

KIND = "studio"
MEDIA_TYPES = {"image/png": "image", "image/jpeg": "image", "image/webp": "image", "audio/wav": "audio",
               "audio/x-wav": "audio", "audio/mpeg": "audio", "audio/mp4": "audio", "audio/x-m4a": "audio",
               "audio/flac": "audio", "video/mp4": "video", "video/webm": "video", "video/quicktime": "video"}
NODE_TOOLS = ("aigc.image", "aigc.generate", "aigc.speak", "aigc.lipsync")
MAX_ITEMS = 98            # graph limit is 100 nodes; one-click runs keep room for the continuation node
MAX_ROUNDS = 30           # a stage that keeps needing work is stopped rather than looped forever
NO_ENGINE = ("生成和导出需要工作流引擎：在部署配置里设置 workflow_engine = \"local\"，"
             "或连接 Temporal 服务")
NO_FFMPEG = "控制面没有找到 FFmpeg：安装 FFmpeg，或在部署配置里指定 studio_ffmpeg"


class StudioService:
    def __init__(self, *, store: Any, drafts: Any, workflows: Any, catalog: Any, assets: Any, access: Any,
                 chat: Any, accelerator: Any, signer: MediaSigner, node_id: str, ffmpeg: str, cache_dir: Path,
                 engine: Callable[[], Any], tool_hub: Any, grants: Any = None) -> None:
        self.store, self.drafts, self.workflows, self.catalog = store, drafts, workflows, catalog
        self.assets, self.access, self.chat, self.accelerator = assets, access, chat, accelerator
        self.signer, self.node_id, self.ffmpeg_setting = signer, node_id, ffmpeg
        self.cache_dir, self.engine, self.tool_hub, self.grants = cache_dir, engine, tool_hub, grants
        self.runs = StudioRuns(store)
        self.progress: dict[str, dict[str, Any]] = {}          # job id -> live progress of custom nodes
        self.subscribers: list[tuple[str, str, asyncio.Queue[dict[str, Any]]]] = []
        self._durations: dict[str, float] = {}
        self._node_cache: tuple[float, list[dict[str, Any]], str] | None = None
        self._watcher: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ helpers
    def ffmpeg(self) -> str | None:
        return find_ffmpeg(self.ffmpeg_setting)

    def engine_name(self) -> str:
        engine = self.engine()
        return str(getattr(engine, "name", "temporal")) if engine is not None else ""

    def tool_name(self, tool: str) -> str:
        return sanitize_tool_name(f"node:{self.node_id}:{tool}")

    def _context(self) -> dict[str, Any]:
        return {"progress": self.progress, "accelerator": self.accelerator.snapshot()}

    # ------------------------------------------------------------------ documents
    def load(self, actor: AuthContext, pid: str) -> tuple[StudioDoc, int, str]:
        try:
            draft = self.drafts.get(actor, KIND, pid)
        except AgentOSError as exc:
            if exc.code == ErrorCode.INVALID:
                raise AgentOSError(ErrorCode.INVALID, "项目不存在") from exc
            raise
        return StudioDoc.model_validate(draft["payload"]), int(draft["revision"]), str(draft["updated_at"])

    def mutate(self, actor: AuthContext, pid: str, change: Callable[[StudioDoc], None]) -> StudioDoc:
        """Load, change and save with the draft's optimistic revision, retrying lost races."""
        for _ in range(6):
            doc, revision, _stamp = self.load(actor, pid)
            change(doc)
            try:
                self.drafts.save(actor, KIND, pid, doc.model_dump(mode="json"), revision)
            except AgentOSError as exc:
                if exc.code == ErrorCode.CONFLICT:
                    continue
                raise
            return doc
        raise AgentOSError(ErrorCode.CONFLICT, "项目正被同时修改，请稍后重试")

    # ------------------------------------------------------------------ takes
    def project_runs(self, actor: AuthContext, pid: str, rows: list[dict[str, Any]] | None = None) -> list[RunView]:
        return self.runs.project_runs(actor.tenant_id, actor.subject, pid, rows=rows, **self._context())

    def duration(self, actor: AuthContext, asset_id: str) -> float:
        if asset_id not in self._durations:
            try:
                path = Path(str(self.assets.get_internal(actor, asset_id)["path"]))
            except AgentOSError:
                return 0.0
            self._durations[asset_id] = round(media_duration(path, self.ffmpeg() or ""), 3)
        return self._durations[asset_id]

    def takes(self, actor: AuthContext, doc: StudioDoc, views: list[RunView], *,
              durations: bool = False) -> list[dict[str, Any]]:
        """Visible candidates, oldest first: completed node outputs and attached media."""
        hidden = set(doc.hidden)
        items: list[tuple[str, int, dict[str, Any]]] = []
        for view in views:
            for order, node in enumerate(view.nodes):
                asset_id = str((node.output or {}).get("asset_id") or "")
                if node.state != "succeeded" or not node.slot or not asset_id or node.id in hidden:
                    continue
                meta = {**node.meta, "engine": node.engine}
                meta.pop("progress", None)
                if node.kind == "render":
                    meta.update({k: v for k, v in (node.output or {}).items() if k != "asset_id"})
                items.append((view.created_at, order, {
                    "id": node.id, "project_id": doc.project.id, "slot": node.slot, "target_id": node.target,
                    "asset_id": asset_id, "job_id": node.id, "meta": meta, "origin": "generated",
                    "created_at": node.finished_at or view.created_at}))
        for order, upload in enumerate(doc.uploads):
            if upload.id in hidden:
                continue
            items.append((upload.created_at, order, {
                "id": upload.id, "project_id": doc.project.id, "slot": upload.slot, "target_id": upload.target_id,
                "asset_id": upload.asset_id, "job_id": "", "meta": {"uploaded": True, "name": upload.name,
                                                                     **upload.meta},
                "origin": "upload", "created_at": upload.created_at}))
        items.sort(key=lambda item: (item[0], item[1]))
        result = []
        for _, _, take in items:
            take["rating"] = int(doc.ratings.get(take["id"], 0))
            if durations and take["slot"] in ("voice", "voice_sample", "voice_ref", "music", "clip", "lipsync"):
                take["meta"].setdefault("duration", self.duration(actor, take["asset_id"]))
            result.append(take)
        return result

    @staticmethod
    def picks(doc: StudioDoc, takes: list[dict[str, Any]]) -> dict[str, Pick]:
        """The take used per slot: the explicit choice, else the first candidate (newest render)."""
        grouped: dict[str, list[dict[str, Any]]] = {}
        for take in takes:
            grouped.setdefault(slot_key(take["slot"], take["target_id"]), []).append(take)
        chosen: dict[str, Pick] = {}
        for key, candidates in grouped.items():
            slot = key.split(":", 1)[0]
            wanted = doc.selections.get(key)
            if slot == "music" or wanted == "":
                continue
            explicit = [t for t in candidates if t["id"] == wanted]
            used = explicit[0] if explicit else candidates[-1] if slot == "render" else candidates[0]
            chosen[key] = Pick(used["id"], used["asset_id"], float(used["meta"].get("duration") or 0.0))
        return chosen

    def state(self, actor: AuthContext, pid: str, *, durations: bool = True) -> Snapshot:
        doc, _revision, stamp = self.load(actor, pid)
        views = self.project_runs(actor, pid)
        takes = self.takes(actor, doc, views, durations=durations)
        return Snapshot(doc, views, takes, self.picks(doc, takes), stamp)

    def media_url(self, actor: AuthContext, asset_id: str, *, width: int = 0, variant: str = "") -> str:
        return self.signer.url(asset_id, actor.tenant_id, actor.subject, width=width, variant=variant)

    def take_view(self, actor: AuthContext, take: dict[str, Any]) -> dict[str, Any]:
        row = self.store.get_asset(actor.tenant_id, take["asset_id"]) or {}
        mime = str(row.get("mime") or "")
        view = {**take, "mime": mime, "url": self.media_url(actor, take["asset_id"])}
        if mime.startswith("image/"):
            view["thumb"] = self.media_url(actor, take["asset_id"], width=480)
        elif mime.startswith("video/") and self.ffmpeg():
            view["thumb"] = self.media_url(actor, take["asset_id"], width=480)
            view["poster_url"] = self.media_url(actor, take["asset_id"], variant="poster")
            if mime != "video/mp4":
                view["preview_url"] = self.media_url(actor, take["asset_id"], variant="preview")
        meta = take.get("meta") or {}
        if meta.get("poster_asset_id"):
            view["poster_url"] = self.media_url(actor, str(meta["poster_asset_id"]))
            view["thumb"] = self.media_url(actor, str(meta["poster_asset_id"]), width=480)
        if meta.get("srt_asset_id"):
            view["srt_url"] = self.media_url(actor, str(meta["srt_asset_id"]))
        return view

    # ------------------------------------------------------------------ projects
    def list_projects(self, actor: AuthContext) -> list[dict[str, Any]]:
        items = []
        rows = self.store.list_workflow_runs(actor.tenant_id)
        for draft in self.drafts.list(actor, KIND):
            try:
                doc = StudioDoc.model_validate(draft["payload"])
            except ValidationError:
                continue
            project = doc.project
            views = self.project_runs(actor, project.id, rows)
            picks = self.picks(doc, self.takes(actor, doc, views))
            cover = next((picks[k].asset_id for k in [slot_key("frame", s.id) for s in project.shots] +
                          [slot_key("char_ref", c.id) for c in project.characters] +
                          [slot_key("loc_ref", loc.id) for loc in project.locations] if k in picks), "")
            items.append({"id": project.id, "title": project.title, "logline": project.logline,
                          "format": project.format, "style_id": project.style_id, "genre": project.genre,
                          "revision": doc.revision, "created_at": draft["updated_at"],
                          "updated_at": draft["updated_at"],
                          "cover": self.media_url(actor, cover, width=640) if cover else "",
                          "progress": plan.progress(project, picks),
                          "active_jobs": sum(1 for v in views if not v.settled for n in v.nodes
                                             if n.state in ("queued", "running", "approval", "blocked"))})
        return items

    def create_project(self, actor: AuthContext, body: dict[str, Any]) -> dict[str, Any]:
        title = str(body.get("title") or "").strip()[:60] or "未命名作品"
        try:
            project = Project(title=title, format=body.get("format") or "9:16",
                              style_id=str(body.get("style_id") or "anime-cel"),
                              premise=str(body.get("premise") or "")[:4000], genre=str(body.get("genre") or "")[:40],
                              target_seconds=int(body.get("target_seconds") or 60),
                              script=str(body.get("script") or "")[:60000])
        except (ValidationError, ValueError) as exc:
            raise AgentOSError(ErrorCode.INVALID, "项目参数无效") from exc
        self.drafts.save(actor, KIND, project.id, StudioDoc(project=project).model_dump(mode="json"), 0)
        return self.view(actor, project.id)

    def view(self, actor: AuthContext, pid: str) -> dict[str, Any]:
        now = self.state(actor, pid)
        project = now.doc.project
        return {"project": {**project.model_dump(mode="json"), "revision": now.doc.revision,
                            "updated_at": now.updated_at},
                "selections": {key: pick.take_id for key, pick in now.picks.items()},
                "takes": [self.take_view(actor, take) for take in now.takes],
                "jobs": self.jobs_of(now.views, limit=200),
                "warnings": director.continuity_warnings(project),
                "progress": plan.progress(project, now.picks),
                "estimate": plan.estimate(project, now.picks)}

    def save(self, actor: AuthContext, pid: str, body: dict[str, Any]) -> dict[str, Any]:
        expected = body.get("revision")
        data = body.get("project")
        if not isinstance(data, dict) or type(expected) is not int:
            raise AgentOSError(ErrorCode.INVALID, "缺少项目内容或版本号")
        try:
            project = Project.model_validate({**data, "id": pid})
        except ValidationError as exc:
            first = exc.errors()[0]
            raise AgentOSError(ErrorCode.INVALID,
                               f"项目内容无效：{'.'.join(map(str, first['loc']))} {first['msg']}") from exc

        def apply(doc: StudioDoc) -> None:
            if doc.revision != expected:
                raise AgentOSError(ErrorCode.CONFLICT, "项目已在别处修改，请刷新后再保存")
            doc.project = project
            doc.revision += 1

        self.mutate(actor, pid, apply)
        return self.view(actor, pid)

    def delete(self, actor: AuthContext, pid: str) -> None:
        _doc, revision, _stamp = self.load(actor, pid)
        for view in self.project_runs(actor, pid):
            if not view.settled:
                self.store.request_cancel(actor.tenant_id, view.run_id)
        self.drafts.delete(actor, KIND, pid, revision)

    # ------------------------------------------------------------------ choices about takes
    def _take(self, actor: AuthContext, pid: str, take_id: str) -> dict[str, Any]:
        take = next((t for t in self.state(actor, pid, durations=False).takes if t["id"] == take_id), None)
        if take is None:
            raise AgentOSError(ErrorCode.INVALID, "结果不存在")
        return take

    def select(self, actor: AuthContext, pid: str, slot: str, target_id: str, take_id: str | None) -> dict[str, Any]:
        now = self.state(actor, pid, durations=False)
        if slot not in SLOTS or not plan.target_exists(now.doc.project, slot, target_id):
            raise AgentOSError(ErrorCode.INVALID, "选择的位置不存在")
        if take_id and not any(t["id"] == take_id and t["slot"] == slot and t["target_id"] == target_id
                               for t in now.takes):
            raise AgentOSError(ErrorCode.INVALID, "这个结果不属于该位置")

        def apply(current: StudioDoc) -> None:
            current.selections[slot_key(slot, target_id)] = take_id or ""

        self.mutate(actor, pid, apply)
        picks = self.state(actor, pid, durations=False).picks
        return {"selections": {key: pick.take_id for key, pick in picks.items()}}

    def rate(self, actor: AuthContext, pid: str, take_id: str, rating: int) -> None:
        self._take(actor, pid, take_id)
        value = max(-1, min(5, int(rating)))

        def apply(doc: StudioDoc) -> None:
            if value:
                doc.ratings[take_id] = value
            else:
                doc.ratings.pop(take_id, None)

        self.mutate(actor, pid, apply)

    def hide(self, actor: AuthContext, pid: str, take_id: str) -> None:
        take = self._take(actor, pid, take_id)

        def apply(doc: StudioDoc) -> None:
            if take_id not in doc.hidden:
                doc.hidden.append(take_id)
            key = slot_key(take["slot"], take["target_id"])
            if doc.selections.get(key) == take_id:
                doc.selections.pop(key)

        self.mutate(actor, pid, apply)

    def _attach(self, actor: AuthContext, pid: str, upload: Upload, *, select: bool) -> dict[str, Any]:
        def apply(doc: StudioDoc) -> None:
            doc.uploads.append(upload)
            if select:
                doc.selections[slot_key(upload.slot, upload.target_id)] = upload.id

        doc = self.mutate(actor, pid, apply)
        take = next(t for t in self.takes(actor, doc, [], durations=True) if t["id"] == upload.id)
        self.publish(actor.tenant_id, actor.subject, {"type": "take", "take": take, "selected": select})
        return self.take_view(actor, take)

    def promote_voice(self, actor: AuthContext, pid: str, take_id: str) -> dict[str, Any]:
        """Use a good generated line as the character's reference voice, for consistency."""
        take = self._take(actor, pid, take_id)
        doc, _revision, _stamp = self.load(actor, pid)
        if take["slot"] not in ("voice", "voice_sample"):
            raise AgentOSError(ErrorCode.INVALID, "只能把配音结果设为参考音")
        found = doc.project.line(take["target_id"])
        character_id = (found[1].character_id if found else take["target_id"]) or ""
        if found and not character_id:
            narrator = plan.speaker(doc.project, found[1])
            character_id = narrator.id if narrator else ""
        if not character_id or doc.project.character(character_id) is None:
            raise AgentOSError(ErrorCode.INVALID, "这句台词没有对应角色")
        upload = Upload(asset_id=take["asset_id"], slot="voice_ref", target_id=character_id, name="来自配音结果",
                        created_at=utc_now().isoformat(), meta={"from_take": take_id})
        return self._attach(actor, pid, upload, select=True)

    def upload(self, actor: AuthContext, pid: str, *, slot: str, target_id: str, data: bytes, mime: str,
               name: str) -> dict[str, Any]:
        doc, _revision, _stamp = self.load(actor, pid)
        if slot not in plan.UPLOAD_SLOTS or not plan.target_exists(doc.project, slot, target_id):
            raise AgentOSError(ErrorCode.INVALID, "上传位置不存在")
        if MEDIA_TYPES.get(mime) != plan.UPLOAD_SLOTS[slot]:
            raise AgentOSError(ErrorCode.INVALID, "文件类型不适合这个位置")
        registered = self.assets.register_local(actor, data, mime=mime, name=name[:120],
                                                meta={"kind": "studio_upload", "slot": slot}, source_node="upload")
        upload = Upload(asset_id=registered["asset_id"], slot=slot, target_id=target_id, name=name[:120],
                        created_at=utc_now().isoformat())
        return self._attach(actor, pid, upload, select=slot != "music")

    # ------------------------------------------------------------------ starting runs
    def _require_engine(self) -> None:
        if self.engine() is None:
            raise AgentOSError(ErrorCode.UNAVAILABLE, NO_ENGINE)

    def _require_node(self) -> None:
        if not self.node_id:
            raise AgentOSError(ErrorCode.UNAVAILABLE, "部署还没有指定生成节点，只能上传素材、写剧本和导出")

    def tool_node(self, index: int, spec: NodeSpec, request: dict[str, Any]) -> dict[str, Any]:
        return {"id": f"n{index:03d}", "kind": "tool", "label": spec.label,
                "config": {"tool": self.tool_name(spec.tool), "arguments": spec.arguments,
                           "studio": {"kind": spec.kind, "slot": spec.slot, "target": spec.target,
                                      "engine": spec.engine, "estimate": spec.estimate, "meta": spec.meta,
                                      "request": request}}}

    @staticmethod
    def custom_node(index: int, adapter: str, label: str, inputs: dict[str, Any], studio: dict[str, Any]
                    ) -> dict[str, Any]:
        return {"id": f"n{index:03d}", "kind": "custom", "label": label,
                "config": {"adapter": adapter, "inputs": inputs, "studio": studio}}

    async def launch(self, actor: AuthContext, project: Project, *, suffix: str, name: str,
                     nodes: list[dict[str, Any]], inputs: dict[str, Any], key: str) -> RunView:
        """Publish the request as an immutable workflow version and start it (nodes run in order)."""
        self._require_engine()
        for index, node in enumerate(nodes):
            node["depends_on"] = [nodes[index - 1]["id"]] if index else []
        graph = {"id": workflow_prefix(actor.subject, project.id) + suffix, "name": f"{project.title} · {name}",
                 "nodes": nodes, "input_schema": {}}
        try:
            bound = await self.catalog.bind_graph(actor, graph)
        except AgentOSError as exc:
            if not any(n["kind"] == "tool" for n in nodes):
                raise
            if exc.code == ErrorCode.UNAVAILABLE:
                raise AgentOSError(exc.code, "生成节点当前离线：确认节点已开机、节点服务在运行且网络可达") from exc
            if exc.code == ErrorCode.INVALID:
                raise AgentOSError(ErrorCode.UNAVAILABLE,
                                   "生成节点没有提供需要的工具：在节点上安装对应制作能力") from exc
            raise
        published = self.workflows.publish(actor, bound)
        started = self.workflows.start(actor, published["workflow_id"], published["version"],
                                       {"studio": {"project_id": project.id, **inputs}}, key)
        view = self.runs.view(actor.tenant_id, started["internal_run_id"], **self._context())
        if view is None:
            raise AgentOSError(ErrorCode.UNCERTAIN, "运行已创建但无法读取，请刷新")
        for item in view.nodes:
            self.publish(view.tenant, view.subject, {"type": "job", "job": self.job(view, item)})
        return view

    def specs(self, project: Project, picks: dict[str, Pick], items: list[tuple[str, str, str, dict[str, Any]]]
              ) -> tuple[list[NodeSpec], list[str]]:
        """Prepare (kind, target, engine, overrides) items; impossible ones become notes."""
        specs: list[NodeSpec] = []
        skipped: list[str] = []
        for kind, target, engine_id, overrides in items:
            try:
                chosen = plan.choose_engine(engine_id, plan.GENERATED[kind][1])
                seed = overrides.get("seed")
                seed = int(seed) if isinstance(seed, int) and seed >= 0 else random.randint(1, 2**31 - 1)
                specs.append(plan.prepare(project, kind, target, chosen, picks, seed=seed, overrides=overrides))
            except PlanError as exc:
                note = f"{plan.label(project, kind, target)}：{exc}"
                if note not in skipped:
                    skipped.append(note)
        return specs, skipped

    async def generate(self, actor: AuthContext, pid: str, body: dict[str, Any]) -> dict[str, Any]:
        kind = str(body.get("kind") or "")
        targets = [str(t) for t in body.get("target_ids") or []][:MAX_ITEMS]
        count = max(1, min(8, int(body.get("count") or 1)))
        overrides = {k: v for k, v in (body.get("overrides") or {}).items()
                     if k in ("seed", "framing", "prompt", "motion_prompt")}
        if kind not in plan.GENERATED:
            raise AgentOSError(ErrorCode.INVALID, "不支持的生成类型")
        if not targets:
            raise AgentOSError(ErrorCode.INVALID, "没有选择要生成的对象")
        self._require_node()
        self._require_engine()
        now = self.state(actor, pid)
        engine_kind = plan.GENERATED[kind][1]
        engine_id = str(body.get("engine") or getattr(now.doc.project.engines, engine_kind))
        try:
            plan.choose_engine(engine_id, engine_kind)
        except PlanError as exc:
            raise AgentOSError(ErrorCode.INVALID, str(exc)) from exc
        items = [(kind, target, engine_id, overrides) for target in targets for _ in range(count)][:MAX_ITEMS]
        return await self.request(actor, now.doc.project, now.picks, items)

    async def request(self, actor: AuthContext, project: Project, picks: dict[str, Pick],
                      items: list[tuple[str, str, str, dict[str, Any]]]) -> dict[str, Any]:
        specs, skipped = self.specs(project, picks, items)
        if not specs:
            raise AgentOSError(ErrorCode.INVALID, skipped[0] if skipped else "没有可以生成的内容")
        counts: dict[str, int] = {}
        for spec in specs:
            counts[spec.label] = counts.get(spec.label, 0) + 1
        seen: dict[str, int] = {}
        nodes = []
        for index, spec in enumerate(specs, start=1):
            if counts[spec.label] > 1:
                seen[spec.label] = seen.get(spec.label, 0) + 1
                spec.label = f"{spec.label} · 候选 {seen[spec.label]}"
            nodes.append(self.tool_node(index, spec, {"engine": spec.engine,
                                                      "overrides": spec.meta.get("overrides") or {}}))
        kinds = {spec.kind for spec in specs}
        name = (plan.label(project, specs[0].kind, specs[0].target) if len(specs) == 1
                else f"{len(specs)} 项" + "、".join(KIND_NAMES.get(plan.GENERATED[k][1], k) for k in sorted(kinds)))
        view = await self.launch(actor, project, suffix="gen", name=name, nodes=nodes,
                                 inputs={"request": "generate"}, key="studio:" + uuid.uuid4().hex)
        return {"jobs": [self.job(view, node) for node in view.nodes], "skipped": skipped}

    async def director_job(self, actor: AuthContext, pid: str, task: str, params: dict[str, Any]) -> dict[str, Any]:
        labels = {"script": "写剧本", "breakdown": "拆分镜", "prompts": "改写画面提示词"}
        if task not in labels:
            raise AgentOSError(ErrorCode.INVALID, "不支持的导演任务")
        doc, _revision, _stamp = self.load(actor, pid)
        if task == "breakdown" and not doc.project.script.strip():
            raise AgentOSError(ErrorCode.INVALID, "先写好剧本再拆分镜")
        if task == "prompts" and not doc.project.shots:
            raise AgentOSError(ErrorCode.INVALID, "还没有分镜")
        clean: dict[str, Any] = {k: params[k] for k in ("instructions", "replace", "shot_ids", "model") if k in params}
        if "instructions" in clean:
            clean["instructions"] = str(clean["instructions"])[:2000]
        view = await self.launch(actor, doc.project, suffix="director", name=labels[task],
                                 nodes=[self.director_node(1, doc.project, task, clean)],
                                 inputs={"request": "director"}, key="studio:" + uuid.uuid4().hex)
        return {"job": self.job(view, view.nodes[0])}

    def director_node(self, index: int, project: Project, task: str, params: dict[str, Any]) -> dict[str, Any]:
        labels = {"script": "写剧本", "breakdown": "拆分镜", "prompts": "改写画面提示词"}
        scenes = max(1, len(director.split_scenes(project.script)))
        estimate = {"script": 90, "breakdown": 60 + 40 * scenes, "prompts": 20 * max(1, len(project.shots))}[task]
        return self.custom_node(index, "studio_director", labels[task],
                                {"project_id": project.id, "task": task, **params},
                                {"kind": "director:" + task, "target": project.id, "estimate": estimate,
                                 "request": {"task": task, "params": params}})

    def render_node(self, index: int, project: Project, quality: str) -> dict[str, Any]:
        seconds = sum(s.duration for s in project.shots)
        return self.custom_node(index, "studio_render", "导出成片" if quality == "final" else "生成预览",
                                {"project_id": project.id, "quality": quality},
                                {"kind": "render", "slot": "render", "target": project.id,
                                 "estimate": round(20 + seconds * (4 if quality == "final" else 1)),
                                 "request": {"quality": quality}})

    async def render(self, actor: AuthContext, pid: str, quality: str) -> dict[str, Any]:
        if quality not in ("preview", "final"):
            raise AgentOSError(ErrorCode.INVALID, "未知的导出质量")
        doc, _revision, _stamp = self.load(actor, pid)
        if not doc.project.shots:
            raise AgentOSError(ErrorCode.INVALID, "还没有分镜，先写剧本并拆分镜")
        if not self.ffmpeg():
            raise AgentOSError(ErrorCode.UNAVAILABLE, NO_FFMPEG)
        node = self.render_node(1, doc.project, quality)
        view = await self.launch(actor, doc.project, suffix="render", name=str(node["label"]), nodes=[node],
                                 inputs={"request": "render"}, key="studio:" + uuid.uuid4().hex)
        return {"job": self.job(view, view.nodes[0])}

    # ------------------------------------------------------------------ one-click production
    async def autopilot(self, actor: AuthContext, pid: str, body: dict[str, Any]) -> dict[str, Any]:
        stages = [str(s) for s in body.get("stages") or plan.STAGES]
        if set(stages) - set(plan.STAGES):
            raise AgentOSError(ErrorCode.INVALID, "未知的制作阶段")
        quality = str(body.get("quality") or "preview")
        if quality not in ("preview", "final"):
            raise AgentOSError(ErrorCode.INVALID, "未知的导出质量")
        now = self.state(actor, pid)
        project, picks = now.doc.project, now.picks
        if "script" in stages and not project.script.strip() and not (project.premise.strip() or
                                                                        project.logline.strip()):
            raise AgentOSError(ErrorCode.INVALID, "先填写故事梗概或剧本")
        self._require_engine()
        expanded = [x for s in plan.STAGES if s in stages for x in (["frames", "end_frames"] if s == "frames" else [s])]
        pipeline = {"id": uuid.uuid4().hex[:12], "stages": expanded, "index": 0, "round": 0,
                    "quality": quality, "rebreak": bool(body.get("rebreak"))}
        view, notes, _stage = await self.advance(actor, pid, pipeline)
        if view is None:
            return {"jobs": [], "skipped": notes, "estimate": plan.estimate(project, picks),
                    "message": "没有需要制作的内容"}
        return {"jobs": [self.job(view, node) for node in view.nodes], "skipped": notes,
                "estimate": plan.estimate(project, picks)}

    async def advance(self, actor: AuthContext, pid: str, pipeline: dict[str, Any]
                      ) -> tuple[RunView | None, list[str], str]:
        """Start the next stage that still has work; stages with nothing to do are passed over."""
        stages = list(pipeline["stages"])
        index, rounds = int(pipeline["index"]), int(pipeline.get("round", 0))
        notes: list[str] = []
        while index < len(stages):
            stage = stages[index]
            key = f"studio:{pipeline['id']}:{index}:{rounds}"
            prior = self.store.find_idempotent_receipt(actor.tenant_id, actor.subject, "workflow", key)
            if prior is not None:   # this continuation already ran (e.g. before a restart)
                run_id = json.loads(prior[1])["run_id"]
                return self.runs.view(actor.tenant_id, run_id, **self._context()), notes, stage
            if rounds >= MAX_ROUNDS:
                raise AgentOSError(ErrorCode.INVALID, f"{plan.STAGE_LABELS[stage]}反复没有完成，一键制作已停止")
            now = self.state(actor, pid)
            doc, picks = now.doc, now.picks
            nodes, skipped, truncated = self.stage_nodes(doc.project, picks, stage, pipeline)
            notes += [n for n in skipped if n not in notes]
            if not nodes:
                index, rounds = index + 1, 0
                continue
            following = {**pipeline, "index": index if truncated else index + 1,
                         "round": rounds + 1 if truncated else 0, "rebreak": False}
            if following["index"] < len(stages):
                upcoming = stages[following["index"]]
                nodes.append(self.custom_node(len(nodes) + 1, "studio_next", "一键制作：继续下一步",
                                              {"project_id": pid, "pipeline": following},
                                              {"kind": "stage:" + upcoming, "target": pid, "estimate": 1,
                                               "request": {"pipeline": following}}))
            view = await self.launch(actor, doc.project, suffix="auto", name="一键制作：" + plan.STAGE_LABELS[stage],
                                     nodes=nodes, inputs={"request": "autopilot", "stage": stage,
                                                          "pipeline": pipeline["id"]}, key=key)
            return view, notes, stage
        return None, notes, ""

    def stage_nodes(self, project: Project, picks: dict[str, Pick], stage: str, pipeline: dict[str, Any]
                    ) -> tuple[list[dict[str, Any]], list[str], bool]:
        if stage == "script":
            return ([] if project.script.strip() else [self.director_node(1, project, "script", {})]), [], False
        if stage == "breakdown":
            if project.shots and not pipeline.get("rebreak"):
                return [], [], False
            if not project.script.strip():
                return [], ["没有剧本，跳过拆分镜"], False
            return [self.director_node(1, project, "breakdown", {"replace": True})], [], False
        if stage == "render":
            if not project.shots:
                return [], ["没有分镜，跳过导出"], False
            if not self.ffmpeg():
                return [], ["控制面没有 FFmpeg，跳过导出"], False
            return [self.render_node(1, project, str(pipeline.get("quality") or "preview"))], [], False
        work, skipped = plan.stage_work(project, stage, picks)
        if not work:
            return [], skipped, False
        self._require_node()
        items: list[tuple[str, str, str, dict[str, Any]]] = [
            (kind, target, plan.stage_engine(project, kind, target).id, {}) for kind, target in work]
        specs, notes = self.specs(project, picks, items)
        truncated = len(specs) > MAX_ITEMS
        nodes = [self.tool_node(i, spec, {"engine": spec.engine, "overrides": {}})
                 for i, spec in enumerate(specs[:MAX_ITEMS], start=1)]
        return nodes, skipped + notes, truncated

    # ------------------------------------------------------------------ jobs
    @staticmethod
    def job(view: RunView, node: NodeView) -> dict[str, Any]:
        result = dict(node.output or {}) if node.state == "succeeded" else {}
        return {"id": node.id, "run_id": view.run_id, "project_id": view.project_id, "kind": node.kind,
                "target_id": node.target, "engine": node.engine, "label": node.label, "params": {},
                "gpu": node.is_tool, "state": node.state, "progress": dict(node.meta.get("progress") or {}),
                "result": result, "error": node.error, "attempt": 1, "estimate_seconds": node.estimate,
                "created_at": view.created_at, "started_at": node.started_at, "finished_at": node.finished_at,
                "approval_id": node.approval_id, "step_id": node.step_id, "batch_size": len(view.nodes)}

    def jobs_of(self, views: list[RunView], *, limit: int = 200) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        for view in views:   # newest run first, its nodes in order
            jobs += [self.job(view, node) for node in view.nodes]
            if len(jobs) >= limit and view.settled:
                break
        return jobs

    def active_jobs(self, actor: AuthContext) -> list[dict[str, Any]]:
        views = [v for v in list(self.runs.running.values()) if v.tenant == actor.tenant_id
                 and v.subject == actor.subject]
        views.sort(key=lambda v: v.created_at)
        return [self.job(v, n) for v in views for n in v.nodes
                if n.state in ("queued", "running", "approval", "blocked")]

    def queue(self, actor: AuthContext) -> dict[str, Any]:
        active = self.active_jobs(actor)
        running = [j for j in active if j["state"] == "running"]
        remaining = sum(j["estimate_seconds"] for j in active if j["state"] == "queued")
        remaining += sum(j["estimate_seconds"] / 2 for j in running)
        return {"queued": sum(1 for j in active if j["state"] == "queued"), "running": running,
                "waiting": sum(1 for j in active if j["state"] in ("approval", "blocked")),
                "eta_seconds": round(remaining), "gpu": self.accelerator.snapshot()}

    def _own_node(self, actor: AuthContext, job_id: str) -> tuple[RunView, NodeView]:
        run_id, _, node_id = job_id.partition(".")
        view = self.runs.view(actor.tenant_id, run_id, **self._context()) if run_id else None
        if view is None or view.subject != actor.subject:
            raise AgentOSError(ErrorCode.INVALID, "任务不存在")
        node = next((n for n in view.nodes if n.node_id == node_id), None)
        if node is None:
            raise AgentOSError(ErrorCode.INVALID, "任务不存在")
        return view, node

    def cancel(self, actor: AuthContext, job_id: str) -> dict[str, Any]:
        """Cancels the request the job belongs to: finished candidates stay, the rest stop."""
        view, _node = self._own_node(actor, job_id)
        self.access.require(actor, "task.cancel", kind="task", resource_id=view.run_id)
        updated = self.store.request_cancel(actor.tenant_id, view.run_id)
        return {"run_id": view.run_id, "state": updated.state.value if updated else "cancel_requested"}

    async def retry(self, actor: AuthContext, job_id: str, *, batch: bool = False) -> dict[str, Any]:
        view, node = self._own_node(actor, job_id)
        now = self.state(actor, view.project_id)
        doc, picks = now.doc, now.picks
        if node.is_tool:
            nodes = [n for n in view.nodes if n.is_tool and n.state in ("failed", "cancelled", "skipped")] \
                if batch else [node]
            items = [(n.kind, n.target, str(n.request.get("engine") or n.engine),
                      dict(n.request.get("overrides") or {}))
                     for n in nodes if plan.target_exists(doc.project, "char_ref" if n.slot == "voice_sample"
                                                          else n.slot, n.target)]
            if not items:
                raise AgentOSError(ErrorCode.INVALID, "原来的生成对象已经不存在")
            return await self.request(actor, doc.project, picks, items)
        if node.kind.startswith("director:"):
            result = await self.director_job(actor, view.project_id, node.kind.split(":", 1)[1],
                                             dict(node.request.get("params") or {}))
            return {"jobs": [result["job"]]}
        if node.kind == "render":
            result = await self.render(actor, view.project_id, str(node.request.get("quality") or "preview"))
            return {"jobs": [result["job"]]}
        if node.kind.startswith("stage:"):
            pipeline = {**dict(node.request.get("pipeline") or {}), "id": uuid.uuid4().hex[:12], "round": 0}
            started, notes, _stage = await self.advance(actor, view.project_id, pipeline)
            return {"jobs": [self.job(started, n) for n in started.nodes] if started else [], "skipped": notes}
        raise AgentOSError(ErrorCode.INVALID, "这个任务不能重试")

    # ------------------------------------------------------------------ live events
    def subscribe(self, actor: AuthContext) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        self.subscribers.append((actor.tenant_id, actor.subject, queue))
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self.subscribers = [s for s in self.subscribers if s[2] is not queue]

    def publish(self, tenant: str, subject: str, event: dict[str, Any]) -> None:
        for owner_tenant, owner_subject, queue in list(self.subscribers):
            if owner_tenant == tenant and owner_subject == subject:
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(event)

    def report(self, job_id: str, **fields: Any) -> None:
        entry = self.progress.setdefault(job_id, {"started_at": utc_now().isoformat()})
        entry.update(fields)

    async def start(self) -> None:
        if self._watcher is None:
            self._watcher = asyncio.create_task(self._watch(), name="studio-watch")

    async def stop(self) -> None:
        if self._watcher is not None:
            self._watcher.cancel()
            await asyncio.gather(self._watcher, return_exceptions=True)
            self._watcher = None

    def _seed(self) -> None:
        name = self.engine_name()
        if not name:
            return
        for run in self.store.list_engine_runs(name):
            if (run.tenant_id, run.id) not in self.runs.running:
                self.runs.view(run.tenant_id, run.id, **self._context())

    async def _watch(self) -> None:
        """Turn run progress into job/take events for open studio pages."""
        last_seed = last_gpu = 0.0
        while True:
            await asyncio.sleep(1.0)
            try:
                now = time.monotonic()
                if now - last_seed > 30:
                    last_seed = now
                    self._seed()
                if not self.subscribers:
                    continue
                context = self._context()
                for (tenant, run_id), before in list(self.runs.running.items()):
                    after = self.runs.view(tenant, run_id, **context)
                    if after is None:
                        self.runs.running.pop((tenant, run_id), None)
                        continue
                    self._publish_changes(before, after)
                if now - last_gpu > 15:
                    last_gpu = now
                    for tenant, subject, _queue in list(self.subscribers):
                        self.publish(tenant, subject, {"type": "gpu", "gpu": context["accelerator"]})
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep the live view going; the next tick retries
                log.warning("studio live update failed", exc_info=True)

    def _publish_changes(self, before: RunView, after: RunView) -> None:
        previous = {n.node_id: n for n in before.nodes}
        for node in after.nodes:
            old = previous.get(node.node_id)
            if old is not None and (old.state, old.error, old.meta.get("progress")) == \
                    (node.state, node.error, node.meta.get("progress")):
                continue
            self.publish(after.tenant, after.subject, {"type": "job", "job": self.job(after, node)})
            if node.state == "succeeded" and node.slot and (node.output or {}).get("asset_id"):
                self.publish(after.tenant, after.subject, {"type": "take", "selected": False, "take": {
                    "id": node.id, "project_id": after.project_id, "slot": node.slot, "target_id": node.target,
                    "asset_id": node.output["asset_id"] if node.output else ""}})

    # ------------------------------------------------------------------ node, engines, permissions
    async def node_tools(self, actor: AuthContext, *, refresh: bool = False) -> tuple[list[dict[str, Any]], str]:
        """Live node tool list (cached 30 s); falls back to the last advertised snapshot."""
        if not self.node_id:
            return [], "unconfigured"
        if self._node_cache and not refresh and time.monotonic() - self._node_cache[0] < 30:
            return self._node_cache[1], self._node_cache[2]
        row = self.store.get_node(actor.tenant_id, self.node_id)
        if row is None or row.get("status") != "paired":
            result: tuple[list[dict[str, Any]], str] = ([], "unpaired")
        else:
            try:
                await asyncio.wait_for(self.tool_hub.refresh_node_tools(actor, self.node_id), timeout=4)
                fresh = self.store.get_node(actor.tenant_id, self.node_id) or row
                result = (json.loads(fresh.get("capabilities_json") or "[]"), "online")
            except (AgentOSError, TimeoutError):
                result = (json.loads(row.get("capabilities_json") or "[]"), "offline")
        self._node_cache = (time.monotonic(), result[0], result[1])
        return result

    def permissions(self, actor: AuthContext, tools: list[dict[str, Any]]) -> dict[str, Any]:
        offered = [t for t in NODE_TOOLS if any(str(c.get("name")) == t for c in tools)]
        entries = []
        for tool in offered:
            name = self.tool_name(tool)
            entries.append({"tool": name, "label": {"aigc.image": "图像", "aigc.generate": "视频",
                                                    "aigc.speak": "配音", "aigc.lipsync": "口型"}[tool],
                            "grant": self.grants.active(actor, name) if self.grants else None})
        return {"tools": entries, "needs_approval": any(e["grant"] is None for e in entries),
                "can_grant": bool(self.grants) and "admin" in actor.roles and actor.has_scope("approve")}

    async def grant(self, actor: AuthContext, hours: int) -> dict[str, Any]:
        if self.grants is None:
            raise AgentOSError(ErrorCode.UNSUPPORTED, "这个部署不支持预授权")
        tools, _state = await self.node_tools(actor, refresh=True)
        offered = [t for t in NODE_TOOLS if any(str(c.get("name")) == t for c in tools)]
        if not offered:
            raise AgentOSError(ErrorCode.UNAVAILABLE, "生成节点当前没有提供制作工具")
        issued = [await self.grants.issue(actor, self.tool_name(tool), hours) for tool in offered]
        return {"grants": issued}

    async def overview(self, actor: AuthContext) -> dict[str, Any]:
        tools, state = await self.node_tools(actor)
        status = availability(tools, node_state=state)
        row = (self.store.get_node(actor.tenant_id, self.node_id) if self.node_id else None) or {}
        offered = sorted(str(t.get("name")) for t in tools if str(t.get("name", "")).startswith("aigc."))
        return {"engines": [e.public(*status.get(e.id, (False, ""))) for e in ENGINES],
                "styles": STYLES, "emotions": EMOTIONS,
                "node": {"id": self.node_id, "state": state, "os": row.get("os", ""),
                         "last_seen": row.get("last_heartbeat") or "", "tools": offered},
                "workflow": {"available": self.engine() is not None, "engine": self.engine_name(),
                             "message": "" if self.engine() is not None else NO_ENGINE},
                "permissions": self.permissions(actor, tools),
                "ffmpeg": bool(self.ffmpeg()), "queue": self.queue(actor)}

    # ------------------------------------------------------------------ media
    def sign(self, actor: AuthContext, asset_ids: list[str], width: int) -> dict[str, dict[str, str]]:
        """Short-lived media links for assets the caller can read (library previews)."""
        has_ffmpeg = bool(self.ffmpeg())
        urls: dict[str, dict[str, str]] = {}
        for asset_id in asset_ids[:300]:
            try:
                meta = self.assets.get(actor, asset_id)
            except AgentOSError:
                continue
            mime = str(meta.get("mime") or "")
            owner = str(meta.get("subject") or actor.subject)
            entry = {"url": self.signer.url(asset_id, actor.tenant_id, owner)}
            if width and (mime.startswith("image/") or (mime.startswith("video/") and has_ffmpeg)):
                entry["thumb"] = self.signer.url(asset_id, actor.tenant_id, owner, width=width)
            urls[asset_id] = entry
        return urls

    def media_file(self, asset_id: str, tenant: str, expires: int, signature: str, *, width: int = 0,
                   variant: str = "") -> tuple[Path, str]:
        """Resolve a signed link to the file to send (or a cached derivative of it)."""
        row = self.store.get_asset(tenant, asset_id)
        if row is None or variant not in VARIANTS or not self.signer.verify(
                asset_id, tenant, str(row.get("subject") or ""), expires, signature):
            raise AgentOSError(ErrorCode.DENIED, "媒体链接无效或已过期")
        path, mime = Path(str(row["path"])), str(row.get("mime") or "application/octet-stream")
        if not path.is_file():
            raise AgentOSError(ErrorCode.UNAVAILABLE, "媒体文件不存在")
        ffmpeg = self.ffmpeg() or ""
        derived = self.cache_dir / "derived"
        try:
            if variant == "preview" and mime.startswith("video/") and ffmpeg:
                return h264_preview(path, derived, asset_id, ffmpeg), "video/mp4"
            if variant == "poster" and mime.startswith("video/") and ffmpeg:
                return thumbnail(path, derived, asset_id, 1280, ffmpeg=ffmpeg, video=True), "image/jpeg"
            if width and mime.startswith("image/"):
                return thumbnail(path, derived, asset_id, width), "image/jpeg"
            if width and mime.startswith("video/") and ffmpeg:
                return thumbnail(path, derived, asset_id, width, ffmpeg=ffmpeg, video=True), "image/jpeg"
        except (OSError, ValueError) as exc:
            log.info("media derivative unavailable for %s: %s", asset_id, exc)
        except Exception:  # noqa: BLE001 - a broken derivative falls back to the original
            log.warning("media derivative failed for %s", asset_id, exc_info=True)
        return path, mime
