"""Owner-issued pre-authorization for one tool, bounded in time and revocable (FR-GOV-02).

A grant lets the owner's own tasks call one named tool without a per-step approval until it
expires or is revoked. It never widens scopes: the policy still checks arguments, scopes and
budget at every dispatch. Deleting and outbound tools keep per-step approval.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, Protocol

from agentos.kernel.dto import PolicyGrant
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.store import StoreProtocol
from agentos.kernel.types import AuthContext, ToolSpec, utc_now
from agentos.services.access import ROLE_ADMIN
from agentos.services.audit import AuditService

MAX_HOURS = 24 * 7
STEP_APPROVAL_RISKS = frozenset({"delete", "egress"})


class ToolResolver(Protocol):
    async def resolve(self, actor: AuthContext, name: str) -> ToolSpec: ...


class GrantService:
    def __init__(self, store: StoreProtocol, audit: AuditService, tools: ToolResolver) -> None:
        self._store, self._audit, self._tools = store, audit, tools

    @staticmethod
    def _public(grant: PolicyGrant) -> dict[str, Any]:
        active = not grant.revoked and grant.expires_at > utc_now()
        return {"id": grant.id, "tool_name": grant.tool_name, "risks": grant.risks,
                "expires_at": grant.expires_at.isoformat(), "issued_by": grant.issued_by,
                "revoked": grant.revoked, "active": active}

    def list(self, actor: AuthContext) -> list[dict[str, Any]]:
        if actor.is_expired:
            raise AgentOSError(ErrorCode.DENIED, "auth context expired")
        grants = sorted(self._store.list_grants(actor.tenant_id, actor.subject), key=lambda g: g.expires_at,
                        reverse=True)
        return [self._public(g) for g in grants]

    def active(self, actor: AuthContext, tool_name: str) -> dict[str, Any] | None:
        now = utc_now()
        found = [g for g in self._store.list_grants(actor.tenant_id, actor.subject)
                 if g.tool_name == tool_name and not g.revoked and g.expires_at > now]
        return self._public(max(found, key=lambda g: g.expires_at)) if found else None

    @staticmethod
    def _require_owner(actor: AuthContext) -> None:
        if actor.is_expired or ROLE_ADMIN not in actor.roles or not actor.has_scope("approve"):
            raise AgentOSError(ErrorCode.DENIED, "only the owner can pre-authorize tools")

    async def issue(self, actor: AuthContext, tool_name: str, hours: int) -> dict[str, Any]:
        self._require_owner(actor)
        if type(hours) is not int or not 1 <= hours <= MAX_HOURS:
            raise AgentOSError(ErrorCode.INVALID, f"hours must be between 1 and {MAX_HOURS}")
        spec = await self._tools.resolve(actor, tool_name)
        if STEP_APPROVAL_RISKS.intersection(spec.risks):
            raise AgentOSError(ErrorCode.DENIED, "deleting or outbound tools keep per-step approval")
        grant = PolicyGrant(id="grt_" + uuid.uuid4().hex[:16], tenant_id=actor.tenant_id, subject=actor.subject,
                            tool_name=spec.name, risks=list(spec.risks),
                            expires_at=utc_now() + timedelta(hours=hours), issued_by=actor.subject)
        self._store.upsert_grant(grant)
        self._audit.record(actor.tenant_id, "policy.grant_issued", subject=actor.subject, detail={
            "grant_id": grant.id, "tool": grant.tool_name, "risks": grant.risks,
            "expires_at": grant.expires_at.isoformat()})
        return self._public(grant)

    def revoke(self, actor: AuthContext, grant_id: str) -> dict[str, Any]:
        self._require_owner(actor)
        grant = next((g for g in self._store.list_grants(actor.tenant_id, actor.subject) if g.id == grant_id), None)
        if grant is None:
            raise AgentOSError(ErrorCode.INVALID, "grant not found")
        revoked = grant.model_copy(update={"revoked": True})
        self._store.upsert_grant(revoked)
        self._audit.record(actor.tenant_id, "policy.grant_revoked", subject=actor.subject,
                           detail={"grant_id": grant.id, "tool": grant.tool_name})
        return self._public(revoked)
