"""规划 / 角色 / 交付集成测试（FR-ORC-03~05、09~12）。

计划经入口 payload.plan 声明（确定性校验），PlanRunner 用同一账本/Policy/审批执行：
- 并行形态：两个无依赖任务并发完成；
- 流水线形态：依赖顺序执行；
- 评审形态：reviewer 角色（只读工具白名单）依赖上游产出做检查；
- 交付汇总：delivery 事件含任务状态/检查/成本/未决项；
- 审批阻塞：计划内写任务阻塞 → 批准 → 续跑完成（单次效果）。
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from pathlib import Path

from agentos.apps.composition import SystemHandle
from agentos.kernel.states import RunState
from tests.conftest import owner_token


def _plan(plan_id: str, tasks: list[dict[str, object]]) -> dict[str, object]:
    return {
        "plan_id": plan_id, "version": 1, "goal_id": "g1", "goal_version": 1,
        "tasks": tasks,
    }


def _task(task_id: str, role: str, depends: list[str] | None = None,
          checks: list[str] | None = None) -> dict[str, object]:
    return {
        "task_id": task_id, "role_id": role, "depends_on": depends or [],
        "budget_microusd": 0, "acceptance_checks": checks or [],
    }


def _script_groups(*groups: tuple[str, list[dict[str, object]]]) -> list[dict[str, object]]:
    return [{"when_user_contains": g, "script": s} for g, s in groups]


def _delivery(handle: SystemHandle, run_id: str) -> dict[str, object]:
    events = handle.store.event_list("default", run_id, limit=500)
    for e in reversed(events):
        if e["kind"] == "run.state_changed":
            payload = e["payload"]
            if "delivery" in payload:
                return payload["delivery"]
    return {}


def _task_states(handle: SystemHandle, run_id: str) -> dict[str, str]:
    delivery = _delivery(handle, run_id)
    return {t["task_id"]: str(t["state"]) for t in delivery.get("tasks", [])}  # type: ignore[union-attr]


async def _submit_plan(system: SystemHandle, plan: dict[str, object], token: str) -> str:
    receipt = await system.ingress.accept(
        {"payload": {"goal": "执行计划", "plan": plan}},
        channel="cli", credentials={"token": token},
    )
    return receipt.run_id


def test_parallel_tasks_complete(make_system: Callable[..., SystemHandle]) -> None:
    script = _script_groups(
        ("t1", [{"tool_calls": [{"id": "a1", "name": "math.add", "arguments": {"a": 1, "b": 1}}]},
                {"content": "t1 sum=2"}]),
        ("t2", [{"tool_calls": [{"id": "b1", "name": "math.add", "arguments": {"a": 2, "b": 2}}]},
                {"content": "t2 sum=4"}]),
    )
    system = make_system(model_script=script)  # type: ignore[arg-type]
    system.boot()
    plan = _plan("p1", [_task("t1", "worker"), _task("t2", "worker")])
    rid = asyncio.run(_submit_plan(system, plan, owner_token(system)))
    state = asyncio.run(system.worker.drive_until_settled(rid))
    assert state == "succeeded"
    states = _task_states(system, rid)
    assert states == {"t1": "completed", "t2": "completed"}
    assert _delivery(system, rid)["cost"]["settled"] >= 0


def test_pipeline_dependency_order(make_system: Callable[..., SystemHandle]) -> None:
    script = _script_groups(
        ("t1", [{"tool_calls": [{"id": "a1", "name": "math.add", "arguments": {"a": 1, "b": 1}}]},
                {"content": "t1 done"}]),
        ("t2", [{"tool_calls": [{"id": "b1", "name": "clock.now", "arguments": {}}]},
                {"content": "t2 done after t1"}]),
    )
    system = make_system(model_script=script)  # type: ignore[arg-type]
    system.boot()
    plan = _plan("p2", [_task("t1", "worker"), _task("t2", "worker", depends=["t1"])])
    rid = asyncio.run(_submit_plan(system, plan, owner_token(system)))
    state = asyncio.run(system.worker.drive_until_settled(rid))
    assert state == "succeeded"
    # 顺序：t1 的完成事件早于 t2 的任何步骤事件
    events = system.store.event_list("default", rid, limit=500)
    seq = {("t1", "completed"): None, ("t2", "first_step"): None}
    for e in events:
        if e["kind"] == "plan.task_finished" and e["payload"].get("task_id") == "t1":
            seq[("t1", "completed")] = e["seq"]
        elif (
            e["kind"] == "step.state_changed"
            and "t2" in str(e["payload"].get("step_id", ""))
            and seq[("t2", "first_step")] is None
        ):
            seq[("t2", "first_step")] = e["seq"]
    assert seq[("t1", "completed")] is not None and seq[("t2", "first_step")] is not None
    assert seq[("t1", "completed")] < seq[("t2", "first_step")]  # type: ignore[operator]


def test_reviewer_role_readonly_whitelist(make_system: Callable[..., SystemHandle]) -> None:
    """评审形态：reviewer 只读白名单——fs.write_text 不可见，写意图被拒。"""
    script = _script_groups(
        ("t1", [{"tool_calls": [{"id": "a1", "name": "math.add", "arguments": {"a": 1, "b": 1}}]},
                {"content": "t1 done"}]),
        ("t2", [{"tool_calls": [{"id": "b1", "name": "fs.write_text",
                                 "arguments": {"path": "out/evil.txt", "content": "x"}}]},
                {"content": "should not happen"}]),
    )
    system = make_system(model_script=script)  # type: ignore[arg-type]
    system.boot()
    plan = _plan("p3", [_task("t1", "worker"), _task("t2", "reviewer", depends=["t1"])])
    rid = asyncio.run(_submit_plan(system, plan, owner_token(system)))
    state = asyncio.run(system.worker.drive_until_settled(rid))
    assert state == "failed"  # reviewer 白名单外的写工具被确定性拒绝
    assert not (system.settings.sandbox_dir / "out" / "evil.txt").exists()
    kinds = [e.kind for e in system.deps.audit.query("default", kind_prefix="policy.")]
    assert "policy.denied" in kinds


def test_acceptance_check_failure_fails_run(make_system: Callable[..., SystemHandle]) -> None:
    script = _script_groups(
        ("t1", [{"content": "我没有写文件"}]),
    )
    system = make_system(model_script=script)  # type: ignore[arg-type]
    system.boot()
    plan = _plan("p4", [_task("t1", "worker", checks=["exists:out/required.txt"])])
    rid = asyncio.run(_submit_plan(system, plan, owner_token(system)))
    state = asyncio.run(system.worker.drive_until_settled(rid))
    assert state == "failed"
    delivery = _delivery(system, rid)
    assert delivery["unresolved"] == ["t1"]
    checks = delivery["tasks"][0]["checks"]
    assert checks and checks[0]["passed"] is False


def test_plan_approval_blocking_and_resume(make_system: Callable[..., SystemHandle], data_dir: Path) -> None:
    """计划内写任务：阻塞 → 批准 → 续跑；效果恰好一次。"""
    script = _script_groups(
        ("t1", [{"tool_calls": [{"id": "a1", "name": "fs.write_text",
                                 "arguments": {"path": "out/plan.txt", "content": "planned"}}]},
                {"content": "t1 wrote"}]),
        ("t2", [{"content": "t2 reviewed"}]),
    )
    system = make_system(model_script=script)  # type: ignore[arg-type]
    system.boot()
    token = owner_token(system)
    plan = _plan("p5", [_task("t1", "worker"), _task("t2", "reviewer", depends=["t1"])])
    rid = asyncio.run(_submit_plan(system, plan, token))
    state = asyncio.run(system.worker.drive_until_settled(rid))
    assert state == "awaiting_approval"
    assert not (data_dir / "sandbox" / "out" / "plan.txt").exists()

    pending = system.deps.approvals.list_pending("default")
    actor = asyncio.run(system.deps.identity.authenticate({"token": token}))
    system.deps.approvals.decide(actor, pending[0].id, "approve")
    state = asyncio.run(system.worker.drive_until_settled(rid))
    assert state == "succeeded"
    lines = (data_dir / "sandbox" / "out" / "plan.txt").read_text(encoding="utf-8")
    assert lines == "planned"


def test_unknown_role_rejected(make_system: Callable[..., SystemHandle]) -> None:
    system = make_system(model_script=[])  # type: ignore[arg-type]
    system.boot()
    plan = _plan("p6", [{"task_id": "t1", "role_id": "hacker", "depends_on": [],
                         "budget_microusd": 0, "acceptance_checks": []}])
    token = owner_token(system)
    rid = asyncio.run(_submit_plan(system, plan, token))
    state = asyncio.run(system.worker.drive_until_settled(rid))
    assert state == "failed"


def _runs_raw(data_dir: Path) -> list[dict[str, str]]:
    conn = sqlite3.connect(data_dir / "agentos.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, state FROM runs").fetchall()
    conn.close()
    return [dict(r) for r in rows]


_ = RunState
