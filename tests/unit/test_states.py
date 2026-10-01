"""状态机合法迁移（蓝图 §5）。"""

from __future__ import annotations

import pytest

from agentos.kernel.errors import AgentOSError
from agentos.kernel.states import (
    ApprovalState,
    RunState,
    StepState,
    assert_run_transition,
    assert_step_transition,
    can_transition,
)


def test_run_happy_path_transitions() -> None:
    assert can_transition(RunState.CREATED, RunState.RUNNING)
    assert can_transition(RunState.RUNNING, RunState.AWAITING_APPROVAL)
    assert can_transition(RunState.AWAITING_APPROVAL, RunState.RUNNING)
    assert can_transition(RunState.RUNNING, RunState.SUCCEEDED)


def test_run_terminal_states_absorb_nothing() -> None:
    for terminal in (RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED):
        with pytest.raises(AgentOSError):
            assert_run_transition(terminal, RunState.RUNNING)


def test_run_illegal_jump_rejected() -> None:
    with pytest.raises(AgentOSError):
        assert_run_transition(RunState.CREATED, RunState.SUCCEEDED)  # 必须经过 RUNNING


def test_step_uncertain_is_absorbing() -> None:
    # uncertain 只能由人工对账解决；自动恢复不得复活该状态
    with pytest.raises(AgentOSError):
        assert_step_transition(StepState.UNCERTAIN, StepState.DISPATCHED)
    with pytest.raises(AgentOSError):
        assert_step_transition(StepState.UNCERTAIN, StepState.COMPLETED)


def test_step_dispatched_can_go_uncertain_or_complete() -> None:
    assert can_transition(StepState.DISPATCHED, StepState.UNCERTAIN)
    assert can_transition(StepState.DISPATCHED, StepState.COMPLETED)
    assert can_transition(StepState.DISPATCHED, StepState.PENDING_EXTERNAL)


def test_approval_terminal_states() -> None:
    # consumed 是终态：已消费批准不得再次消费
    assert ApprovalState.CONSUMED.value in {"consumed"}
    with pytest.raises(AgentOSError):
        assert_step_transition(StepState.COMPLETED, StepState.FAILED)
