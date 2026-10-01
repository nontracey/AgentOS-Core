"""M5 企业隔离模拟（FR-GOV-01/07）：两组织×两角色，跨域访问全拒。

用两个 tenant_id（org-alpha / org-beta）模拟两个组织。所有 store 查询
均带 tenant_id 过滤——跨组织读写/搜索/导出 0 条泄漏。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from agentos.adapters.store.sqlite import SqliteStore
from agentos.kernel.types import AuthContext, utc_now


def actor(tenant: str, subject: str) -> AuthContext:
    return AuthContext(
        subject=subject, tenant_id=tenant, scopes=["read", "write", "approve"],
        policy_version="enterprise-sim", expires_at=utc_now() + timedelta(minutes=10),
    )


@pytest.fixture
def store(tmp_path: Path) -> SqliteStore:
    return SqliteStore(tmp_path / "ent.db")


def test_cross_org_run_isolation(store: SqliteStore) -> None:
    """A 组织的 run 对 B 组织不可见（FR-GOV-01 两组织隔离）。"""
    receipt = store.accept_once(
        envelope_json="{}", envelope_id="env1", idempotency_key="k1",
        payload_hash="h", tenant_id="org-alpha", subject="alice",
        channel="cli", run_budget_microusd=0, attachment_ids=[],
    )
    runs_b = store.list_runs("org-beta", subject="alice")
    assert all(r.id != receipt.run_id for r in runs_b)


def test_cross_org_step_isolation(store: SqliteStore) -> None:
    receipt = store.accept_once(
        envelope_json="{}", envelope_id="env2", idempotency_key="k2",
        payload_hash="h", tenant_id="org-alpha", subject="alice",
        channel="cli", run_budget_microusd=0, attachment_ids=[],
    )
    steps = store.list_steps("org-beta", receipt.run_id)
    assert steps == []


def test_cross_org_approval_isolation(store: SqliteStore) -> None:
    from agentos.kernel.states import StepState
    from agentos.kernel.store import StepRecord

    receipt = store.accept_once(
        envelope_json="{}", envelope_id="env3", idempotency_key="k3",
        payload_hash="h", tenant_id="org-alpha", subject="alice",
        channel="cli", run_budget_microusd=0, attachment_ids=[],
    )
    store.create_step(
        StepRecord(tenant_id="org-alpha", run_id=receipt.run_id, id="s1",
                   call_id=f"{receipt.run_id}:s1", tool_name="fs.write_text",
                   args_hash="x", state=StepState.AWAITING_APPROVAL)
    )
    approvals_b = store.list_approvals("org-beta")
    assert approvals_b == []


def test_cross_org_budget_isolation(store: SqliteStore) -> None:
    receipt = store.accept_once(
        envelope_json="{}", envelope_id="env4", idempotency_key="k4",
        payload_hash="h", tenant_id="org-alpha", subject="alice",
        channel="cli", run_budget_microusd=1000, attachment_ids=[],
    )
    assert store.budget_reserve("org-beta", receipt.run_id, "c1", 100) is False
