"""错误契约（蓝图 §5）：网络超时不能一律归为可重试；必须区分是否可能已产生外部效果。"""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    """统一错误语义。`side_effect_unknown=True` 的错误不得盲重试，须转对账。"""

    INVALID = "invalid"
    DENIED = "denied"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    UNCERTAIN = "uncertain"
    CONFLICT = "conflict"
    BUDGET_EXCEEDED = "budget_exceeded"

    @property
    def side_effect_unknown(self) -> bool:
        """该错误是否意味着外部效果未知（uncertain/timeout 需对账，不得自动重放）。"""
        return self in (ErrorCode.UNCERTAIN, ErrorCode.TIMEOUT)


class AgentOSError(Exception):
    """跨层抛出的业务错误；HTTP/CLI 边界映射为稳定状态码。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        detail: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail: dict[str, object] = detail or {}

    def to_payload(self) -> dict[str, object]:
        return {"code": str(self.code), "message": self.message, "detail": self.detail}


def unsupported_capability(reason: str) -> AgentOSError:
    """FR-TOOL-10：不支持的能力必须返回可解释错误，不得伪装为成功。"""
    return AgentOSError(
        ErrorCode.UNSUPPORTED,
        f"unsupported capability: {reason}",
        detail={"reason": reason},
    )
