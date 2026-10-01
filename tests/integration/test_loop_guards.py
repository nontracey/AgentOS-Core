"""循环护栏（FR-ORC-06）：诱导循环也必须在预算内以可读原因终止。"""

from __future__ import annotations

from collections.abc import Callable

from agentos.apps.composition import SystemHandle
from agentos.kernel.states import StepState
from tests.conftest import owner_token


async def submit(system: SystemHandle, goal: str = "demo") -> str:
    token = owner_token(system)
    receipt = await system.ingress.accept(
        {"payload": {"goal": goal}}, channel="cli", credentials={"token": token}
    )
    return await system.worker.drive_until_settled(receipt.run_id)


async def test_repeated_action_guard_triggers(
    make_system: Callable[..., SystemHandle],
) -> None:
    script = [
        {"tool_calls": [{"id": f"c{n}", "name": "math.add", "arguments": {"a": 1, "b": 2}}]}
        for n in range(10)
    ] + [{"content": "never reached"}]
    system = make_system(model_script=script, runtime_max_steps=50)
    system.boot()
    state = await submit(system)
    assert state == "failed"
    events = system.deps.audit.query("default", kind_prefix="loop.guard")
    assert any(e.detail.get("code") == "repeated_action_loop" for e in events)


async def test_step_budget_guard_triggers(make_system: Callable[..., SystemHandle]) -> None:
    script = [
        {"tool_calls": [{"id": f"c{n}", "name": "math.add", "arguments": {"a": n, "b": n}}]}
        for n in range(30)
    ] + [{"content": "never reached"}]
    system = make_system(model_script=script, runtime_max_steps=5)
    system.boot()
    state = await submit(system)
    assert state == "failed"
    events = system.deps.audit.query("default", kind_prefix="loop.guard")
    assert any(e.detail.get("code") == "step_budget_exhausted" for e in events)
    # 终止时步数恰为预算上限，不越过
    run = system.store.list_runs("default")[0]
    steps = system.store.list_steps("default", run.id)
    assert len(steps) <= 6
    assert all(s.state in {StepState.COMPLETED, StepState.FAILED, StepState.CANCELLED} for s in steps)


async def test_token_budget_guard_triggers(make_system: Callable[..., SystemHandle]) -> None:
    script = [
        {"tool_calls": [{"id": "c1", "name": "math.add", "arguments": {"a": 1, "b": 2}}]},
        {"tool_calls": [{"id": "c2", "name": "clock.now", "arguments": {}}]},
        {"content": "never reached"},
    ]
    system = make_system(model_script=script, runtime_token_budget=1)
    system.boot()
    state = await submit(system)
    assert state == "failed"
    events = system.deps.audit.query("default", kind_prefix="loop.guard")
    assert any(e.detail.get("code") == "token_budget_exhausted" for e in events)


async def test_budget_circuit_breaker_denies_model_call(
    make_system: Callable[..., SystemHandle],
) -> None:
    script = [{"content": "expensive answer"}]
    system = make_system(
        model_script=script,
        run_budget_microusd=1,
        microusd_per_1k_input=1_000_000,
        microusd_per_1k_output=1_000_000,
    )
    system.boot()
    state = await submit(system)
    assert state == "failed"
    events = system.deps.audit.query("default", kind_prefix="budget.")
    assert any(e.kind == "budget.exceeded" for e in events)
    run = system.store.list_runs("default")[0]
    assert run.error_json is not None and "budget_exceeded" in run.error_json


async def test_timeout_tool_becomes_uncertain_and_never_auto_retries(
    make_system: Callable[..., SystemHandle],
) -> None:
    """FR-ORC-08：结果未知 100% 转 uncertain；重复驱动不盲重试。"""
    script = [
        {"tool_calls": [{"id": "c1", "name": "flaky.timeout", "arguments": {}}]},
        {"content": "unreachable"},
    ]
    system = make_system(model_script=script)
    system.boot()
    state = await submit(system)
    assert state == "paused"
    run = system.store.list_runs("default")[0]
    steps = system.store.list_steps("default", run.id)
    assert steps[0].state == StepState.UNCERTAIN
    # 再次驱动：不得自动重试，仍为 uncertain/paused
    state = await system.worker.drive_until_settled(run.id)
    assert state == "paused"
    kinds = [e.kind for e in system.deps.audit.query("default", kind_prefix="step.")]
    assert "step.uncertain" in kinds
