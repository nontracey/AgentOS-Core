"""HTTP wire forwarding only; model lifecycle admission belongs to ModelGate."""
from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse

from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.services.model_gate import ModelGate


def router(gate: ModelGate) -> APIRouter:
    result = APIRouter()

    @result.post('/internal/model/v1/responses')
    @result.post('/internal/model/v1/responses/compact')
    async def forward(request: Request, authorization: str = Header(default='')) -> StreamingResponse:
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 8 * 1024 * 1024:
                raise AgentOSError(ErrorCode.INVALID, 'model request exceeds bound')
        try:
            body = json.loads(raw)
        except ValueError as exc:
            raise AgentOSError(ErrorCode.INVALID, 'invalid model request JSON') from exc
        if not isinstance(body, dict):
            raise AgentOSError(ErrorCode.INVALID, 'model request must be an object')
        admission = await gate.admit(authorization.removeprefix('Bearer ').strip(), body)
        client = httpx.AsyncClient(timeout=httpx.Timeout(180, connect=10), trust_env=False)
        path = '/responses/compact' if request.url.path.endswith('/compact') else '/responses'
        try:
            upstream = await client.send(client.build_request('POST', gate.upstream_url.rstrip('/') + path,
                json=body, headers={'Authorization': 'Bearer ' + gate.api_key}), stream=True)
        except httpx.HTTPError as exc:
            await client.aclose()
            gate.settle(admission, None)
            raise AgentOSError(ErrorCode.UNAVAILABLE, 'model provider unavailable') from exc

        if upstream.status_code >= 400:
            gate.deps.store.event_append(admission.tenant, admission.run_id, 'model.provider_failure',
                                        {'http_status': upstream.status_code})
        async def chunks() -> AsyncIterator[bytes]:
            usage: dict[str, Any] | None = None
            buffer = b''
            try:
                async for chunk in upstream.aiter_bytes():
                    buffer += chunk
                    while b'\n' in buffer:
                        line, buffer = buffer.split(b'\n', 1)
                        raw = line.removeprefix(b'data: ').strip()
                        if raw.startswith(b'{'):
                            try:
                                event = json.loads(raw)
                                usage = event.get('response', event).get('usage') or usage
                            except (ValueError, AttributeError):
                                pass
                    if len(buffer) > 4 * 1024 * 1024:
                        raise AgentOSError(ErrorCode.UNAVAILABLE, 'model response frame exceeds bound')
                    yield chunk
                if buffer:
                    with contextlib.suppress(ValueError):
                        usage = json.loads(buffer).get('usage') or usage
            finally:
                await upstream.aclose()
                await client.aclose()
                gate.settle(admission, usage)
        return StreamingResponse(chunks(), status_code=upstream.status_code,
                                 media_type=upstream.headers.get('content-type', 'application/json'))
    return result
