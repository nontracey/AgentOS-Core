"""通知服务（FR-EDG-03 M1 基础）：双通道降级 + 系统内待办兜底。

- 通道 1（主）：webhook（AGENTOS_NOTIFY_WEBHOOK_URL 配置时启用）
- 通道 2（备）：系统内待办（inbox 表记录，永远可查询——不宣称必达）
- 主通道失败自动切备并记录；全部失败待办仍存在（FR-EDG-03 验收语义）。
"""

from __future__ import annotations

import json
from typing import Any

from agentos.adapters.store.sqlite import SqliteStore
from agentos.kernel.types import utc_now


class NotificationService:
    def __init__(self, store: SqliteStore, webhook_url: str | None = None) -> None:
        self._store = store
        self._webhook_url = webhook_url

    def notify(self, tenant_id: str, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        attempts: list[dict[str, Any]] = []
        if self._webhook_url:
            ok, detail = self._try_webhook(payload)
            attempts.append({"channel": "webhook", "ok": ok, "detail": detail})
        else:
            attempts.append({"channel": "webhook", "ok": False, "detail": "not configured"})

        # 备通道：系统内待办（最终可查询记录，FR-EDG-03 不宣称必达）
        self._store.inbox_record(
            tenant_id, provider="inbox", event_id=f"{event}:{utc_now().isoformat()}",
            payload_hash=str(hash(json.dumps(payload, sort_keys=True))),
            payload=json.dumps(payload, ensure_ascii=False),
        )
        attempts.append({"channel": "inbox", "ok": True, "detail": "recorded"})

        # 主通道失败不影响待办成立；delivered 反映主通道状态（备通道保证可查询）
        primary_ok = attempts[0]["ok"] if attempts else False
        return {"event": event, "delivered": bool(primary_ok), "attempts": attempts}

    def _try_webhook(self, payload: dict[str, Any]) -> tuple[bool, str]:
        try:
            import httpx

            if not self._webhook_url:
                return False, "not configured"
            response = httpx.post(self._webhook_url, json=payload, timeout=5.0)
            return response.status_code < 400, f"HTTP {response.status_code}"
        except Exception as exc:  # 网络失败不抛出：降级记录
            return False, str(exc)[:120]
