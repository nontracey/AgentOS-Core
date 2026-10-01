"""AgentOS kernel：自持类型、错误与 Port 契约。

本包禁止导入 agentos 的任何实现包（runtime/domains/services/adapters/apps），
由 import-linter 与 scripts/check_dependencies.py 在 CI 强制（FR-ORC-01）。
"""

from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.states import (
    APPROVAL_TERMINAL_STATES,
    RUN_TERMINAL_STATES,
    STEP_TERMINAL_STATES,
    ApprovalState,
    RunState,
    StepState,
    assert_run_transition,
    assert_step_transition,
)

__all__ = [
    "APPROVAL_TERMINAL_STATES",
    "RUN_TERMINAL_STATES",
    "STEP_TERMINAL_STATES",
    "AgentOSError",
    "ApprovalState",
    "ErrorCode",
    "RunState",
    "StepState",
    "assert_run_transition",
    "assert_step_transition",
]
