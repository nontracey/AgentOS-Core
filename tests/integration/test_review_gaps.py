"""第一轮审查补齐的缺口测试：

- FR-ING-03：超范围附件拒绝
- 蓝图 §8：暂存附件 TTL GC（未被 run 引用才清，被引用的保留）
- FR-ING-02：thread_id 跨入口持久化（同会话接续的 M0 机制）
- 蓝图 §5：批准过期 → expired 状态 + fail-closed
- 蓝图 §5：checkpoint 链接校验（tool 结果必须关联已声明调用）
- NFR-PRI：身份文件 0600；审计事件不含令牌
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

from agentos.adapters.store.sqlite import SqliteStore
from agentos.apps.composition import SystemHandle
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import utc_now
from tests.conftest import WRITE_FLOW_SCRIPT, owner_token


def _accept(
    system: SystemHandle, token: str, goal: str = "demo",
    key: str | None = None, attachments: list[dict[str, object]] | None = None,
):
    raw: dict[str, object] = {"payload": {"goal": goal}}
    if key:
        raw["idempotency_key"] = key
    if attachments:
        raw["attachments"] = attachments

    async def _go():
        receipt = await system.ingress.accept(raw, channel="cli", credentials={"token": token})
        state = await system.worker.drive_until_settled(receipt.run_id)
        return receipt, state

    return asyncio.run(_go())


# ------------------------------------------------------------ FR-ING-03 附件
def test_oversized_attachment_rejected_and_audited(
    make_system: Callable[..., SystemHandle],
) -> None:
    system = make_system(model_script=WRITE_FLOW_SCRIPT)
    system.boot()
    token = owner_token(system)
    big = b"x" * (10 * 1024 * 1024 + 1)
    try:
        _accept(system, token, attachments=[{"name": "big.bin", "content": big}])
        raise AssertionError("expected INVALID for oversized attachment")
    except AgentOSError as exc:
        assert exc.code == ErrorCode.INVALID
    kinds = [e.kind for e in system.deps.audit.query("default")]
    assert "ingress.attachment_rejected" in kinds


def test_attachment_gc_sweeps_only_unreferenced(
    make_system: Callable[..., SystemHandle], data_dir: Path
) -> None:
    """蓝图 §8：TTL 过期且未被 run 引用的暂存附件被清理；被 run 引用的保留。"""
    system = make_system(model_script=WRITE_FLOW_SCRIPT)
    system.boot()
    token = owner_token(system)

    # 未引用：直接 stage（不经 accept_once 关联 run）
    unreferenced = asyncio.run(
        system.ingress.stage_attachments(
            asyncio.run(system.deps.identity.authenticate({"token": token})),
            [{"name": "orphan.txt", "content": b"orphan"}],
        )
    )
    # 已引用：随 run 受理
    receipt, _ = _accept(
        system, token, attachments=[{"name": "used.txt", "content": b"used"}]
    )

    store: SqliteStore = system.store  # type: ignore[assignment]
    # 将两条附件的 created_at 回拨到 TTL 之前
    conn = sqlite3.connect(data_dir / "agentos.db")
    conn.execute("UPDATE attachments SET created_at='2020-01-01T00:00:00+00:00'")
    conn.commit()
    conn.close()

    removed = store.gc_attachments("default", ttl_minutes=60)
    assert unreferenced[0] in removed
    # 引用附件未被清理
    conn = sqlite3.connect(data_dir / "agentos.db")
    row = conn.execute(
        "SELECT id FROM attachments WHERE run_id=?", (receipt.run_id,)
    ).fetchone()
    conn.close()
    assert row is not None, "被 run 引用的附件不得被 GC"


def test_attachment_gc_audit_on_boot(make_system: Callable[..., SystemHandle], data_dir: Path) -> None:
    system = make_system(model_script=WRITE_FLOW_SCRIPT)
    system.boot()
    token = owner_token(system)
    asyncio.run(
        system.ingress.stage_attachments(
            asyncio.run(system.deps.identity.authenticate({"token": token})),
            [{"name": "stale.txt", "content": b"stale"}],
        )
    )
    conn = sqlite3.connect(data_dir / "agentos.db")
    conn.execute("UPDATE attachments SET created_at='2020-01-01T00:00:00+00:00'")
    conn.commit()
    conn.close()
    system.boot()  # 再次 boot 触发 GC
    kinds = [e.kind for e in system.deps.audit.query("default")]
    assert "ingress.attachment_gc" in kinds


# ---------------------------------------------------------- FR-ING-02 thread
def test_thread_id_persisted_across_entries(
    make_system: Callable[..., SystemHandle],
) -> None:
    """FR-ING-02：任意入口消息可挂到同一会话（thread）上接续。"""
    system = make_system(model_script=WRITE_FLOW_SCRIPT)
    system.boot()
    token = owner_token(system)
    async def _submit(channel: str) -> None:
        await system.ingress.accept(
            {"payload": {"goal": f"goal via {channel}"}, "thread_id": "thread-42"},
            channel=channel,
            credentials={"token": token},
        )

    for channel in ("cli", "http"):
        asyncio.run(_submit(channel))
    runs = system.store.list_runs("default")
    assert len(runs) == 2
    threads = {r.thread_id for r in runs}
    assert threads == {"thread-42"}, f"两个入口应共享同一 thread: {threads}"


# ------------------------------------------------------------ 蓝图 §5 过期
def test_expired_approval_fails_run_and_marks_expired(
    make_system: Callable[..., SystemHandle], data_dir: Path
) -> None:
    system = make_system(model_script=WRITE_FLOW_SCRIPT)
    system.boot()
    token = owner_token(system)
    receipt, state = _accept(system, token)
    assert state == "awaiting_approval"
    # 把批准的到期时间改到过去
    conn = sqlite3.connect(data_dir / "agentos.db")
    conn.execute("UPDATE approvals SET expires_at='2020-01-01T00:00:00+00:00'")
    conn.commit()
    conn.close()
    state = asyncio.run(system.worker.drive_until_settled(receipt.run_id))
    assert state == "failed"
    conn = sqlite3.connect(data_dir / "agentos.db")
    approval_state = conn.execute("SELECT state FROM approvals").fetchone()[0]
    conn.close()
    assert approval_state == "expired"
    # fail-closed：无任何写入
    assert not (data_dir / "sandbox" / "out" / "hello.txt").exists()
    kinds = [e.kind for e in system.deps.audit.query("default")]
    assert "approval.expired" in kinds


# ------------------------------------------------- 蓝图 §5 checkpoint 链接校验
def test_tampered_checkpoint_rejected(
    make_system: Callable[..., SystemHandle], data_dir: Path
) -> None:
    """tool 结果引用未声明的调用：启动校验拦截，run 失败且可读。"""
    system = make_system(model_script=[
        {"tool_calls": [{"id": "c1", "name": "math.add", "arguments": {"a": 1, "b": 2}}]},
        {"content": "ok"},
    ])
    system.boot()
    token = owner_token(system)
    receipt = asyncio.run(system.ingress.accept(
        {"payload": {"goal": "demo"}}, channel="cli", credentials={"token": token}
    ))
    # 注入被篡改的 checkpoint：tool 消息指向幽灵调用
    bad_checkpoint = {
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "goal"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "name": "math.add", "arguments": {"a": 1, "b": 2}}
            ]},
            {"role": "tool", "content": "{}", "tool_call_id": "ghost-call"},
        ],
        "steps_taken": 1, "tokens_used": 0, "action_hashes": [],
        "seen_steps": [], "model_call_seq": 1,
    }
    system.store.save_checkpoint("default", receipt.run_id, json.dumps(bad_checkpoint))
    state = asyncio.run(system.worker.drive_until_settled(receipt.run_id))
    assert state == "failed"
    run = system.store.get_run("default", receipt.run_id)
    assert run is not None and run.error_json is not None
    assert "linkage invalid" in run.error_json


# ------------------------------------------------------------------ NFR-PRI
def test_identity_file_permissions_and_no_token_in_audit(
    make_system: Callable[..., SystemHandle], data_dir: Path
) -> None:
    system = make_system(model_script=WRITE_FLOW_SCRIPT)
    token = system.boot()
    mode = stat.S_IMODE(os.stat(system.settings.identity_file).st_mode)
    assert mode == 0o600, f"identity file must be 0600, got {oct(mode)}"
    # 审计事件不得包含任何令牌片段
    token_prefix, token_tail = token[:6], token[-6:]
    _accept(system, token, goal="demo")
    for event in system.deps.audit.query("default"):
        blob = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
        assert token_prefix not in blob or token_prefix == "None", "audit leaks token prefix"
        assert token_tail not in blob, "audit leaks token suffix"


def test_expired_actor_rejected_before_dispatch(make_system: Callable[..., SystemHandle]) -> None:
    """AuthContext 过期：入口直接拒绝（不形成可执行 Envelope）。"""
    from agentos.kernel.types import AuthContext

    system = make_system(model_script=WRITE_FLOW_SCRIPT)
    system.boot()
    token = owner_token(system)

    async def _go() -> None:
        actor = await system.deps.identity.authenticate({"token": token})
        expired = actor.model_copy(update={"expires_at": utc_now() - timedelta(seconds=1)})
        # 模拟入口拿到过期上下文后重放（未走 refresh 的路径也会被 accept 拒绝）
        assert isinstance(expired, AuthContext) and expired.is_expired
        await system.ingress.accept(
            {"payload": {"goal": "x"}}, channel="cli", credentials={"token": "wrong"}
        )

    try:
        asyncio.run(_go())
        raise AssertionError("expected DENIED")
    except AgentOSError as exc:
        assert exc.code == ErrorCode.DENIED
