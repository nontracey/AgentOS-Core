"""Studio HTTP routes (`/v1/studio/*`) over the trusted actor resolver."""
from __future__ import annotations

import asyncio
import io
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from urllib.parse import unquote

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse

from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import AuthContext
from agentos.services.studio.service import MEDIA_TYPES, StudioService

UPLOAD_LIMIT = 256 * 1024**2


def _image_ok(data: bytes) -> None:
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > 60_000_000:
                raise ValueError("too many pixels")
            image.verify()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise AgentOSError(ErrorCode.INVALID, "图片已损坏或尺寸过大") from exc


def mount(app: FastAPI, handle: Any, actor_for: Callable[[str | None], Awaitable[AuthContext]]) -> None:
    service: StudioService = handle.studio

    async def reader(authorization: str | None) -> AuthContext:
        return await actor_for(authorization)

    async def writer(authorization: str | None, action: str = "studio.write") -> AuthContext:
        actor = await actor_for(authorization)
        handle.access.require_write_allowed(actor, action)
        return actor

    @app.get("/v1/studio/overview")
    async def overview(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        return await service.overview(await reader(authorization))

    @app.post("/v1/studio/permissions", status_code=201)
    async def permissions(body: dict[str, Any], authorization: str | None = Header(default=None)) -> dict[str, Any]:
        """Owner pre-authorizes the generation node's production tools for a while (revocable)."""
        return await service.grant(await writer(authorization, "policy.grant"), int(body.get("hours") or 24))

    @app.get("/v1/studio/projects")
    async def projects(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        return service.list_projects(await reader(authorization))

    @app.post("/v1/studio/projects", status_code=201)
    async def create(body: dict[str, Any], authorization: str | None = Header(default=None)) -> dict[str, Any]:
        return service.create_project(await writer(authorization), body)

    @app.get("/v1/studio/projects/{pid}")
    async def project(pid: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        return service.view(await reader(authorization), pid)

    @app.put("/v1/studio/projects/{pid}")
    async def save(pid: str, body: dict[str, Any], authorization: str | None = Header(default=None)
                   ) -> dict[str, Any]:
        return service.save(await writer(authorization), pid, body)

    @app.delete("/v1/studio/projects/{pid}")
    async def delete(pid: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        service.delete(await writer(authorization), pid)
        return {"deleted": True}

    @app.post("/v1/studio/projects/{pid}/select")
    async def select(pid: str, body: dict[str, Any], authorization: str | None = Header(default=None)
                     ) -> dict[str, Any]:
        take_id = body.get("take_id")
        return service.select(await writer(authorization), pid, str(body.get("slot") or ""),
                              str(body.get("target_id") or ""), str(take_id) if take_id else None)

    @app.post("/v1/studio/projects/{pid}/takes/{take_id}/rate")
    async def rate(pid: str, take_id: str, body: dict[str, Any], authorization: str | None = Header(default=None)
                   ) -> dict[str, Any]:
        service.rate(await writer(authorization), pid, take_id, int(body.get("rating") or 0))
        return {"rated": True}

    @app.delete("/v1/studio/projects/{pid}/takes/{take_id}")
    async def hide_take(pid: str, take_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        service.hide(await writer(authorization), pid, take_id)
        return {"deleted": True}

    @app.post("/v1/studio/projects/{pid}/takes/{take_id}/promote-voice")
    async def promote(pid: str, take_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        return service.promote_voice(await writer(authorization), pid, take_id)

    @app.post("/v1/studio/projects/{pid}/upload", status_code=201)
    async def upload(pid: str, request: Request, slot: str = Query(...), target_id: str = Query(...),
                     authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await writer(authorization, "asset.upload")
        mime = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if mime not in MEDIA_TYPES:
            raise AgentOSError(ErrorCode.INVALID, "支持 PNG/JPEG/WebP 图片、WAV/MP3/M4A/FLAC 音频、MP4/WebM/MOV 视频")
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > UPLOAD_LIMIT:
                raise AgentOSError(ErrorCode.INVALID, "文件超过 256MB")
        if not data:
            raise AgentOSError(ErrorCode.INVALID, "文件是空的")
        if MEDIA_TYPES[mime] == "image":
            _image_ok(bytes(data))
        name = re.sub(r"[^\w. ()\-一-鿿]", "_", unquote(request.headers.get("x-file-name", "素材")))[:120]
        return await asyncio.to_thread(service.upload, actor, pid, slot=slot, target_id=target_id,
                                       data=bytes(data), mime=mime, name=name)

    @app.post("/v1/studio/projects/{pid}/generate", status_code=202)
    async def generate(pid: str, body: dict[str, Any], authorization: str | None = Header(default=None)
                       ) -> dict[str, Any]:
        return await service.generate(await writer(authorization, "workflow.start"), pid, body)

    @app.post("/v1/studio/projects/{pid}/director/{task}", status_code=202)
    async def direct(pid: str, task: str, body: dict[str, Any], authorization: str | None = Header(default=None)
                     ) -> dict[str, Any]:
        return await service.director_job(await writer(authorization, "workflow.start"), pid, task, body)

    @app.post("/v1/studio/projects/{pid}/render", status_code=202)
    async def render_film(pid: str, body: dict[str, Any], authorization: str | None = Header(default=None)
                          ) -> dict[str, Any]:
        return await service.render(await writer(authorization, "workflow.start"), pid,
                                    str(body.get("quality") or "preview"))

    @app.post("/v1/studio/projects/{pid}/autopilot", status_code=202)
    async def autopilot(pid: str, body: dict[str, Any], authorization: str | None = Header(default=None)
                        ) -> dict[str, Any]:
        return await service.autopilot(await writer(authorization, "workflow.start"), pid, body)

    @app.get("/v1/studio/jobs")
    async def jobs(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await reader(authorization)
        return {"jobs": service.active_jobs(actor), "summary": service.queue(actor)}

    @app.post("/v1/studio/jobs/{job_id}/cancel")
    async def cancel(job_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        return service.cancel(await writer(authorization, "task.cancel"), job_id)

    @app.post("/v1/studio/jobs/{job_id}/retry", status_code=202)
    async def retry(job_id: str, batch: int = Query(default=0), authorization: str | None = Header(default=None)
                    ) -> dict[str, Any]:
        return await service.retry(await writer(authorization, "workflow.start"), job_id, batch=bool(batch))

    @app.get("/v1/studio/events")
    async def events(authorization: str | None = Header(default=None)) -> StreamingResponse:
        actor = await reader(authorization)
        queue = service.subscribe(actor)

        async def stream() -> AsyncIterator[str]:
            try:
                yield "data: " + json.dumps({"type": "hello", "gpu": service.accelerator.snapshot()}) + "\n\n"
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    yield "data: " + json.dumps(event, ensure_ascii=False, default=str) + "\n\n"
            finally:
                service.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store, private", "X-Accel-Buffering": "no"})

    @app.post("/v1/studio/sign")
    async def sign(body: dict[str, Any], authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await reader(authorization)
        ids = [str(i) for i in (body.get("asset_ids") or [])]
        return {"urls": service.sign(actor, ids, int(body.get("width") or 0))}

    @app.get("/v1/studio/media/{asset_id}")
    async def media(asset_id: str, e: int = Query(...), t: str = Query(...), sig: str = Query(...),
                    w: int = Query(default=0, ge=0, le=1600), v: str = Query(default="")) -> FileResponse:
        try:
            path, mime = await asyncio.to_thread(service.media_file, asset_id, t, e, sig, width=w, variant=v)
        except AgentOSError as exc:
            raise HTTPException(status_code=403 if exc.code == ErrorCode.DENIED else 404,
                                detail=exc.to_payload()) from exc
        return FileResponse(path, media_type=mime, headers={"Cache-Control": "private, max-age=3600"})
