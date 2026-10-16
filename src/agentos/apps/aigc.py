"""Production UI endpoints using the control plane's existing trusted actor resolver."""
from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Header, Request

from agentos.apps.composition import SystemHandle
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import AuthContext
from agentos.services.aigc import AigcService
from agentos.services.capabilities import CapabilityCatalog


def mount(app: FastAPI, handle: SystemHandle, actor_for: Callable[[str | None], Awaitable[AuthContext]]) -> None:
    catalog = handle.deps.tools
    assert isinstance(catalog, CapabilityCatalog)
    service = AigcService(handle.drafts, handle.assets, handle.workflows, catalog)

    @app.get('/v1/aigc/catalog')
    async def catalog_route(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await actor_for(authorization)
        handle.access.require(actor, 'workflow.list')
        specs = await catalog.list_tools(actor)
        tools = [s.model_dump(mode='json') for s in specs if '_aigc_' in s.name]
        return {'engine_enabled': handle.workflow_driver.engine is not None,
                'tools': tools, 'templates': ['pet', 'story'],
                'can_edit': actor.has_scope('write')}

    @app.get('/v1/aigc/projects')
    async def projects(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        return handle.drafts.list(await actor_for(authorization), 'aigc')

    @app.get('/v1/aigc/projects/{id}')
    async def project(id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        return handle.drafts.get(await actor_for(authorization), 'aigc', id)

    @app.post('/v1/aigc/projects/{id}')
    async def save(id: str, body: dict[str, Any],
                   authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await actor_for(authorization)
        return service.save(actor, id, body.get('payload', {}), body.get('expected_revision', -1))

    @app.post('/v1/aigc/projects/{id}/start', status_code=202)
    async def start(id: str, body: dict[str, Any],
                    authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await actor_for(authorization)
        handle.access.require_write_allowed(actor, 'workflow.start')
        return await service.start(actor, id, body, engine_enabled=handle.workflow_driver.engine is not None)

    @app.post('/v1/aigc/projects/{id}/export')
    async def export_project(id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await actor_for(authorization)
        handle.access.require_write_allowed(actor, 'asset.upload')
        return service.export(actor, id)

    @app.get('/v1/aigc/projects/{id}/runs')
    async def runs(id: str, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        return service.runs(await actor_for(authorization), id)

    @app.post('/v1/aigc/materials', status_code=201)
    async def upload(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await actor_for(authorization)
        handle.access.require_write_allowed(actor, 'asset.upload')
        if not actor.has_scope('write'):
            raise AgentOSError(ErrorCode.DENIED, 'media upload requires write scope')
        handle.access.require(actor, 'workflow.list')
        mime = request.headers.get('content-type', '').split(';')[0].strip().lower()
        allowed = {'image/png': b'\x89PNG\r\n\x1a\n', 'image/jpeg': b'\xff\xd8\xff',
                   'image/webp': b'RIFF', 'audio/wav': b'RIFF', 'audio/x-wav': b'RIFF',
                   'audio/mpeg': None, 'video/mp4': None, 'video/webm': b'\x1aE\xdf\xa3'}
        if mime not in allowed:
            raise AgentOSError(ErrorCode.INVALID, 'supported media: PNG/JPEG/WebP, WAV/MP3, MP4/WebM')
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > 64 * 1024**2:
                raise AgentOSError(ErrorCode.INVALID, 'media exceeds 64 MiB upload limit')
            data.extend(chunk)
        signature = allowed[mime]
        if (not data or (signature and not data.startswith(signature))
                or (mime == 'image/webp' and data[8:12] != b'WEBP')
                or (mime in {'audio/wav', 'audio/x-wav'} and data[8:12] != b'WAVE')
                or (mime == 'video/mp4' and data[4:8] != b'ftyp')
                or (mime == 'audio/mpeg' and not (data.startswith(b'ID3')
                    or (len(data) >= 2 and data[0] == 255 and data[1] & 224 == 224)))):
            raise AgentOSError(ErrorCode.INVALID, 'media type does not match file header')
        digest = hashlib.sha256(data).hexdigest()
        if mime.startswith('image/'):
            import io

            from PIL import Image

            try:
                with Image.open(io.BytesIO(data)) as image:
                    if image.width * image.height > 40_000_000:
                        raise ValueError('image pixel budget exceeded')
                    image.verify()
            except (OSError, ValueError, Image.DecompressionBombError) as exc:
                raise AgentOSError(ErrorCode.INVALID, 'image is damaged or exceeds the pixel limit') from exc
        for asset in handle.assets.list(actor):
            if asset['subject'] == actor.subject and not asset['run_id'] and asset['sha256'] == digest \
                    and asset['mime'] == mime:
                handle.assets.read(actor, asset['id'])
                return {**asset, 'asset_id': asset['id'], 'reused': True}
        from urllib.parse import unquote

        name = re.sub(r'[^\w. -]', '_', unquote(request.headers.get('x-media-name', '素材')))[:120]
        return handle.assets.register_local(actor, bytes(data), mime=mime, name=name,
            meta={'kind': 'aigc_material'}, source_node='upload')
