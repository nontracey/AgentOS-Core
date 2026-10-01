"""审批 CAS 语义（FR-GOV-02）：绑定主体/参数/有效期；消费原子；过期/重放失效。"""

from __future__ import annotations

from datetime import timedelta

import pytest

from agentos.adapters.store.sqlite import SqliteStore
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.states import ApprovalState
from agentos.kernel.store import ApprovalRecord
from agentos.kernel.types import AuthContext, utc_now
from agentos.services.approvals import ApprovalService


def actor(scopes: list[str] | None = None, subject: str = "owner") -> AuthContext:
    return AuthContext(
        subject=subject,
        tenant_id="default",
        scopes=scopes or ["read", "write", "approve"],
        policy_version="t",
        expires_at=utc_now() + timedelta(minutes=5),
    )


@pytest.fixture
def store(tmp_path: object) -> SqliteStore:
    return SqliteStore(tmp_path / "a.db")  # type: ignore[arg-type, operator]


def _prepare_step(store: SqliteStore, run_id: str = "run_1") -> None:
    from agentos.kernel.states import StepState
    from agentos.kernel.store import StepRecord

    store._lock  # noqa: B018
    store._conn.execute(  # type: ignore[attr-defined]
        "INSERT INTO runs(tenant_id,id,subject,state,created_at,updated_at) "
        "VALUES('default',?,'owner','running',?,?)",
        (run_id, utc_now().isoformat(), utc_now().isoformat()),
    )
    store._conn.commit()  # type: ignore[attr-defined]
    store.create_step(
        StepRecord(
            tenant_id="default", run_id=run_id, id="s1", call_id=f"{run_id}:s1",
            tool_name="fs.write_text", args_hash="hash1", state=StepState.PREPARED,
        )
    )


def test_request_once_is_idempotent(store: SqliteStore) -> None:
    _prepare_step(store)
    svc = ApprovalService(store)
    a1 = svc.request_once("default", "run_1:s1", "owner", "hash1", "pv", "summary")
    a2 = svc.request_once("default", "run_1:s1", "owner", "hash1", "pv", "summary")
    assert a1.id == a2.id
    assert a1.state == ApprovalState.PENDING


def test_decide_approve_and_cas_consume(store: SqliteStore) -> None:
    _prepare_step(store)
    svc = ApprovalService(store)
    approval = svc.request_once("default", "run_1:s1", "owner", "hash1", "pv", "summary")
    decided = svc.decide(actor(), approval.id, "approve")
    assert decided.state == ApprovalState.APPROVED
    consumed = store.consume_approval("default", approval.id, "hash1")
    assert consumed is not None and consumed.state == ApprovalState.CONSUMED
    # CAS：二次消费失败
    assert store.consume_approval("default", approval.id, "hash1") is None


def test_double_decide_conflict(store: SqliteStore) -> None:
    _prepare_step(store)
    svc = ApprovalService(store)
    approval = svc.request_once("default", "run_1:s1", "owner", "hash1", "pv", "summary")
    svc.decide(actor(), approval.id, "approve")
    with pytest.raises(AgentOSError) as exc_info:
        svc.decide(actor(), approval.id, "reject")
    assert exc_info.value.code == ErrorCode.CONFLICT


def test_wrong_subject_cannot_decide(store: SqliteStore) -> None:
    _prepare_step(store)
    svc = ApprovalService(store)
    approval = svc.request_once("default", "run_1:s1", "owner", "hash1", "pv", "summary")
    with pytest.raises(AgentOSError) as exc_info:
        svc.decide(actor(subject="helper"), approval.id, "approve")
    assert exc_info.value.code == ErrorCode.DENIED


def test_actor_without_approve_scope_cannot_decide(store: SqliteStore) -> None:
    _prepare_step(store)
    svc = ApprovalService(store)
    approval = svc.request_once("default", "run_1:s1", "owner", "hash1", "pv", "summary")
    with pytest.raises(AgentOSError):
        svc.decide(actor(scopes=["read"]), approval.id, "approve")


def test_expired_approval_cannot_be_decided(store: SqliteStore) -> None:
    _prepare_step(store)
    expired = ApprovalRecord(
        tenant_id="default", id="apr_run_1:s1", call_id="run_1:s1", subject="owner",
        args_hash="hash1", policy_version="pv", state=ApprovalState.PENDING,
        expires_at=utc_now() - timedelta(seconds=1),
    )
    store.request_approval_once(expired)
    svc = ApprovalService(store)
    with pytest.raises(AgentOSError) as exc_info:
        svc.decide(actor(), expired.id, "approve")
    assert exc_info.value.code == ErrorCode.CONFLICT


def test_consume_requires_matching_args_hash(store: SqliteStore) -> None:
    """审批后参数变化：消费失败 → 派发被拒（FR-GOV-02 参数不符）。"""
    _prepare_step(store)
    svc = ApprovalService(store)
    approval = svc.request_once("default", "run_1:s1", "owner", "hash1", "pv", "summary")
    svc.decide(actor(), approval.id, "approve")
    assert store.consume_approval("default", approval.id, "DIFFERENT-hash") is None
    # 参数正确才能消费
    assert store.consume_approval("default", approval.id, "hash1") is not None
