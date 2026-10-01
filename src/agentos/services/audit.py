"""审计服务（FR-GOV-03）：身份判定、授权、审批、写工具与状态变更 100% 落事件。

M0 基线：append-only（SQLite 触发器强制拒绝 UPDATE/DELETE，见 adapters/store/sqlite.py）。
M5 加哈希链与独立锚点；本服务接口保持不变。
"""

from __future__ import annotations

from typing import Any

from agentos.kernel.store import AuditEvent, StoreProtocol


class AuditService:
    def __init__(self, store: StoreProtocol) -> None:
        self._store = store

    def record(
        self,
        tenant_id: str,
        kind: str,
        *,
        subject: str | None = None,
        run_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> AuditEvent:
        # 审计不含提示/工具正文：detail 由调用方保证只放元数据（ID/哈希/原因）。
        return self._store.audit_append(tenant_id, kind, subject, run_id, detail or {})

    def query(
        self,
        tenant_id: str,
        *,
        run_id: str | None = None,
        kind_prefix: str | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        return self._store.audit_query(tenant_id, run_id=run_id, kind_prefix=kind_prefix, limit=limit)
