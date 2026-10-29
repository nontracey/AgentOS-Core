"""HTTP routes for direct chat (`/v1/chat/*`); streaming uses authenticated fetch + SSE."""
from __future__ import annotations

import asyncio
import io
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from urllib.parse import unquote

from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import PlainTextResponse, Response, StreamingResponse

from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import AuthContext
from agentos.services.chat import ChatService

IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".xml",
                   ".html", ".css", ".js", ".ts", ".tsx", ".jsx", ".py", ".java", ".go", ".rs", ".c", ".h",
                   ".cpp", ".hpp", ".sh", ".ps1", ".sql", ".toml", ".ini", ".log", ".srt", ".vue", ".kt",
                   ".swift", ".rb", ".php", ".lua", ".r", ".m", ".tex"}
SSE_HEADERS = {"Cache-Control": "no-store, private", "X-Accel-Buffering": "no"}


def _sse(event: dict[str, Any]) -> str:
    return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"


async def _with_keepalive(events: AsyncIterator[dict[str, Any]]) -> AsyncIterator[str]:
    iterator = events.__aiter__()
    pending: asyncio.Task[dict[str, Any]] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=15)
            if not done:
                yield ": keep-alive\n\n"
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            yield _sse(event)
    finally:
        if pending is not None and not pending.done():
            pending.cancel()


def mount(app: FastAPI, service: ChatService, actor_for: Callable[[str | None], Awaitable[AuthContext]],
          *, write_guard: Callable[[AuthContext, str], None],
          register_asset: Callable[[AuthContext, bytes, str, str], dict[str, Any]]) -> None:

    async def writer(authorization: str | None, action: str) -> AuthContext:
        actor = await actor_for(authorization)
        write_guard(actor, action)
        return actor

    @app.get("/v1/chat/status")
    async def chat_status(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        await actor_for(authorization)
        return await service.status()

    @app.get("/v1/chat/models")
    async def chat_models(refresh: int = Query(default=0, ge=0, le=1),
                          purpose: str = Query(default="chat", pattern=r"^[a-z][a-z0-9_.-]{0,63}$"),
                          authorization: str | None = Header(default=None)) -> dict[str, Any]:
        """Models usable for ``purpose``; endpoints the egress policy refuses carry the reason."""
        await actor_for(authorization)
        models, errors = await service.models_for(purpose, refresh=bool(refresh))
        default = ""
        try:
            endpoint, name, _info = await service.resolve_model("", purpose)
            default = f"{endpoint.id}/{name}"
        except AgentOSError:
            pass
        return {"models": [m.public() for m in models if service.chat_capable(m)],
                "endpoints": [{**e.public(), "online": e.id not in errors, "error": errors.get(e.id, ""),
                               "blocked": bool(service.refusal(e, purpose)), "open_for": service.open_for(e)}
                              for e in service.endpoints],
                "default": default, "purpose": purpose}

    @app.get("/v1/chat/presets")
    async def presets(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        return service.presets(await actor_for(authorization))

    @app.post("/v1/chat/presets", status_code=201)
    async def save_preset(body: dict[str, Any], authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await writer(authorization, "chat.preset")
        name = str(body.get("name") or "").strip()[:40]
        prompt = str(body.get("system_prompt") or "").strip()
        if not name or not prompt:
            raise AgentOSError(ErrorCode.INVALID, "预设需要名称和系统提示词")
        return service.store.save_preset(actor.tenant_id, actor.subject, name=name, system_prompt=prompt[:8000],
                                         description=str(body.get("description") or "")[:120],
                                         preset_id=str(body.get("id") or ""))

    @app.delete("/v1/chat/presets/{preset_id}")
    async def delete_preset(preset_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await writer(authorization, "chat.preset")
        service.store.delete_preset(actor.tenant_id, actor.subject, preset_id)
        return {"deleted": True}

    @app.get("/v1/chat/conversations")
    async def conversations(q: str = Query(default="", max_length=100), archived: int = Query(default=0),
                            authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        actor = await actor_for(authorization)
        rows = service.store.list_conversations(actor.tenant_id, actor.subject, archived=bool(archived), query=q)
        return [{**row, "live": row["id"] in service.live} for row in rows]

    @app.post("/v1/chat/conversations", status_code=201)
    async def create(body: dict[str, Any], authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await writer(authorization, "chat.write")
        return service.create(actor, body)

    @app.get("/v1/chat/conversations/{cid}")
    async def detail(cid: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        return service.detail(await actor_for(authorization), cid)

    @app.patch("/v1/chat/conversations/{cid}")
    async def update(cid: str, body: dict[str, Any], authorization: str | None = Header(default=None)
                     ) -> dict[str, Any]:
        actor = await writer(authorization, "chat.write")
        return service.update(actor, cid, body)

    @app.delete("/v1/chat/conversations/{cid}")
    async def delete(cid: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await writer(authorization, "chat.write")
        await service.delete(actor, cid)
        return {"deleted": True}

    @app.post("/v1/chat/conversations/{cid}/messages")
    async def send(cid: str, body: dict[str, Any], authorization: str | None = Header(default=None)) -> Any:
        actor = await writer(authorization, "chat.write")
        live, queue = await service.send(actor, cid, body)

        async def events() -> AsyncIterator[dict[str, Any]]:
            yield {"type": "start", **{k: v for k, v in live.status_detail.items() if k != "stats"}}
            async for event in service.subscribe(live, queue=queue):
                yield event

        return StreamingResponse(_with_keepalive(events()), media_type="text/event-stream", headers=SSE_HEADERS)

    @app.get("/v1/chat/conversations/{cid}/live")
    async def live(cid: str, authorization: str | None = Header(default=None)) -> Any:
        actor = await actor_for(authorization)
        service.conversation(actor, cid)
        current = service.live.get(cid)
        if current is None:
            return Response(status_code=204)
        return StreamingResponse(_with_keepalive(service.subscribe(current, replay=True)),
                                 media_type="text/event-stream", headers=SSE_HEADERS)

    @app.post("/v1/chat/conversations/{cid}/stop")
    async def stop(cid: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await writer(authorization, "chat.write")
        return {"stopped": await service.stop(actor, cid)}

    @app.get("/v1/chat/conversations/{cid}/export")
    async def export(cid: str, authorization: str | None = Header(default=None)) -> PlainTextResponse:
        actor = await actor_for(authorization)
        conversation = service.detail(actor, cid)
        lines = [f"# {conversation['title'] or '对话'}", ""]
        for message in conversation["messages"]:
            who = "我" if message["role"] == "user" else (message["model"] or "助手")
            lines += [f"## {who}", "", message["content"], ""]
        return PlainTextResponse("\n".join(lines), media_type="text/markdown; charset=utf-8")

    @app.post("/v1/chat/attachments", status_code=201)
    async def attach(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await writer(authorization, "asset.upload")
        name = re.sub(r"[^\w. ()\-一-鿿]", "_", unquote(request.headers.get("x-file-name", "附件")))[:120]
        mime = request.headers.get("content-type", "").split(";")[0].strip().lower()
        extension = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
        is_image = mime in IMAGE_TYPES
        if not is_image and not (mime.startswith("text/") or mime in {"application/json", "application/xml"}
                                 or extension in TEXT_EXTENSIONS):
            raise AgentOSError(ErrorCode.INVALID, "支持图片（PNG/JPEG/WebP/GIF）和文本类文件（txt/md/csv/json/代码等）")
        limit = 20 * 1024**2 if is_image else 2 * 1024**2
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > limit:
                raise AgentOSError(ErrorCode.INVALID, "附件超过大小限制（图片 20MB，文本 2MB）")
        if not data:
            raise AgentOSError(ErrorCode.INVALID, "附件是空的")
        if is_image:
            from PIL import Image

            try:
                with Image.open(io.BytesIO(bytes(data))) as image:
                    width, height = image.size
                    if width * height > 40_000_000:
                        raise ValueError("too many pixels")
                    image.verify()
            except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
                raise AgentOSError(ErrorCode.INVALID, "图片已损坏或尺寸过大") from exc
        else:
            try:
                bytes(data).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise AgentOSError(ErrorCode.INVALID, "文本附件需要 UTF-8 编码") from exc
            mime = "text/plain" if not mime.startswith("text/") and mime != "application/json" else mime
        registered = register_asset(actor, bytes(data), mime, name)
        return {"asset_id": registered["asset_id"], "name": name, "mime": mime, "size": len(data),
                "kind": "image" if is_image else "text"}
