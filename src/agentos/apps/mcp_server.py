"""Official MCP SDK inbound transport; every operation shares HTTP governance."""
from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from pydantic import AnyHttpUrl

from agentos.apps.composition import SystemHandle
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import AuthContext
from agentos.services.observability import public_value


class PlatformTokenVerifier:
    def __init__(self, handle: SystemHandle, resource: str) -> None:
        self.handle, self.resource = handle, resource

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            actor = await self.handle.deps.identity.authenticate({'token': token})
            if not actor.has_scope('read'):
                return None
            return AccessToken(token=token, client_id=actor.subject, scopes=actor.scopes,
                expires_at=int(actor.expires_at.timestamp()), resource=self.resource, subject=actor.subject)
        except AgentOSError:
            return None


def mount(app: FastAPI, handle: SystemHandle) -> MCPServer:
    resource = handle.settings.runtime_gateway_url.rstrip('/') + '/mcp'
    server = MCPServer('AgentOS', version='0.1.0', log_level='WARNING',
        token_verifier=PlatformTokenVerifier(handle, resource),
        auth=AuthSettings(issuer_url=AnyHttpUrl(handle.settings.runtime_gateway_url),
            resource_server_url=AnyHttpUrl(resource), required_scopes=['read'], validate_token_resource=True))

    async def identity() -> tuple[AuthContext, str]:
        credential = get_access_token()
        if not credential or credential.expires_at is not None and credential.expires_at <= time.time():
            raise AgentOSError(ErrorCode.DENIED, 'MCP authentication required')
        actor = await handle.deps.identity.authenticate({'token': credential.token})
        return actor, credential.token

    @server.tool()
    async def task_submit(goal: str, idempotency_key: str) -> dict[str, Any]:
        """Submit a governed task; returns a persistent handle, never waits for the model."""
        actor, token = await identity()
        handle.access.require_write_allowed(actor, 'task.create')
        receipt = await handle.ingress.accept({'idempotency_key': idempotency_key, 'payload': {'goal': goal}},
                                              channel='mcp', credentials={'token': token})
        return receipt.model_dump(mode='json')

    @server.tool()
    async def task_status(run_id: str) -> dict[str, Any]:
        """Read only an authorized task's state and completed answer."""
        actor, _ = await identity()
        handle.access.require(actor, 'task.read', kind='task', resource_id=run_id)
        run = handle.store.get_run(actor.tenant_id, run_id)
        assert run
        import json
        cp = json.loads(run.checkpoint_json or '{}')
        return {'run_id': run.id, 'state': run.state.value,
                'answer': next((m['content'] for m in reversed(cp.get('messages', []))
                                if m.get('role') == 'assistant'), None)}

    @server.tool()
    async def task_cancel(run_id: str) -> dict[str, Any]:
        """Request cancellation; effect reconciliation still remains authoritative."""
        actor, _ = await identity()
        handle.access.require_write_allowed(actor, 'task.cancel')
        handle.access.require(actor, 'task.cancel', kind='task', resource_id=run_id)
        handle.store.request_cancel(actor.tenant_id, run_id)
        return {'run_id': run_id, 'cancellation_requested': True}

    if handle.knowledge.enabled:
        @server.tool()
        async def knowledge_search(query: str, limit: int = 5) -> list[dict[str, Any]]:
            """Search only the caller's currently authorized knowledge scope."""
            actor, _ = await identity()
            handle.access.require(actor, 'knowledge.search')
            hits = await handle.knowledge.search(actor, query, max(1, min(limit, 20)))
            return [public_value(hit.model_dump(mode='json')) for hit in hits]

    @server.tool()
    async def skills_list() -> list[dict[str, Any]]:
        """Discover installed immutable skill versions without granting execution rights."""
        actor, _ = await identity()
        handle.access.require(actor, 'skill.list')
        return handle.skills.list(actor)

    http_app = server.streamable_http_app(streamable_http_path='/', stateless_http=True, json_response=True)
    app.mount('/mcp', http_app)
    return server
