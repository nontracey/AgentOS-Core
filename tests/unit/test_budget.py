"""预算服务（FR-GOV-04）：预留→结算→超限熔断，异常携带预算明细。"""

from __future__ import annotations

import pytest

from agentos.adapters.store.sqlite import SqliteStore
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.services.budget import BudgetService
from agentos.services.ingress import canonical_hash


@pytest.fixture
def service(tmp_path: object) -> tuple[BudgetService, str]:
    store = SqliteStore(tmp_path / "b.db")  # type: ignore[arg-type, operator]
    envelope = (
        '{"id":"e","actor":{"subject":"owner","tenant_id":"default","scopes":[],'
        '"policy_version":"t","expires_at":"2099-01-01T00:00:00Z"},"payload":{"goal":"g"},'
        '"channel":"cli","idempotency_key":"k","payload_hash":"h","attachment_ids":[]}'
    )
    receipt = store.accept_once(
        envelope_json=envelope, envelope_id="e", idempotency_key="k", payload_hash="h",
        tenant_id="default", subject="owner", channel="cli", run_budget_microusd=1000,
        attachment_ids=[],
    )
    return BudgetService(store), receipt.run_id


def test_reserve_within_budget(service: tuple[BudgetService, str]) -> None:
    svc, run_id = service
    svc.reserve("default", run_id, "c1", 400)
    assert svc.totals("default", run_id)["reserved"] == 400


def test_reserve_over_budget_raises_with_detail(service: tuple[BudgetService, str]) -> None:
    svc, run_id = service
    svc.reserve("default", run_id, "c1", 900)
    with pytest.raises(AgentOSError) as exc_info:
        svc.reserve("default", run_id, "c2", 200)
    assert exc_info.value.code == ErrorCode.BUDGET_EXCEEDED
    assert exc_info.value.detail["reserved"] == 900


def test_settle_then_reserve_frees_capacity(service: tuple[BudgetService, str]) -> None:
    svc, run_id = service
    svc.reserve("default", run_id, "c1", 1000)
    svc.settle("default", "c1", 700)
    assert svc.totals("default", run_id)["settled"] == 700
    svc.reserve("default", run_id, "c2", 300)  # 结算释放预留
    assert svc.totals("default", run_id)["reserved"] == 300


def test_release_returns_reservation(service: tuple[BudgetService, str]) -> None:
    svc, run_id = service
    svc.reserve("default", run_id, "c1", 1000)
    svc.release("default", "c1")
    svc.reserve("default", run_id, "c2", 1000)


def test_canonical_hash_is_stable() -> None:
    assert canonical_hash({"a": 1, "b": 2}) == canonical_hash({"b": 2, "a": 1})
    assert canonical_hash({"a": 1}) != canonical_hash({"a": 2})
