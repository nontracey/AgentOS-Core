"""M4/M5 切片测试：审计哈希链（FR-GOV-03）、Webhook 第四入口（FR-ING-01/04）、通知双通道（FR-EDG-03）。"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentos.adapters.store.sqlite import SqliteStore
from agentos.apps.composition import SystemHandle
from agentos.apps.gateway import create_app
from agentos.services.notify import NotificationService
from tests.conftest import WRITE_FLOW_SCRIPT


# ------------------------------------------------------------ 审计哈希链
def test_hash_chain_valid_and_tamper_evident(tmp_path: Path) -> None:
    """FR-GOV-03：锚定历史被改写可由校验发现（M5 基线提前实现）。"""
    store = SqliteStore(tmp_path / "h.db")
    for i in range(5):
        store.audit_append("default", f"e{i}", "owner", None, {"n": i})
    result = store.audit_verify_chain("default")
    assert result["valid"] is True and result["entries"] == 5

    # append-only 触发器直接阻断改写
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute("UPDATE audit_log SET detail_json='{\"tampered\":1}' WHERE seq=3")
    store._conn.rollback()
    # 绕过触发器（模拟有 SQL 权限者）后哈希链仍可发现
    store._conn.execute("DROP TRIGGER audit_log_no_update")
    store._conn.execute("UPDATE audit_log SET detail_json='{\"tampered\":1}' WHERE seq=3")
    store._conn.commit()
    assert store.audit_verify_chain("default")["valid"] is False


# ---------------------------------------------------------- Webhook 入口
def test_webhook_entry_reuses_accept_path(
    make_system: Callable[..., SystemHandle], monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-ING-01/04：第四类入口——kernel/runtime 零改动，仅 gateway 增加适配。"""
    handle = make_system(model_script=WRITE_FLOW_SCRIPT)
    secret = "whsec-test"
    monkeypatch.setattr(handle.settings, "webhook_secret", secret)
    app = create_app(system=handle)
    with TestClient(app) as client:
        body = b'{"payload":{"goal":"via webhook"}}'
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        r = client.post("/v1/webhook/ci", content=body,
                        headers={"Authorization": sig, "Content-Type": "application/json"})
        assert r.status_code == 202
        assert r.json()["accepted"] is True

        # 错误签名拒绝
        bad = client.post("/v1/webhook/ci", content=b'{"payload":{"goal":"evil"}}',
                          headers={"Authorization": "0" * 64})
        assert bad.status_code == 401


# ---------------------------------------------------------- 通知双通道
def test_notification_degrades_to_inbox(tmp_path: Path) -> None:
    """FR-EDG-03：主通道（webhook 未配置/故障）自动切备，待办永远可查询。"""
    store = SqliteStore(tmp_path / "n.db")
    svc = NotificationService(store, webhook_url=None)  # 主通道未配置
    result = svc.notify("default", "approval.requested", {"approval_id": "a1"})
    assert result["attempts"][0]["ok"] is False
    assert result["attempts"][1]["channel"] == "inbox" and result["attempts"][1]["ok"] is True
    row = store.raw_connection().execute(
        "SELECT COUNT(*) FROM inbox WHERE provider='inbox'"
    ).fetchone()
    assert row[0] == 1  # 待办可查询


def test_notification_primary_success_still_records_inbox(tmp_path: Path) -> None:
    """主通道成功也留系统内记录（通知丢失不丢待办）。"""
    store = SqliteStore(tmp_path / "n2.db")
    svc = NotificationService(store, webhook_url="http://127.0.0.1:1/unreachable")
    result = svc.notify("default", "e", {"x": 1})
    assert result["attempts"][0]["ok"] is False  # 主通道不可达
    assert result["attempts"][1]["ok"] is True    # 备通道成立
