"""M0 写操作安全门（需求 §9：至少 10 个正反例；所有拒绝与批准有事件）。"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from pathlib import Path

from agentos.apps.composition import SystemHandle
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.states import RunState, StepState
from tests.conftest import WRITE_FLOW_SCRIPT, add_subject, owner_token


async def submit_and_drive(
    system: SystemHandle, token: str, goal: str = "demo", key: str | None = None
) -> tuple[object, str]:
    raw: dict[str, object] = {"payload": {"goal": goal}}
    if key:
        raw["idempotency_key"] = key
    receipt = await system.ingress.accept(raw, channel="cli", credentials={"token": token})
    state = await system.worker.drive_until_settled(receipt.run_id)
    return receipt, state


def script_of(*turns: dict[str, object]) -> list[dict[str, object]]:
    return list(turns)


# ---------------------------------------------------------------- 1-2 身份门
async def test_missing_token_rejected_and_audited(make_system: Callable[..., SystemHandle]) -> None:
    system = make_system(model_script=WRITE_FLOW_SCRIPT)
    system.boot()
    with pytest_denied():
        await system.ingress.accept({"payload": {"goal": "x"}}, channel="cli", credentials={})
    kinds = [e.kind for e in system.deps.audit.query("default")]
    assert "identity.denied" in kinds


async def test_forged_token_rejected_and_audited(make_system: Callable[..., SystemHandle]) -> None:
    system = make_system(model_script=WRITE_FLOW_SCRIPT)
    system.boot()
    with pytest_denied():
        await system.ingress.accept(
            {"payload": {"goal": "x"}}, channel="cli", credentials={"token": "forged-token"}
        )
    kinds = [e.kind for e in system.deps.audit.query("default")]
    assert "identity.denied" in kinds


def pytest_denied():  # 上下文管理器小工具
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        try:
            yield
        except AgentOSError as exc:
            assert exc.code == ErrorCode.DENIED
            return
        raise AssertionError("expected DENIED")

    return _ctx()


# ---------------------------------------------------------------- 3-4 重放/冲突
async def test_replay_returns_same_receipt(make_system: Callable[..., SystemHandle]) -> None:
    system = make_system(model_script=WRITE_FLOW_SCRIPT)
    system.boot()
    token = owner_token(system)
    raw = {"payload": {"goal": "demo"}, "idempotency_key": "key-1"}
    r1 = await system.ingress.accept(raw, channel="cli", credentials={"token": token})
    r2 = await system.ingress.accept(raw, channel="cli", credentials={"token": token})
    assert r2.replayed is True and r2.run_id == r1.run_id


async def test_same_key_different_payload_conflicts_and_audits(
    make_system: Callable[..., SystemHandle],
) -> None:
    system = make_system(model_script=WRITE_FLOW_SCRIPT)
    system.boot()
    token = owner_token(system)
    await system.ingress.accept(
        {"payload": {"goal": "demo"}, "idempotency_key": "key-2"},
        channel="cli", credentials={"token": token},
    )
    try:
        await system.ingress.accept(
            {"payload": {"goal": "different"}, "idempotency_key": "key-2"},
            channel="cli", credentials={"token": token},
        )
        raise AssertionError("expected CONFLICT")
    except AgentOSError as exc:
        assert exc.code == ErrorCode.CONFLICT
    kinds = [e.kind for e in system.deps.audit.query("default")]
    assert "ingress.conflict" in kinds


# ------------------------------------------------------------ 5-7 写操作审批门
async def test_write_tool_blocks_until_approved(
    make_system: Callable[..., SystemHandle], data_dir: Path
) -> None:
    system = make_system(model_script=WRITE_FLOW_SCRIPT)
    system.boot()
    token = owner_token(system)
    _, state = await submit_and_drive(system, token)
    assert state == "awaiting_approval"
    target = data_dir / "sandbox" / "out" / "hello.txt"
    assert not target.exists()  # 未批准不得产生写入副作用

    pending = system.deps.approvals.list_pending("default")
    assert len(pending) == 1
    actor = await system.deps.identity.authenticate({"token": token})
    system.deps.approvals.decide(actor, pending[0].id, "approve")
    state = await system.worker.drive_until_settled(pending[0].call_id.split(":", 1)[0])
    assert state == "succeeded"
    assert target.read_text(encoding="utf-8") == "hello agentos"
    kinds = [e.kind for e in system.deps.audit.query("default")]
    assert "approval.requested" in kinds and "approval.approved" in kinds


async def test_rejected_approval_fails_step_without_side_effect(
    make_system: Callable[..., SystemHandle], data_dir: Path
) -> None:
    system = make_system(model_script=WRITE_FLOW_SCRIPT)
    system.boot()
    token = owner_token(system)
    _, state = await submit_and_drive(system, token)
    assert state == "awaiting_approval"
    pending = system.deps.approvals.list_pending("default")
    actor = await system.deps.identity.authenticate({"token": token})
    system.deps.approvals.decide(actor, pending[0].id, "reject")
    state = await system.worker.drive_until_settled(pending[0].call_id.split(":", 1)[0])
    assert state == "failed"
    assert not (data_dir / "sandbox" / "out" / "hello.txt").exists()
    kinds = [e.kind for e in system.deps.audit.query("default")]
    assert "approval.rejected" in kinds


async def test_helper_cannot_approve_owner_request(
    make_system: Callable[..., SystemHandle],
) -> None:
    system = make_system(model_script=WRITE_FLOW_SCRIPT)
    system.boot()
    owner = owner_token(system)
    await submit_and_drive(system, owner)
    pending = system.deps.approvals.list_pending("default")
    helper_token = add_subject(system, "helper", ["read", "approve"])
    helper = await system.deps.identity.authenticate({"token": helper_token})
    try:
        system.deps.approvals.decide(helper, pending[0].id, "approve")
        raise AssertionError("expected DENIED for cross-subject approval")
    except AgentOSError as exc:
        assert exc.code == ErrorCode.DENIED


# ------------------------------------------------------------- 8 跨主体隔离
async def test_cross_subject_tasks_are_isolated(
    make_system: Callable[..., SystemHandle],
) -> None:
    system = make_system(model_script=WRITE_FLOW_SCRIPT)
    system.boot()
    owner = owner_token(system)
    receipt, _ = await submit_and_drive(system, owner)
    helper_token = add_subject(system, "helper", ["read", "write", "approve"])
    helper = await system.deps.identity.authenticate({"token": helper_token})
    # 服务端范围过滤：helper 列表看不到 owner 的 run
    visible = system.store.list_runs("default", subject=helper.subject)
    assert all(r.id != receipt.run_id for r in visible)


# --------------------------------------------------------------- 9-11 注入面
async def test_injected_unknown_tool_never_executes(
    make_system: Callable[..., SystemHandle],
) -> None:
    evil = script_of(
        {"tool_calls": [{"id": "c1", "name": "shell.exec", "arguments": {"cmd": "rm -rf /"}}]},
        {"content": "done"},
    )
    system = make_system(model_script=evil)
    system.boot()
    _, state = await submit_and_drive(system, owner_token(system))
    assert state == "failed"
    steps = system.store.list_steps("default", system.store.list_runs("default")[0].id)
    assert all(s.state == StepState.FAILED for s in steps)
    kinds = [e.kind for e in system.deps.audit.query("default")]
    assert "policy.denied" in kinds


async def test_injected_path_escape_fails_without_side_effect(
    make_system: Callable[..., SystemHandle], data_dir: Path
) -> None:
    evil = script_of(
        {
            "tool_calls": [
                {"id": "c1", "name": "fs.write_text",
                 "arguments": {"path": "../../../escape.txt", "content": "pwned"}}
            ]
        },
        {"content": "done"},
    )
    system = make_system(model_script=evil)
    system.boot()
    _, state = await submit_and_drive(system, owner_token(system))
    assert state == "failed"
    assert not (data_dir.parent / "escape.txt").exists()
    assert not (data_dir / "escape.txt").exists()


async def test_model_allow_never_overrides_policy_deny(
    make_system: Callable[..., SystemHandle],
) -> None:
    """FR-GOV-05：模型建议没有授权效力——只读主体调写工具，Policy 必拒。"""
    evil = WRITE_FLOW_SCRIPT
    system = make_system(model_script=evil)
    system.boot()
    helper_token = add_subject(system, "helper", ["read"])
    _, state = await submit_and_drive(system, helper_token)
    assert state == "failed"
    kinds = [e.kind for e in system.deps.audit.query("default")]
    assert "policy.denied" in kinds
    # 越权执行数为 0：无审批请求、无写入
    assert system.deps.approvals.list_pending("default") == []


# ------------------------------------------------------------------ 12 取消
async def test_cancel_from_awaiting_approval(make_system: Callable[..., SystemHandle]) -> None:
    system = make_system(model_script=WRITE_FLOW_SCRIPT)
    system.boot()
    token = owner_token(system)
    receipt, state = await submit_and_drive(system, token)
    assert state == "awaiting_approval"
    run = await asyncio.to_thread(system.store.request_cancel, "default", receipt.run_id)
    assert run is not None and run.state == RunState.CANCEL_REQUESTED
    state = await system.worker.drive_until_settled(receipt.run_id)
    assert state == "cancelled"
    kinds = [e.kind for e in system.deps.audit.query("default")]
    assert "run.cancelled" in kinds


# --------------------------------------------------------- 13 拒绝全部有审计
async def test_security_denials_all_audited(make_system: Callable[..., SystemHandle]) -> None:
    system = make_system(model_script=WRITE_FLOW_SCRIPT)
    system.boot()
    for creds in ({}, {"token": "bad"}):
        with contextlib.suppress(AgentOSError):
            await system.ingress.accept({"payload": {"goal": "x"}}, channel="cli", credentials=creds)
    events = system.deps.audit.query("default")
    denied = [e for e in events if e.kind == "identity.denied"]
    assert len(denied) == 2
    assert all(e.detail.get("channel") == "cli" for e in denied)
    _ = json  # 保持导入整洁
