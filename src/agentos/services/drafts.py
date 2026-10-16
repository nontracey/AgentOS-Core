"""Private editable snapshots, separate from immutable execution contracts."""
from __future__ import annotations

import json
import re
from typing import Any

from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.store import StoreProtocol
from agentos.kernel.types import AuthContext
from agentos.services.access import AccessService


class DraftService:
    def __init__(self, store: StoreProtocol, access: AccessService) -> None:
        self.store, self.access = store, access

    def _check(self, actor: AuthContext, kind: str, id: str = "draft") -> None:
        self.access.require(actor, "workflow.list")
        if kind not in {"workflow", "mcp", "aigc"} or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", id):
            raise AgentOSError(ErrorCode.INVALID, "invalid draft kind or identifier")

    @staticmethod
    def _project(row: dict[str, Any]) -> dict[str, Any]:
        return {"id": row["id"], "revision": row["revision"], "updated_at": row["updated_at"],
                "payload": json.loads(row["payload_json"])}

    def list(self, actor: AuthContext, kind: str) -> list[dict[str, Any]]:
        self._check(actor, kind)
        return [self._project(r) for r in self.store.list_drafts(actor.tenant_id, actor.subject, kind)]

    def get(self, actor: AuthContext, kind: str, id: str) -> dict[str, Any]:
        self._check(actor, kind, id)
        row = self.store.get_draft(actor.tenant_id, actor.subject, kind, id)
        if row is None:
            raise AgentOSError(ErrorCode.INVALID, "draft not found")
        return self._project(row)

    def save(self, actor: AuthContext, kind: str, id: str, payload: dict[str, Any],
             expected_revision: int) -> dict[str, Any]:
        self._check(actor, kind, id)
        self.access.require_write_allowed(actor, "workflow.draft")
        if not actor.has_scope("write"):
            raise AgentOSError(ErrorCode.DENIED, "draft editing requires write scope")
        if type(expected_revision) is not int or expected_revision < 0:
            raise AgentOSError(ErrorCode.INVALID, "expected_revision must be a nonnegative integer")
        try:
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise AgentOSError(ErrorCode.INVALID, "draft must contain valid JSON values") from exc
        if len(encoded.encode()) > 1_000_000:
            raise AgentOSError(ErrorCode.INVALID, "draft exceeds one megabyte")
        if not self.store.save_draft(actor.tenant_id, actor.subject, kind, id, encoded, expected_revision):
            raise AgentOSError(ErrorCode.CONFLICT, "draft changed in another session; reload before saving")
        return {"id": id, "revision": expected_revision + 1}

    def delete(self, actor: AuthContext, kind: str, id: str, expected_revision: int) -> None:
        self._check(actor, kind, id)
        self.access.require_write_allowed(actor, "workflow.draft")
        if not actor.has_scope("write"):
            raise AgentOSError(ErrorCode.DENIED, "draft editing requires write scope")
        if type(expected_revision) is not int or expected_revision < 1:
            raise AgentOSError(ErrorCode.INVALID, "invalid draft revision")
        if not self.store.delete_draft(actor.tenant_id, actor.subject, kind, id, expected_revision):
            raise AgentOSError(ErrorCode.CONFLICT, "draft changed in another session")
