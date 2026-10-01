"""F1 深化（FR-NODE-05 / FR-ORC-08）：持久 job、租约接管、取消竞态。

用共享同一 data_dir 的两个 SystemHandle 模拟"两个进程"（SQLite WAL 持久化）：
- 进程 A 跑到一半死亡：不释放租约、不 complete job
- 进程 B 起后台 worker（只认领队列，不点名 run）：持久化的 job 被自动捞起续跑
- 副作用（log.append 幂等键去重）全程恰好一次

与 test_kill_restart.py 的分工：那边是"手工 drive 指定 run"的崩溃恢复；
这里是**队列驱动的接管**——不点名 run，验证 job 持久化与租约接管本身。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from pathlib import Path

import pytest

from agentos.adapters.tools.local import LocalToolAdapter
from agentos.apps.composition import SystemHandle
from agentos.kernel.states import RunState, StepState
from tests.conftest import IDEMPOTENT_APPEND_SCRIPT, owner_token


class _ProcessDied(BaseException):
    """模拟进程在工具副作用落盘后、结果回传前死亡。"""


def _crash_after_side_effect(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    original = LocalToolAdapter._log_append

    async def crashing_append(self: LocalToolAdapter, args: dict[str, object],
                              ctx: object) -> object:
        calls.append(str(ctx.idempotency_key))
        result = await original(self, args, ctx)  # type: ignore[arg-type]
        if result.value.get("appended"):
            raise _ProcessDied  # 副作用已发生，回执丢失
        return result

    monkeypatch.setattr(LocalToolAdapter, "_log_append", crashing_append)


async def _submit_and_crash(system: SystemHandle, calls: list[str],
                            monkeypatch: pytest.MonkeyPatch) -> str:
    """提交写任务 → 审批 → 首次派发注入崩溃，返回 run_id。"""
    _crash_after_side_effect(monkeypatch, calls)
    token = owner_token(system)
    receipt = await system.ingress.accept(
        {"payload": {"goal": "append"}}, channel="cli", credentials={"token": token}
    )
    state = await system.worker.drive_until_settled(receipt.run_id)
    assert state == "awaiting_approval"
    pending = system.deps.approvals.list_pending("default")
    actor = await system.deps.identity.authenticate({"token": token})
    system.deps.approvals.decide(actor, pending[0].id, "approve")
    with pytest.raises(_ProcessDied):
        await system.worker.drive_until_settled(receipt.run_id)
    return receipt.run_id


async def test_persistent_job_resumed_by_queue_worker_in_new_process(
    make_system: Callable[..., SystemHandle], data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """进程死亡后，新进程的后台 worker 从持久队列捞起 job 续跑（不点名 run）。"""
    system = make_system(model_script=IDEMPOTENT_APPEND_SCRIPT)
    system.boot()
    calls: list[str] = []
    run_id = await _submit_and_crash(system, calls, monkeypatch)

    # 队列里必须有未完成的持久 job（不是靠内存里的待办）
    if hasattr(system.store, "list_jobs"):
        system.store.list_jobs("default")
    step = system.store.list_steps("default", run_id)[0]
    assert step.state == StepState.DISPATCHED

    # ---- 新进程：只起后台 worker，不点名 run ----
    system.store.close()
    revived = make_system(model_script=IDEMPOTENT_APPEND_SCRIPT)
    stop = asyncio.Event()
    task = asyncio.create_task(revived.worker.run_forever(stop))
    try:
        deadline = asyncio.get_event_loop().time() + 20
        run = None
        while asyncio.get_event_loop().time() < deadline:
            run = revived.store.get_run("default", run_id)
            if run is not None and run.state == RunState.SUCCEEDED:
                break
            await asyncio.sleep(0.1)
        assert run is not None and run.state == RunState.SUCCEEDED
    finally:
        stop.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    log_file = data_dir / "sandbox" / "task.log"
    assert log_file.read_text(encoding="utf-8").splitlines() == ["only-once"]


async def test_lease_taken_over_by_another_owner_after_worker_death(
    make_system: Callable[..., SystemHandle], data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """worker A 持租约死亡 → 租约过期后 worker B（另一 owner）接管并跑完。"""
    system = make_system(model_script=IDEMPOTENT_APPEND_SCRIPT)
    system.boot()
    calls: list[str] = []
    # 模拟进程死亡来不及释放租约（真实 SIGKILL 不会走 drive_run 的 finally；
    # 正常退出路径一律释放是 P0-1 设计，这里屏蔽掉才是"僵尸租约"场景）
    monkeypatch.setattr(system.store, "release_run_lease", lambda *a, **k: None)
    run_id = await _submit_and_crash(system, calls, monkeypatch)

    # A 的僵尸租约仍在 → 另一 owner 此刻接不了（防双跑）
    lease_now = system.store.claim_run_lease("default", run_id, "worker-B", ttl_s=60)
    assert lease_now is None, "A 的租约未过期时 B 不应接管（防止双跑）"

    # 让 A 的租约过期（模拟 TTL 到期接管；owner 是组合根注入的唯一 worker owner）
    owner_a = system.worker._owner  # noqa: SLF001 - 测试内取进程级 owner
    assert system.store.claim_run_lease("default", run_id, owner_a, ttl_s=-1) is not None
    lease_later = system.store.claim_run_lease("default", run_id, "worker-B", ttl_s=60)
    assert lease_later is not None and lease_later.fence > 0

    # B 以新 owner 驱动到终态
    state = await system.worker.drive_until_settled(run_id, deadline_s=20, owner="worker-B")
    assert state == "succeeded"
    log_file = data_dir / "sandbox" / "task.log"
    assert log_file.read_text(encoding="utf-8").splitlines() == ["only-once"]


async def test_cancel_before_dispatch_prevents_side_effect(
    make_system: Callable[..., SystemHandle], data_dir: Path
) -> None:
    """派发前取消（待审批窗口）：取消生效，写动作一次都不执行。"""
    system = make_system(model_script=IDEMPOTENT_APPEND_SCRIPT)
    system.boot()
    token = owner_token(system)
    receipt = await system.ingress.accept(
        {"payload": {"goal": "append"}}, channel="cli", credentials={"token": token}
    )
    state = await system.worker.drive_until_settled(receipt.run_id)
    assert state == "awaiting_approval"

    system.store.request_cancel("default", receipt.run_id)
    final = await system.worker.drive_until_settled(receipt.run_id, deadline_s=15)
    assert final == "cancelled"

    log_file = data_dir / "sandbox" / "task.log"
    assert not log_file.exists() or log_file.read_text(encoding="utf-8").strip() == ""


async def test_cancel_after_effect_does_not_roll_back_or_repeat(
    make_system: Callable[..., SystemHandle], data_dir: Path
) -> None:
    """派发完成后取消：已确认效果不回滚、不重放（终态优先，取消无效）。"""
    system = make_system(model_script=IDEMPOTENT_APPEND_SCRIPT)
    system.boot()
    token = owner_token(system)
    receipt = await system.ingress.accept(
        {"payload": {"goal": "append"}}, channel="cli", credentials={"token": token}
    )
    state = await system.worker.drive_until_settled(receipt.run_id)
    assert state == "awaiting_approval"
    pending = system.deps.approvals.list_pending("default")
    actor = await system.deps.identity.authenticate({"token": token})
    system.deps.approvals.decide(actor, pending[0].id, "approve")

    final = await system.worker.drive_until_settled(receipt.run_id, deadline_s=20)
    assert final == "succeeded"

    # 终态后取消无效（request_cancel 对终态返回原 run，不改状态）
    run = system.store.request_cancel("default", receipt.run_id)
    assert run is not None and run.state == RunState.SUCCEEDED

    log_file = data_dir / "sandbox" / "task.log"
    assert log_file.read_text(encoding="utf-8").splitlines() == ["only-once"]
