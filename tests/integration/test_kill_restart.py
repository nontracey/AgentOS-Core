"""kill/restart 恢复演练（FR-ORC-02/FR-ORC-08）。

- 注入式崩溃：工具已产生副作用但结果未落账（进程"死亡"）→ 步骤留在 dispatched。
- 重启后同一 runId 继续：同一步骤 ID、同一幂等键 → 恰好一次已确认效果。
- 跨进程持久化：CLI 子进程提交的 run，在"新进程"中可查询、可续跑。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from agentos.adapters.tools.local import LocalToolAdapter
from agentos.apps.composition import SystemHandle
from agentos.kernel.states import StepState
from tests.conftest import IDEMPOTENT_APPEND_SCRIPT, owner_token


class _ProcessDied(BaseException):
    """模拟进程在工具副作用落盘后、结果回传前死亡。"""


async def test_crash_after_side_effect_resumes_with_single_effect(
    make_system: Callable[..., SystemHandle], data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system = make_system(model_script=IDEMPOTENT_APPEND_SCRIPT)
    system.boot()
    token = owner_token(system)

    original = LocalToolAdapter._log_append
    calls: list[str] = []

    async def crashing_append(self: LocalToolAdapter, args: dict[str, object], ctx: object) -> object:
        calls.append(str(ctx.idempotency_key))
        result = await original(self, args, ctx)  # type: ignore[arg-type]
        if result.value.get("appended"):
            raise _ProcessDied  # 副作用已发生，回执丢失
        return result

    monkeypatch.setattr(LocalToolAdapter, "_log_append", crashing_append)

    receipt = await system.ingress.accept(
        {"payload": {"goal": "append"}}, channel="cli", credentials={"token": token}
    )
    # log.append 是写工具：先走审批门，批准后首次派发即注入崩溃
    state = await system.worker.drive_until_settled(receipt.run_id)
    assert state == "awaiting_approval"
    pending = system.deps.approvals.list_pending("default")
    actor = await system.deps.identity.authenticate({"token": token})
    system.deps.approvals.decide(actor, pending[0].id, "approve")

    with pytest.raises(_ProcessDied):
        await system.worker.drive_until_settled(receipt.run_id)

    # ---- "进程死亡"：步骤留在 dispatched，效果已发生但未确认 ----
    run = system.store.get_run("default", receipt.run_id)
    assert run is not None and run.state.value in {"running", "awaiting_approval"}
    step = system.store.list_steps("default", run.id)[0]
    assert step.state == StepState.DISPATCHED

    # ---- 重启：全新 system handle（同一数据目录）----
    system.store.close()
    revived = make_system(model_script=IDEMPOTENT_APPEND_SCRIPT)
    state = await revived.worker.drive_until_settled(run.id)
    assert state == "succeeded"

    log_file = data_dir / "sandbox" / "task.log"
    lines = log_file.read_text(encoding="utf-8").splitlines()
    # 同一 call（同幂等键）恢复：恰好一次已确认效果
    assert lines == ["only-once"]
    assert len(calls) == 2  # 工具被调用了两次，但外部效果只发生一次（幂等键去重）


async def test_crash_between_steps_resumes_from_checkpoint(
    make_system: Callable[..., SystemHandle], data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """崩溃发生在步骤之间（checkpoint 已含 assistant 消息但结果不全）→ 回卷重放。"""
    script = [
        {"tool_calls": [{"id": "c1", "name": "math.add", "arguments": {"a": 1, "b": 1}}]},
        {"content": "final answer"},
    ]
    system = make_system(model_script=script)
    system.boot()
    token = owner_token(system)

    receipt = await system.ingress.accept(
        {"payload": {"goal": "math"}}, channel="cli", credentials={"token": token}
    )
    run_id = receipt.run_id
    # 崩溃注入：math.add 派发后、效果产生前死亡（步骤留在 dispatched）
    async def crashing(self: LocalToolAdapter, args: dict[str, object], ctx: object) -> object:
        raise _ProcessDied
    monkeypatch.setattr(LocalToolAdapter, "_math_add", crashing)

    with pytest.raises(_ProcessDied):
        await system.worker.drive_until_settled(run_id)

    system.store.close()
    monkeypatch.undo()  # 重启后注入解除：正常续跑
    revived = make_system(model_script=script)
    state = await revived.worker.drive_until_settled(run_id)
    assert state == "succeeded"
    run = revived.store.get_run("default", run_id)
    assert run is not None
    checkpoint = json.loads(run.checkpoint_json)
    # checkpoint 完整：assistant → tool 结果 → 最终答复
    roles = [m["role"] for m in checkpoint["messages"]]
    assert roles[0] == "system" and roles[1] == "user"
    assert "assistant" in roles and "tool" in roles
    # math.add 恰好执行一次（重放复用账本结果）
    steps = revived.store.list_steps("default", run_id)
    assert len([s for s in steps if s.state == StepState.COMPLETED]) == 1


def test_run_persists_across_cli_processes(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """跨"进程"持久化：子进程提交 → 新进程查询同一 runId。"""
    monkeypatch.setenv("AGENTOS_DATA_DIR", str(data_dir))
    env = {**os.environ, "AGENTOS_DATA_DIR": str(data_dir)}
    script_path = data_dir / "script.json"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(json.dumps(IDEMPOTENT_APPEND_SCRIPT), encoding="utf-8")

    r0 = subprocess.run(
        [sys.executable, "-m", "agentos.apps.cli", "boot"],
        capture_output=True, text=True, env=env, timeout=120, cwd=Path(__file__).parents[2],
    )
    assert r0.returncode == 0, r0.stderr

    cli = [sys.executable, "-m", "agentos.apps.cli", "task", "submit"]
    r1 = subprocess.run(
        [*cli, "跨进程目标", "--script", str(script_path), "--no-wait"],
        capture_output=True, text=True, env=env, timeout=120, cwd=Path(__file__).parents[2],
    )
    assert r1.returncode == 0, r1.stderr
    payload = json.loads(r1.stdout)
    run_id = payload["receipt"]["run_id"]

    r2 = subprocess.run(
        [sys.executable, "-m", "agentos.apps.cli", "task", "get", run_id],
        capture_output=True, text=True, env=env, timeout=120, cwd=Path(__file__).parents[2],
    )
    assert r2.returncode == 0, r2.stderr
    assert json.loads(r2.stdout)["run"]["id"] == run_id


_ = asyncio  # asyncio 引用保留（恢复路径均为 async）
