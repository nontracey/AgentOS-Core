"""SQLite 账本行为：accept_once 幂等/冲突、append-only、租约与 fence、预算。"""

from __future__ import annotations

import sqlite3

import pytest

from agentos.adapters.store.sqlite import SqliteStore
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.states import RunState

ENVELOPE = (
    '{"id":"env1","actor":{"subject":"owner","tenant_id":"default","scopes":["read"],'
    '"policy_version":"t","expires_at":"2099-01-01T00:00:00Z"},"payload":{"goal":"g"},'
    '"channel":"cli","idempotency_key":"k1","payload_hash":"h","attachment_ids":[]}'
)

@pytest.fixture
def store(tmp_path: object) -> SqliteStore:
    return SqliteStore(tmp_path / "s.db")  # type: ignore[arg-type, operator]


def _accept(store: SqliteStore, key: str = "k1", payload_hash: str = "h") -> object:
    return store.accept_once(
        envelope_json=ENVELOPE, envelope_id="env1", idempotency_key=key,
        payload_hash=payload_hash, tenant_id="default", subject="owner", channel="cli",
        run_budget_microusd=1000, attachment_ids=[],
    )


def test_accept_once_creates_run_and_job(store: SqliteStore) -> None:
    receipt = _accept(store)
    assert receipt.accepted is True
    run = store.get_run("default", receipt.run_id)
    assert run is not None and run.state == RunState.CREATED


def test_same_key_same_hash_replays_original(store: SqliteStore) -> None:
    first = _accept(store, key="k1", payload_hash="h")
    second = _accept(store, key="k1", payload_hash="h")
    assert second.replayed is True
    assert second.run_id == first.run_id


def test_same_key_different_hash_conflicts(store: SqliteStore) -> None:
    _accept(store, key="k1", payload_hash="h")
    with pytest.raises(AgentOSError) as exc_info:
        _accept(store, key="k1", payload_hash="DIFFERENT")
    assert exc_info.value.code == ErrorCode.CONFLICT


def test_audit_is_append_only(store: SqliteStore) -> None:
    store.audit_append("default", "test.event", "owner", None, {"k": "v"})
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute("UPDATE audit_log SET kind='tampered'")  # type: ignore[attr-defined]
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute("DELETE FROM audit_log")  # type: ignore[attr-defined]
    store._conn.rollback()  # type: ignore[attr-defined]


def test_events_are_append_only(store: SqliteStore) -> None:
    receipt = _accept(store)
    store.event_append("default", receipt.run_id, "e1", {"a": 1})
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute("UPDATE events SET kind='tampered'")  # type: ignore[attr-defined]
    store._conn.rollback()  # type: ignore[attr-defined]


def test_run_lease_is_exclusive(store: SqliteStore) -> None:
    receipt = _accept(store)
    run_id = receipt.run_id
    lease1 = store.claim_run_lease("default", run_id, "worker-a", ttl_s=60)
    assert lease1 is not None
    # 另一 owner 在租约有效期内无法认领
    assert store.claim_run_lease("default", run_id, "worker-b", ttl_s=60) is None
    # 同一 owner 可续（幂等重入）
    lease2 = store.claim_run_lease("default", run_id, "worker-a", ttl_s=60)
    assert lease2 is not None and lease2.fence > lease1.fence


def test_lease_reclaim_after_expiry(store: SqliteStore) -> None:
    receipt = _accept(store)
    run_id = receipt.run_id
    store.claim_run_lease("default", run_id, "worker-a", ttl_s=-1)  # 立即过期
    assert store.claim_run_lease("default", run_id, "worker-b", ttl_s=60) is not None


def test_job_claim_fifo_and_complete_cas(store: SqliteStore) -> None:
    receipt = _accept(store)
    store.enqueue_job("default", receipt.run_id)
    job = store.claim_job("default", "w1", ttl_s=60)
    assert job is not None
    assert store.complete_job("default", job.id, "w1", job.fence, "done") is True
    # 旧 fence 的 complete 失败（返回 0 行即失去所有权）
    assert store.complete_job("default", job.id, "w1", job.fence - 1, "done") is False


def test_budget_reserve_settle_and_circuit_break(store: SqliteStore) -> None:
    receipt = _accept(store)  # budget=1000
    run_id = receipt.run_id
    assert store.budget_reserve("default", run_id, "c1", 600) is True
    assert store.budget_reserve("default", run_id, "c2", 600) is False  # 超限熔断
    store.budget_settle("default", "c1", 500)
    totals = store.budget_totals("default", run_id)
    assert totals["settled"] == 500
    # 结算后释放额度，可再次预留
    assert store.budget_reserve("default", run_id, "c3", 400) is True


def test_outbox_dedup_by_event_key(store: SqliteStore) -> None:
    store.outbox_append("default", "ek1", "topic", "{}")
    store.outbox_append("default", "ek1", "topic", "{}")
    drained = store.outbox_drain("default")
    assert len(drained) == 1
    assert store.outbox_drain("default") == []


def test_transition_run_cas_version(store: SqliteStore) -> None:
    receipt = _accept(store)
    run_id = receipt.run_id
    updated = store.transition_run(
        "default", run_id, {RunState.CREATED}, RunState.RUNNING, expected_version=1
    )
    assert updated is not None and updated.version == 2
    # 版本不符的迁移被拒绝
    assert store.transition_run(
        "default", run_id, {RunState.RUNNING}, RunState.SUCCEEDED, expected_version=1
    ) is None
