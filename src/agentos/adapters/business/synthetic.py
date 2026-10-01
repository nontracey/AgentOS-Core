"""Synthetic read-only example for an existing project system, not a new PM database."""
from __future__ import annotations

from typing import Any

from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import AuthContext


class SyntheticProjectAdapter:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def list_projects(self, actor: AuthContext) -> list[dict[str, Any]]:
        if actor.is_expired or not actor.has_scope('read'):
            raise AgentOSError(ErrorCode.DENIED, 'project read scope required')
        return [{k: row[k] for k in ('external_id', 'name', 'status')} for row in self.rows
                if row['tenant_id'] == actor.tenant_id and actor.subject in row['members']]

    async def get_project(self, actor: AuthContext, external_id: str) -> dict[str, Any]:
        for row in await self.list_projects(actor):
            if row['external_id'] == external_id:
                return row
        raise AgentOSError(ErrorCode.DENIED, 'project is outside current membership')
