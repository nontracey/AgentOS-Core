"""Owner-issued tokens for external agents (Claude Code, Codex, other MCP clients).

Each token is its own subject: member role, chosen scopes from read/write/exec/egress, an
expiry, and revocation that takes effect on the next request or step. It can never approve
or administer, so an agent cannot decide its own approval requests; the owner does that in
the workspace. Only the token's hash is stored and the plaintext is returned once.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import timedelta
from typing import Any, Protocol

from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import AuthContext, utc_now
from agentos.services.access import ROLE_ADMIN
from agentos.services.audit import AuditService

PREFIX = "agent:"
SCOPES = ("read", "write", "exec", "egress")
MAX_DAYS = 365
MAX_ACTIVE = 20
KEEP_DEAD_DAYS = 30
TOKEN_PREFIX = "aos_agent_"


class AgentSubjects(Protocol):
    def register_agent_subject(self, subject: str, *, token_sha256: str, scopes: list[str], expires_at: str,
                               label: str, created_by: str, tenant_id: str = "default") -> None: ...

    def agent_subjects(self, tenant_id: str) -> list[dict[str, Any]]: ...

    def remove_agent_subjects(self, subjects: list[str]) -> None: ...

    def revoke_subject(self, subject: str) -> None: ...


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _stale(row: dict[str, Any], cutoff: str) -> bool:
    """Inactive long enough that the listing no longer needs it (the audit log keeps the history)."""
    if row["active"]:
        return False
    return bool(row["expires_at"] and row["expires_at"] < cutoff) or (row["revoked"] and row["created_at"] < cutoff)


class AgentTokenService:
    def __init__(self, identity: AgentSubjects, audit: AuditService, *, mcp_url: str) -> None:
        self._identity, self._audit, self.mcp_url = identity, audit, mcp_url

    @staticmethod
    def _require_owner(actor: AuthContext) -> None:
        if actor.is_expired or ROLE_ADMIN not in actor.roles or not actor.has_scope("approve"):
            raise AgentOSError(ErrorCode.DENIED, "only the owner can manage agent tokens")

    @staticmethod
    def _public(row: dict[str, Any]) -> dict[str, Any]:
        expired = bool(row["expires_at"]) and row["expires_at"] <= utc_now().isoformat()
        return {"id": row["subject"][len(PREFIX):], "subject": row["subject"], "label": row["label"],
                "scopes": row["scopes"], "created_at": row["created_at"], "expires_at": row["expires_at"],
                "revoked": row["revoked"], "active": not row["revoked"] and not expired}

    def overview(self, actor: AuthContext) -> dict[str, Any]:
        self._require_owner(actor)
        return {"tokens": [self._public(r) for r in self._identity.agent_subjects(actor.tenant_id)],
                "mcp_url": self.mcp_url, "scopes": list(SCOPES)}

    def issue(self, actor: AuthContext, label: str, scopes: list[str], days: int) -> dict[str, Any]:
        self._require_owner(actor)
        label = " ".join(str(label or "").split())[:40]
        if not label:
            raise AgentOSError(ErrorCode.INVALID, "give the token a name, e.g. the agent that will use it")
        wanted = sorted({"read", *scopes}, key=SCOPES.index) if set(scopes) <= set(SCOPES) else []
        if not wanted:
            raise AgentOSError(ErrorCode.INVALID, f"scopes must be chosen from {', '.join(SCOPES)}")
        if type(days) is not int or not 1 <= days <= MAX_DAYS:
            raise AgentOSError(ErrorCode.INVALID, f"days must be between 1 and {MAX_DAYS}")
        rows = [self._public(r) for r in self._identity.agent_subjects(actor.tenant_id)]
        cutoff = (utc_now() - timedelta(days=KEEP_DEAD_DAYS)).isoformat()
        self._identity.remove_agent_subjects([r["subject"] for r in rows if _stale(r, cutoff)])
        if sum(1 for r in rows if r["active"]) >= MAX_ACTIVE:
            raise AgentOSError(ErrorCode.CONFLICT, f"at most {MAX_ACTIVE} active agent tokens; revoke one first")
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        subject = PREFIX + uuid.uuid4().hex[:12]
        expires_at = (utc_now() + timedelta(days=days)).isoformat()
        self._identity.register_agent_subject(subject, token_sha256=_digest(token), scopes=wanted,
                                              expires_at=expires_at, label=label, created_by=actor.subject,
                                              tenant_id=actor.tenant_id)
        self._audit.record(actor.tenant_id, "identity.agent_token_issued", subject=actor.subject, detail={
            "agent_subject": subject, "label": label, "scopes": wanted, "expires_at": expires_at})
        row = next(r for r in self._identity.agent_subjects(actor.tenant_id) if r["subject"] == subject)
        return {**self._public(row), "token": token}

    def revoke(self, actor: AuthContext, token_id: str) -> dict[str, Any]:
        self._require_owner(actor)
        subject = PREFIX + token_id
        row = next((r for r in self._identity.agent_subjects(actor.tenant_id) if r["subject"] == subject), None)
        if row is None:
            raise AgentOSError(ErrorCode.INVALID, "agent token not found")
        self._identity.revoke_subject(subject)
        self._audit.record(actor.tenant_id, "identity.agent_token_revoked", subject=actor.subject,
                           detail={"agent_subject": subject, "label": row["label"]})
        return self._public({**row, "revoked": True})
