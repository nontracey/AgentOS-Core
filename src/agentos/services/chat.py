"""Direct chat: conversations, context window, live generations and presets.

A generation runs as a background task. Browsers subscribe to it; closing the page does
not stop the answer, and reopening the conversation re-attaches to the live stream.

Governance: every endpoint declares where prompts go (``egress`` local or cloud) and the
deployment's ``model_egress`` decides whether cloud endpoints may be used at all. Under local
egress a deployment may still open cloud endpoints for named purposes (``cloud_purposes``,
for example only the studio director); every other purpose stays local. Cloud models are only
used when someone chose them (a model reference or the deployment default), never picked
implicitly. Each generation is audited with purpose, endpoint, egress, model, token counts and
outcome, never with content.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import math
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentos.adapters.model.chat_endpoints import (
    ChatEndpoint,
    EndpointError,
    ModelInfo,
    complete,
    list_models,
    stream_chat,
    unload_all,
)
from agentos.adapters.store.chat_sqlite import ChatStore
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import AuthContext
from agentos.services.accelerator import AcceleratorLease

AuditHook = Callable[[AuthContext, str, dict[str, Any]], None]

BUILTIN_PRESETS: list[dict[str, Any]] = [
    {"id": "assistant", "name": "通用助手", "description": "日常问答、写作、分析",
     "system_prompt": (
         "你是运行在用户自己电脑上的 AI 助手。用准确、自然的中文回答，先给结论，再给必要的理由或步骤。"
         "不确定的地方直接说明，不编造事实、数字或链接。代码放在带语言标注的 Markdown 代码块里。")},
    {"id": "screenwriter", "name": "短剧编剧", "description": "竖屏漫剧/短剧剧本、台词、反转",
     "system_prompt": (
         "你是擅长竖屏漫剧和短剧的编剧。写作要求：开头三秒抛出冲突或悬念；每场戏只推进一个目标；"
         "每 30–60 秒安排一次反转或情绪落差；结尾留钩子。台词口语化，每句不超过 20 个字，"
         "不同角色说话方式要有区分；动作和画面描写写成看得见、能拍出来的具体动作，不写心理独白。"
         "输出格式：场景标题（地点·时间），然后逐行写「角色：台词」和「【画面】具体动作」。")},
    {"id": "storyboard", "name": "分镜师", "description": "把剧本拆成镜头表",
     "system_prompt": (
         "你是动画分镜师。把剧本拆成镜头：每个镜头写景别（远景/全景/中景/近景/特写）、机位与运镜、"
         "画面里正在发生的具体动作、这一镜对应的台词与说话人、建议时长（秒）。"
         "画面必须表现这一刻的剧情和台词，不能用同一构图反复说话；对话戏要交替使用正反打和反应镜头，"
         "景别要有变化。一个镜头只承载一个清晰的动作。")},
    {"id": "image-prompt", "name": "画面提示词", "description": "给生图/图生视频模型写提示词",
     "system_prompt": (
         "你为本地图像和视频模型（FLUX.2 Klein、Qwen-Image-Edit、Wan2.2）写英文提示词。"
         "图像提示词顺序：主体与身份、外貌与服装、动作与表情、构图与景别、镜头与光线、画风与质感；"
         "多参考图时写明每张参考提供什么（身份、服装、场景、画风）。视频提示词只描述这几秒内的运动、"
         "镜头运动和速度，不重复静态外貌。避免互相矛盾的词，不堆砌质量词。")},
    {"id": "coder", "name": "代码助手", "description": "读代码、写代码、排错",
     "system_prompt": (
         "你是资深软件工程师。回答先给能直接运行的代码或修改，再简要解释原因。"
         "修改已有代码时只给需要改动的部分并说明位置。指出潜在的边界条件与风险。")},
    {"id": "translator", "name": "翻译", "description": "中英日互译，保留语气",
     "system_prompt": (
         "你是专业译者。识别原文语言后翻译成用户需要的语言（默认中英互译），保留语气、术语和格式，"
         "只输出译文；有歧义时在译文后用一行括号说明。")},
]

DEFAULT_PARAMS: dict[str, Any] = {"temperature": 0.7, "top_p": 0.9, "num_ctx": 8192, "think": "auto",
                                  "keep_alive": "10m"}
# Display names of model purposes; unknown purposes are shown as they are.
PURPOSE_LABELS = {"chat": "对话", "studio.director": "工作室导演"}
CLOUD_CLOSED = "出口策略只允许本地模型，这个云端端点已停用"
TEXT_ATTACHMENT_LIMIT = 200_000


def estimate_tokens(text: str) -> int:
    cjk = sum(1 for ch in text if "　" <= ch <= "鿿" or "豈" <= ch <= "﫿")
    return math.ceil(cjk * 0.8 + (len(text) - cjk) / 3.6) + 4


def title_from(text: str) -> str:
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "新对话")
    line = re.sub(r"[#>*`_\[\]]+", "", line).strip() or "新对话"
    return line[:28] + ("…" if len(line) > 28 else "")


@dataclass
class LiveGeneration:
    conversation_id: str
    message_id: str
    model: str
    content: str = ""
    thinking: str = ""
    status: str = "pending"
    status_detail: dict[str, Any] = field(default_factory=dict)
    subscribers: list[asyncio.Queue[dict[str, Any]]] = field(default_factory=list)
    task: asyncio.Task[None] | None = None
    started_at: float = field(default_factory=time.monotonic)
    last_flush: float = 0.0

    def publish(self, event: dict[str, Any]) -> None:
        for queue in list(self.subscribers):
            queue.put_nowait(event)

    def snapshot(self) -> dict[str, Any]:
        return {"type": "snapshot", "assistant_message_id": self.message_id, "content": self.content,
                "thinking": self.thinking, "status": self.status, "detail": self.status_detail,
                "model": self.model}


class ChatService:
    def __init__(self, store: ChatStore, endpoints: list[ChatEndpoint], *, lease: AcceleratorLease,
                 egress_policy: str = "local", audit: AuditHook | None = None, default_model: str = "",
                 read_asset: Callable[[AuthContext, str], tuple[dict[str, Any], bytes]] | None = None,
                 cloud_purposes: list[str] | None = None) -> None:
        self.store = store
        self.endpoints = [e for e in endpoints if e.enabled]
        self.lease = lease
        self.egress_policy = egress_policy
        self.audit = audit
        self.default_model = default_model
        self.read_asset = read_asset
        # Purposes that may use cloud endpoints although the deployment's egress is local.
        self.cloud_purposes = sorted(set(cloud_purposes or [])) if egress_policy != "cloud" else []
        # Endpoints the egress boundary closes for every purpose; listed, never called.
        self.blocked = {e.id: CLOUD_CLOSED for e in self.endpoints
                        if e.egress == "cloud" and egress_policy != "cloud" and not self.cloud_purposes}
        self.live: dict[str, LiveGeneration] = {}
        self._models: tuple[float, list[ModelInfo], dict[str, str]] | None = None
        self._models_lock = asyncio.Lock()
        self.store.mark_interrupted()

    def gpu_pressure(self) -> bool:
        """A generation job is waiting for the shared card: answer, then let the model unload."""
        return any(ticket["kind"] != "chat" for ticket in self.lease.waiting)

    # ------------------------------------------------------------ discovery
    def refusal(self, endpoint: ChatEndpoint, purpose: str) -> str:
        """Why prompts for ``purpose`` may not go to ``endpoint``; empty when they may."""
        if endpoint.id in self.blocked:
            return self.blocked[endpoint.id]
        if endpoint.egress != "cloud" or self.egress_policy == "cloud" or purpose in self.cloud_purposes:
            return ""
        names = "、".join(PURPOSE_LABELS.get(p, p) for p in self.cloud_purposes)
        return f"出口策略只允许本地模型；云端端点只用于{names}"

    def open_for(self, endpoint: ChatEndpoint) -> list[str] | None:
        """Purposes a cloud endpoint is limited to under local egress; None when not limited."""
        if endpoint.egress == "cloud" and self.egress_policy != "cloud" and endpoint.id not in self.blocked:
            return list(self.cloud_purposes)
        return None

    def endpoint(self, endpoint_id: str, purpose: str = "chat") -> ChatEndpoint:
        for endpoint in self.endpoints:
            if endpoint.id == endpoint_id:
                reason = self.refusal(endpoint, purpose)
                if reason:
                    raise AgentOSError(ErrorCode.DENIED, reason)
                return endpoint
        raise AgentOSError(ErrorCode.INVALID, f"未配置的模型端点：{endpoint_id}")

    def _find(self, endpoint_id: str) -> ChatEndpoint | None:
        return next((e for e in self.endpoints if e.id == endpoint_id), None)

    async def models(self, *, refresh: bool = False) -> tuple[list[ModelInfo], dict[str, str]]:
        """All models from reachable, allowed endpoints plus a per-endpoint error map (cached 20 s)."""
        async with self._models_lock:
            if self._models and not refresh and time.monotonic() - self._models[0] < 20:
                return self._models[1], self._models[2]
            allowed = [e for e in self.endpoints if e.id not in self.blocked]
            results = await asyncio.gather(*(list_models(e) for e in allowed), return_exceptions=True)
            models: list[ModelInfo] = []
            errors: dict[str, str] = dict(self.blocked)
            for endpoint, result in zip(allowed, results, strict=True):
                if isinstance(result, BaseException):
                    errors[endpoint.id] = str(result) if isinstance(result, EndpointError) else "端点不可用"
                else:
                    models.extend(result)
            self._models = (time.monotonic(), models, errors)
            return models, errors

    async def models_for(self, purpose: str, *, refresh: bool = False) -> tuple[list[ModelInfo], dict[str, str]]:
        """Models usable for ``purpose`` plus why the other endpoints are not (offline or refused)."""
        models, errors = await self.models(refresh=refresh)
        refused = {e.id: reason for e in self.endpoints if (reason := self.refusal(e, purpose))}
        return [m for m in models if m.endpoint not in refused], {**errors, **refused}

    def _audit(self, actor: AuthContext | None, endpoint: ChatEndpoint, model: str, *, purpose: str,
               status: str, stats: dict[str, Any] | None = None) -> None:
        if self.audit is None or actor is None:
            return
        stats = stats or {}
        self.audit(actor, "model.chat", {
            "purpose": purpose, "endpoint": endpoint.id, "model": model, "egress": endpoint.egress,
            "status": status, "input_tokens": int(stats.get("input_tokens") or 0),
            "output_tokens": int(stats.get("output_tokens") or 0),
            "wall_seconds": float(stats.get("wall_seconds") or 0)})

    @staticmethod
    def chat_capable(model: ModelInfo) -> bool:
        caps = set(model.capabilities)
        return "completion" in caps or "embedding" not in caps

    async def resolve_model(self, ref: str = "", purpose: str = "chat") -> tuple[ChatEndpoint, str, ModelInfo | None]:
        """Pick endpoint/model for ``purpose``: an explicit reference, the deployment default, or a local model.

        An explicit reference the egress policy refuses for this purpose is an error. A refused
        deployment default is skipped, and the implicit pick only considers local endpoints
        (any endpoint when the whole deployment allows cloud egress).
        """
        models, errors = await self.models()
        for wanted, explicit in ((ref, True), (self.default_model, False)):
            if not wanted or "/" not in wanted:
                continue
            endpoint_id, name = wanted.split("/", 1)
            found = self._find(endpoint_id)
            if found is None:
                raise AgentOSError(ErrorCode.INVALID, f"未配置的模型端点：{endpoint_id}")
            reason = self.refusal(found, purpose)
            if reason:
                if explicit:
                    raise AgentOSError(ErrorCode.DENIED, reason)
                continue
            if not found.offers(name):
                raise AgentOSError(ErrorCode.DENIED, f"{found.label or found.id} 不提供模型 {name}")
            info = next((m for m in models if m.endpoint == endpoint_id and m.name == name), None)
            if info is None and endpoint_id in errors:
                raise AgentOSError(ErrorCode.UNAVAILABLE, errors[endpoint_id])
            return found, name, info
        local = {e.id for e in self.endpoints
                 if not self.refusal(e, purpose) and (e.egress == "local" or self.egress_policy == "cloud")}
        candidates = [m for m in models if self.chat_capable(m) and m.endpoint in local]
        if not candidates:
            if errors:
                raise AgentOSError(ErrorCode.UNAVAILABLE, "没有可用的模型端点：" + "；".join(errors.values()))
            raise AgentOSError(ErrorCode.UNAVAILABLE, "还没有配置可用的本地模型")
        loaded = [m for m in candidates if m.loaded]
        chosen = (loaded or candidates)[0]
        return self.endpoint(chosen.endpoint, purpose), chosen.name, chosen

    async def status(self) -> dict[str, Any]:
        models, errors = await self.models()
        endpoints = []
        for endpoint in self.endpoints:
            own = [m for m in models if m.endpoint == endpoint.id]
            endpoints.append({**endpoint.public(), "online": endpoint.id not in errors,
                              "blocked": endpoint.id in self.blocked, "open_for": self.open_for(endpoint),
                              "error": errors.get(endpoint.id, ""), "model_count": len(own),
                              "loaded": [m.public() for m in own if m.loaded]})
        return {"endpoints": endpoints, "gpu": self.lease.snapshot(),
                "live": [{"conversation_id": g.conversation_id, "status": g.status} for g in self.live.values()]}

    async def release_gpu(self) -> list[str]:
        """Unload chat models on shared-card endpoints (accelerator hook before generation)."""
        released: list[str] = []
        for endpoint in self.endpoints:
            if endpoint.gpu_shared and endpoint.id not in self.blocked:
                with contextlib.suppress(EndpointError):
                    released += await unload_all(endpoint)
        if released:
            self._models = None
        return released

    # ------------------------------------------------------------ presets
    def presets(self, actor: AuthContext) -> list[dict[str, Any]]:
        return ([{**p, "builtin": True} for p in BUILTIN_PRESETS]
                + self.store.list_presets(actor.tenant_id, actor.subject))

    def preset_prompt(self, actor: AuthContext, preset_id: str) -> str:
        for preset in self.presets(actor):
            if preset["id"] == preset_id:
                return str(preset["system_prompt"])
        return ""

    # ------------------------------------------------------------ conversations
    def conversation(self, actor: AuthContext, cid: str) -> dict[str, Any]:
        found = self.store.get_conversation(actor.tenant_id, actor.subject, cid)
        if found is None:
            raise AgentOSError(ErrorCode.INVALID, "对话不存在")
        return found

    def create(self, actor: AuthContext, body: dict[str, Any]) -> dict[str, Any]:
        preset = str(body.get("preset") or "assistant")
        params = {**DEFAULT_PARAMS, **{k: v for k, v in (body.get("params") or {}).items() if k in DEFAULT_PARAMS}}
        return self.store.create_conversation(
            actor.tenant_id, actor.subject, title=str(body.get("title") or "")[:120],
            model=str(body.get("model") or ""), preset=preset,
            system_prompt=str(body.get("system_prompt") or ""), params=params)

    def update(self, actor: AuthContext, cid: str, body: dict[str, Any]) -> dict[str, Any]:
        current = self.conversation(actor, cid)
        fields: dict[str, Any] = {}
        for key in ("title", "model", "preset", "system_prompt", "pinned", "archived"):
            if key in body:
                fields[key] = body[key]
        if "params" in body and isinstance(body["params"], dict):
            fields["params"] = {**current["params"],
                                **{k: v for k, v in body["params"].items() if k in DEFAULT_PARAMS}}
        updated = self.store.update_conversation(actor.tenant_id, actor.subject, cid, **fields)
        assert updated is not None
        return updated

    def detail(self, actor: AuthContext, cid: str) -> dict[str, Any]:
        conversation = self.conversation(actor, cid)
        live = self.live.get(cid)
        return {**conversation, "messages": self.store.list_messages(cid),
                "live": live.snapshot() if live else None}

    async def delete(self, actor: AuthContext, cid: str) -> None:
        self.conversation(actor, cid)
        await self.stop(actor, cid)
        self.store.delete_conversation(actor.tenant_id, actor.subject, cid)

    # ------------------------------------------------------------ context
    def _system_prompt(self, actor: AuthContext, conversation: dict[str, Any]) -> str:
        custom = str(conversation.get("system_prompt") or "").strip()
        base = custom or self.preset_prompt(actor, str(conversation.get("preset") or "assistant"))
        stamp = time.strftime("%Y-%m-%d %H:%M")
        return f"{base}\n\n当前时间：{stamp}。".strip()

    def _attachment_payload(self, actor: AuthContext, attachments: list[dict[str, Any]],
                            vision: bool) -> tuple[str, list[str], list[str]]:
        text_parts: list[str] = []
        images: list[str] = []
        notes: list[str] = []
        if self.read_asset is None:
            return "", [], []
        for item in attachments:
            asset_id = str(item.get("asset_id") or "")
            if not asset_id:
                continue
            try:
                meta, data = self.read_asset(actor, asset_id)
            except AgentOSError:
                notes.append(f"附件 {item.get('name') or asset_id} 已不可用")
                continue
            mime = str(meta.get("mime") or "")
            name = str(item.get("name") or meta.get("name") or asset_id)
            if mime.startswith("image/"):
                if vision:
                    images.append(base64.b64encode(data).decode())
                else:
                    notes.append(f"图片 {name} 没有发送：当前模型不支持看图")
            else:
                text = data.decode("utf-8", "replace")[:TEXT_ATTACHMENT_LIMIT]
                text_parts.append(f"\n\n附件《{name}》：\n```\n{text}\n```")
        return "".join(text_parts), images, notes

    def build_messages(self, actor: AuthContext, conversation: dict[str, Any], history: list[dict[str, Any]],
                       *, vision: bool, num_ctx: int) -> tuple[list[dict[str, Any]], list[str]]:
        system = self._system_prompt(actor, conversation)
        budget = max(1024, num_ctx - 1536 - estimate_tokens(system))
        chosen: list[dict[str, Any]] = []
        notes: list[str] = []
        used = 0
        # Walk back from the newest message; the newest user turn is always kept.
        for index, message in enumerate(reversed(history)):
            if message["role"] not in ("user", "assistant"):
                continue
            if message["role"] == "assistant" and message["status"] not in ("complete", "stopped"):
                continue
            content = str(message["content"] or "")
            images: list[str] = []
            if message["role"] == "user" and message.get("attachments"):
                extra, images, item_notes = self._attachment_payload(actor, message["attachments"], vision)
                content += extra
                if index == 0:
                    notes += item_notes
            cost = estimate_tokens(content) + 800 * len(images)
            if chosen and used + cost > budget:
                notes.append("较早的对话超出上下文长度，已省略")
                break
            used += cost
            entry: dict[str, Any] = {"role": message["role"], "content": content}
            if images:
                entry["images"] = images
            chosen.append(entry)
        chosen.reverse()
        return [{"role": "system", "content": system}, *chosen], notes

    # ------------------------------------------------------------ generation
    async def send(self, actor: AuthContext, cid: str, body: dict[str, Any]
                   ) -> tuple[LiveGeneration, asyncio.Queue[dict[str, Any]]]:
        conversation = self.conversation(actor, cid)
        if cid in self.live:
            raise AgentOSError(ErrorCode.CONFLICT, "这条对话还在生成中")
        history = self.store.list_messages(cid)
        content = str(body.get("content") or "").strip()
        attachments = [a for a in body.get("attachments") or [] if isinstance(a, dict) and a.get("asset_id")]
        if body.get("regenerate"):
            last_user = next((m for m in reversed(history) if m["role"] == "user"), None)
            if last_user is None:
                raise AgentOSError(ErrorCode.INVALID, "没有可以重新生成的提问")
            self.store.supersede_from(cid, int(last_user["seq"]) + 1)
            user_message = last_user
        elif body.get("edit_message_id"):
            original = next((m for m in history if m["id"] == body["edit_message_id"] and m["role"] == "user"), None)
            if original is None or not content:
                raise AgentOSError(ErrorCode.INVALID, "找不到要修改的提问")
            self.store.supersede_from(cid, int(original["seq"]))
            user_message = self.store.add_message(cid, "user", content=content,
                                                  attachments=attachments or original["attachments"])
        else:
            if not content and not attachments:
                raise AgentOSError(ErrorCode.INVALID, "消息不能为空")
            user_message = self.store.add_message(cid, "user", content=content, attachments=attachments)
        if not conversation["title"]:
            self.store.update_conversation(actor.tenant_id, actor.subject, cid,
                                           title=title_from(content or "附件对话"))
        model_ref = str(body.get("model") or conversation.get("model") or "")
        endpoint, model, info = await self.resolve_model(model_ref, "chat")
        if body.get("model") and body["model"] != conversation.get("model"):
            self.store.update_conversation(actor.tenant_id, actor.subject, cid, model=body["model"])
        assistant = self.store.add_message(cid, "assistant", model=f"{endpoint.id}/{model}", status="pending")
        live = LiveGeneration(conversation_id=cid, message_id=assistant["id"], model=f"{endpoint.id}/{model}")
        live.status_detail = {"user_message": user_message, "assistant_message": assistant}
        self.live[cid] = live
        params = {**DEFAULT_PARAMS, **conversation["params"], **{
            k: v for k, v in (body.get("params") or {}).items() if k in DEFAULT_PARAMS}}
        # Subscribe the caller before the task starts so a fast answer cannot finish unseen.
        queue = self.attach(live)
        live.task = asyncio.create_task(self._run(actor, live, conversation, endpoint, model, info, params,
                                                  bypass=bool(body.get("bypass_gpu_wait"))))
        return live, queue

    async def _run(self, actor: AuthContext, live: LiveGeneration, conversation: dict[str, Any],
                   endpoint: ChatEndpoint, model: str, info: ModelInfo | None, params: dict[str, Any],
                   *, bypass: bool) -> None:
        caps = set(info.capabilities if info else [])
        think_param = params.get("think", "auto")
        think: bool | None = (("thinking" in caps) if think_param == "auto" else think_param in ("on", True))
        if think_param == "auto" and not caps:
            think = None
        num_ctx = int(params.get("num_ctx") or 8192)
        if info and info.context_length:
            num_ctx = min(num_ctx, info.context_length)
        try:
            messages, notes = self.build_messages(actor, conversation, self.store.list_messages(live.conversation_id),
                                                  vision="vision" in caps, num_ctx=num_ctx)
            if notes:
                live.publish({"type": "notice", "notes": notes})
            options = {"temperature": params.get("temperature"), "top_p": params.get("top_p"),
                       "num_ctx": num_ctx if endpoint.kind == "ollama" else None}
            keep_alive: str | int = 0 if self.gpu_pressure() else str(params.get("keep_alive") or "10m")
            guard: contextlib.AbstractAsyncContextManager[Any] = contextlib.nullcontext()
            if endpoint.gpu_shared and not bypass:
                if self.lease.snapshot()["busy"]:
                    self._status(live, "waiting_gpu", gpu=self.lease.snapshot())
                guard = self.lease.hold("chat", "对话：" + (conversation.get("title") or "新对话"),
                                        detail={"conversation_id": live.conversation_id})
            async with guard:
                self._status(live, "loading" if info is not None and not info.loaded else "generating")
                async for event in stream_chat(endpoint, model, messages, think=think, options=options,
                                               keep_alive=keep_alive):
                    if event.kind == "thinking":
                        live.thinking += event.text
                        if live.status != "thinking":
                            self._status(live, "thinking")
                        live.publish({"type": "thinking", "text": event.text})
                    elif event.kind == "content":
                        live.content += event.text
                        if live.status != "generating":
                            self._status(live, "generating")
                        live.publish({"type": "delta", "text": event.text})
                    else:
                        live.status_detail["stats"] = event.stats
                    self._flush(live)
            self._models = None  # loaded-model state changed
            stats = live.status_detail.get("stats", {})
            self._audit(actor, endpoint, model, purpose="chat", status="complete", stats=stats)
            self.store.update_message(live.message_id, content=live.content, thinking=live.thinking,
                                      status="complete", stats=stats)
            self.store.touch(live.conversation_id)
            live.publish({"type": "done", "message": self.store.get_message(live.message_id)})
        except asyncio.CancelledError:
            self.store.update_message(live.message_id, content=live.content, thinking=live.thinking,
                                      status="stopped")
            live.publish({"type": "done", "message": self.store.get_message(live.message_id)})
        except (EndpointError, AgentOSError) as exc:
            self._audit(actor, endpoint, model, purpose="chat", status="error")
            self.store.update_message(live.message_id, content=live.content, thinking=live.thinking,
                                      status="error", error=str(exc))
            live.publish({"type": "error", "message": str(exc),
                          "assistant_message": self.store.get_message(live.message_id)})
        except Exception as exc:  # noqa: BLE001 - unexpected server failure must still end the stream
            self.store.update_message(live.message_id, content=live.content, thinking=live.thinking,
                                      status="error", error="生成过程中发生意外错误")
            live.publish({"type": "error", "message": f"生成过程中发生意外错误：{type(exc).__name__}",
                          "assistant_message": self.store.get_message(live.message_id)})
        finally:
            self.live.pop(live.conversation_id, None)
            live.publish({"type": "end"})

    def _status(self, live: LiveGeneration, status: str, **detail: Any) -> None:
        live.status = status
        live.publish({"type": "status", "status": status, **detail})
        self.store.update_message(live.message_id, status="streaming" if status in ("thinking", "generating")
                                  else "pending")

    def _flush(self, live: LiveGeneration) -> None:
        now = time.monotonic()
        if now - live.last_flush >= 1.0:
            live.last_flush = now
            self.store.update_message(live.message_id, content=live.content, thinking=live.thinking)

    @staticmethod
    def attach(live: LiveGeneration) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        live.subscribers.append(queue)
        return queue

    async def subscribe(self, live: LiveGeneration, *, replay: bool = False,
                        queue: asyncio.Queue[dict[str, Any]] | None = None) -> AsyncIterator[dict[str, Any]]:
        if queue is None:
            queue = self.attach(live)
            if live.task is None or live.task.done():
                # The generation ended between lookup and attach; report what was saved.
                queue.put_nowait({"type": "end"})
        try:
            if replay:
                yield live.snapshot()
            while True:
                event = await queue.get()
                yield event
                if event["type"] == "end":
                    return
        finally:
            with contextlib.suppress(ValueError):
                live.subscribers.remove(queue)

    async def stop(self, actor: AuthContext, cid: str) -> bool:
        self.conversation(actor, cid)
        live = self.live.get(cid)
        if live is None or live.task is None:
            return False
        live.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(live.task), timeout=5)
        return True

    # ------------------------------------------------------------ helpers for other modules
    def _guard(self, endpoint: ChatEndpoint, label: str) -> contextlib.AbstractAsyncContextManager[Any]:
        return self.lease.hold("chat", label) if endpoint.gpu_shared else contextlib.nullcontext()

    async def complete_json(self, actor: AuthContext, messages: list[dict[str, Any]], schema: dict[str, Any], *,
                            purpose: str, model_ref: str = "", temperature: float = 0.4, num_ctx: int = 16384,
                            label: str = "制作助手") -> dict[str, Any]:
        """Structured output for other modules (one repair attempt); audited under ``purpose``."""
        endpoint, model, info = await self.resolve_model(model_ref, purpose)
        if info and info.context_length:
            num_ctx = min(num_ctx, info.context_length)
        options = {"temperature": temperature, "num_ctx": num_ctx if endpoint.kind == "ollama" else None}
        think = False if info and "thinking" in info.capabilities else None
        async with self._guard(endpoint, label):
            try:
                text, stats = await complete(endpoint, model, messages, options=options, json_schema=schema,
                                             think=think, keep_alive=0 if self.gpu_pressure() else "5m")
                try:
                    value = parse_json_object(text)
                except ValueError:
                    fix = "上面的输出不是合法 JSON。只输出符合要求的 JSON 对象，不要任何解释。"
                    repair = [*messages, {"role": "assistant", "content": text}, {"role": "user", "content": fix}]
                    text, stats = await complete(endpoint, model, repair, options=options, json_schema=schema,
                                                 think=think)
                    value = parse_json_object(text)
            except (EndpointError, ValueError):
                self._audit(actor, endpoint, model, purpose=purpose, status="error")
                raise
        self._audit(actor, endpoint, model, purpose=purpose, status="complete", stats=stats)
        return value

    async def stream_text(self, actor: AuthContext, messages: list[dict[str, Any]], *, purpose: str,
                          model_ref: str = "", temperature: float = 0.85, num_ctx: int = 16384,
                          label: str = "制作助手") -> AsyncIterator[str]:
        """Stream answer text (thinking dropped); audited under ``purpose``."""
        endpoint, model, info = await self.resolve_model(model_ref, purpose)
        if info and info.context_length:
            num_ctx = min(num_ctx, info.context_length)
        options = {"temperature": temperature, "num_ctx": num_ctx if endpoint.kind == "ollama" else None}
        stats: dict[str, Any] = {}
        status = "error"
        async with self._guard(endpoint, label):
            try:
                async for event in stream_chat(endpoint, model, messages, options=options,
                                               think=False if info and "thinking" in info.capabilities else None,
                                               keep_alive=0 if self.gpu_pressure() else "5m"):
                    if event.kind == "content":
                        yield event.text
                    elif event.kind == "done":
                        stats = event.stats
                status = "complete"
            finally:
                self._audit(actor, endpoint, model, purpose=purpose, status=status, stats=stats)

    async def complete_text(self, actor: AuthContext, messages: list[dict[str, Any]], *, purpose: str,
                            model_ref: str = "", temperature: float = 0.8, num_ctx: int = 16384,
                            label: str = "制作助手") -> str:
        endpoint, model, info = await self.resolve_model(model_ref, purpose)
        if info and info.context_length:
            num_ctx = min(num_ctx, info.context_length)
        options = {"temperature": temperature, "num_ctx": num_ctx if endpoint.kind == "ollama" else None}
        async with self._guard(endpoint, label):
            try:
                text, stats = await complete(endpoint, model, messages, options=options,
                                             think=False if info and "thinking" in info.capabilities else None,
                                             keep_alive=0 if self.gpu_pressure() else "5m")
            except EndpointError:
                self._audit(actor, endpoint, model, purpose=purpose, status="error")
                raise
        self._audit(actor, endpoint, model, purpose=purpose, status="complete", stats=stats)
        return strip_think(text)


def strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def parse_json_object(text: str) -> dict[str, Any]:
    cleaned = strip_think(text)
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, re.S)
    if fence:
        cleaned = fence.group(1)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in model output")
    value = json.loads(cleaned[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("model output is not a JSON object")
    return value


def default_endpoints(settings_endpoints: list[dict[str, Any]], *, gateway_url: str = "",
                      gateway_key: str = "", gateway_egress: str = "local") -> list[ChatEndpoint]:
    """Configured endpoints, or this machine's Ollama plus the task model gateway."""
    if settings_endpoints:
        return [ChatEndpoint.from_dict(raw) for raw in settings_endpoints]
    endpoints = [ChatEndpoint(id="local", label="本机 Ollama", base_url="http://127.0.0.1:11434",
                              kind="ollama", egress="local")]
    if gateway_url:
        # The task gateway already sits behind the deployment's declared model egress.
        endpoints.append(ChatEndpoint(id="gateway", label="模型网关", base_url=gateway_url, kind="openai",
                                      api_key=gateway_key, egress="cloud" if gateway_egress == "cloud" else "local"))
    return endpoints


def chat_db_path(data_dir: Path) -> Path:
    return data_dir / "chat.db"
