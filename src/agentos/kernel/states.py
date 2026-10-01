"""显式状态机（蓝图 §5）：Run/Step/批准的合法状态与迁移表。

状态语义：
- Run: created/awaiting_input/planning/awaiting_resources/running/awaiting_approval/
       awaiting_review/paused/cancel_requested/succeeded/failed/cancelled
- Step: prepared/awaiting_approval/dispatched/pending_external/completed/failed/
        uncertain/cancelled
- 批准: pending/approved/rejected/expired/consumed（CAS 消费）
"""

from __future__ import annotations

from enum import StrEnum

from agentos.kernel.errors import AgentOSError, ErrorCode


class RunState(StrEnum):
    CREATED = "created"
    AWAITING_INPUT = "awaiting_input"
    PLANNING = "planning"
    AWAITING_RESOURCES = "awaiting_resources"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_REVIEW = "awaiting_review"
    PAUSED = "paused"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepState(StrEnum):
    PREPARED = "prepared"
    AWAITING_APPROVAL = "awaiting_approval"
    DISPATCHED = "dispatched"
    PENDING_EXTERNAL = "pending_external"
    COMPLETED = "completed"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    CANCELLED = "cancelled"


class ApprovalState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CONSUMED = "consumed"


RUN_TERMINAL_STATES: frozenset[RunState] = frozenset(
    {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}
)
STEP_TERMINAL_STATES: frozenset[StepState] = frozenset(
    {StepState.COMPLETED, StepState.FAILED, StepState.UNCERTAIN, StepState.CANCELLED}
)
APPROVAL_TERMINAL_STATES: frozenset[ApprovalState] = frozenset(
    {ApprovalState.REJECTED, ApprovalState.EXPIRED, ApprovalState.CONSUMED}
)

RUN_TERMINAL = RUN_TERMINAL_STATES
STEP_TERMINAL = STEP_TERMINAL_STATES
APPROVAL_TERMINAL = APPROVAL_TERMINAL_STATES

# 合法迁移表（蓝图 §5：确定性业务流程固定合法状态转移）
_RUN_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.CREATED: frozenset(
        {
            RunState.RUNNING,
            RunState.PLANNING,
            RunState.AWAITING_INPUT,
            RunState.AWAITING_APPROVAL,
            RunState.CANCEL_REQUESTED,
            RunState.CANCELLED,
        }
    ),
    RunState.AWAITING_INPUT: frozenset(
        {RunState.PLANNING, RunState.RUNNING, RunState.CANCEL_REQUESTED, RunState.CANCELLED}
    ),
    RunState.PLANNING: frozenset(
        {
            RunState.RUNNING,
            RunState.AWAITING_INPUT,
            RunState.AWAITING_RESOURCES,
            RunState.AWAITING_APPROVAL,
            RunState.FAILED,
            RunState.CANCEL_REQUESTED,
            RunState.CANCELLED,
        }
    ),
    RunState.AWAITING_RESOURCES: frozenset(
        {RunState.RUNNING, RunState.PAUSED, RunState.CANCEL_REQUESTED, RunState.CANCELLED}
    ),
    RunState.RUNNING: frozenset(
        {
            RunState.AWAITING_APPROVAL,
            RunState.AWAITING_INPUT,
            RunState.AWAITING_REVIEW,
            RunState.AWAITING_RESOURCES,
            RunState.PAUSED,
            RunState.CANCEL_REQUESTED,
            RunState.SUCCEEDED,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.AWAITING_APPROVAL: frozenset(
        {RunState.RUNNING, RunState.CANCEL_REQUESTED, RunState.CANCELLED}
    ),
    RunState.AWAITING_REVIEW: frozenset({RunState.RUNNING, RunState.CANCEL_REQUESTED, RunState.CANCELLED}),
    RunState.PAUSED: frozenset(
        {RunState.RUNNING, RunState.FAILED, RunState.CANCEL_REQUESTED, RunState.CANCELLED}
    ),
    RunState.CANCEL_REQUESTED: frozenset({RunState.CANCELLED, RunState.FAILED}),
    RunState.SUCCEEDED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.CANCELLED: frozenset(),
}

_STEP_TRANSITIONS: dict[StepState, frozenset[StepState]] = {
    StepState.PREPARED: frozenset(
        {
            StepState.AWAITING_APPROVAL,
            StepState.DISPATCHED,
            StepState.CANCELLED,
            StepState.FAILED,
        }
    ),
    StepState.AWAITING_APPROVAL: frozenset(
        {StepState.DISPATCHED, StepState.CANCELLED, StepState.FAILED}
    ),
    StepState.DISPATCHED: frozenset(
        {
            StepState.PENDING_EXTERNAL,
            StepState.COMPLETED,
            StepState.FAILED,
            StepState.UNCERTAIN,
            StepState.CANCELLED,
        }
    ),
    StepState.PENDING_EXTERNAL: frozenset(
        {StepState.COMPLETED, StepState.FAILED, StepState.UNCERTAIN, StepState.CANCELLED}
    ),
    StepState.COMPLETED: frozenset(),
    StepState.FAILED: frozenset(),  # 失败步骤的重试走新建 attempt 记录，不复活终态
    StepState.UNCERTAIN: frozenset(),  # uncertain 只能由人工对账解决，阻断依赖步骤
    StepState.CANCELLED: frozenset(),
}


def assert_run_transition(current: RunState, target: RunState) -> None:
    if target not in _RUN_TRANSITIONS[current]:
        raise AgentOSError(
            ErrorCode.CONFLICT,
            f"illegal run transition {current} -> {target}",
        )


def assert_step_transition(current: StepState, target: StepState) -> None:
    if target not in _STEP_TRANSITIONS[current]:
        raise AgentOSError(
            ErrorCode.CONFLICT,
            f"illegal step transition {current} -> {target}",
        )


def can_transition(current: RunState | StepState, target: RunState | StepState) -> bool:
    if isinstance(current, RunState):
        return target in _RUN_TRANSITIONS[current]
    return target in _STEP_TRANSITIONS[current]
