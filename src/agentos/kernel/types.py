"""核心边界类型（蓝图 §5）。

约定：
- BoundaryModel 一律 extra="forbid"，公开 JSON Schema 由 scripts/export_contracts.py 导出。
- 跨语言 JSON 中 Decimal 用十进制字符串、时间用 UTC ISO 8601（由 Pydantic 序列化保证）。
- AuthContext 仅由可信身份/策略层生成，任何入口不得接受客户端自报权限字段。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Risk = Literal["read", "write", "delete", "exec", "egress"]
Recovery = Literal["pure", "idempotency_key", "query", "manual"]


def utc_now() -> datetime:
    return datetime.now(UTC)


class BoundaryModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuthContext(BoundaryModel):
    """经入口验证的主体上下文。不携带任何可由客户端填写的权限字段。"""

    subject: str
    tenant_id: str
    scopes: list[str]
    policy_version: str
    expires_at: datetime
    roles: list[str] = Field(default_factory=list)
    identity_provider: str = "local"
    identity_ref: str = ""

    @property
    def is_expired(self) -> bool:
        return utc_now() >= self.expires_at

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


class Envelope(BoundaryModel):
    """所有入口归一后的内部消息结构（FR-ING-02）。"""

    id: str
    idempotency_key: str
    payload_hash: str
    actor: AuthContext
    channel: str
    thread_id: str | None = None
    payload: dict[str, Any]
    attachment_ids: list[str] = Field(default_factory=list)


class Receipt(BoundaryModel):
    """入口受理回执（FR-ING-06）：相同幂等键重放返回同一回执。"""

    run_id: str
    job_id: str
    accepted: bool
    replayed: bool = False


class Ref(BoundaryModel):
    source_id: str
    source_version: str
    locator: str
    quote: str


class Money(BoundaryModel):
    amount: Decimal
    currency: str


class ToolSpec(BoundaryModel):
    name: str
    version: str
    description: str = ""
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    risks: list[Risk] = Field(default_factory=list)
    recovery: Recovery = "manual"
    timeout_ms: int = Field(gt=0)
    discovery_status: Literal["local", "cached", "refreshed", "unavailable"] = "local"

    @property
    def revision(self) -> str:
        """Contract fingerprint; connection health never changes an immutable binding."""
        import hashlib
        import json

        contract = self.model_dump(mode="json", exclude={"discovery_status"})
        return hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=False).encode()).hexdigest()


class ModelToolCall(BoundaryModel):
    id: str
    name: str
    arguments: dict[str, Any]


class ChatMessage(BoundaryModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    asset_ids: list[str] = Field(default_factory=list)
    tool_call_id: str | None = None
    tool_calls: list[ModelToolCall] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_role_rules(self) -> ChatMessage:
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("tool message requires tool_call_id")
        if self.role != "assistant" and self.tool_calls:
            raise ValueError("only assistant messages may carry tool_calls")
        call_ids = [c.id for c in self.tool_calls]
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("tool_call ids must be unique within one message")
        if self.role == "tool" and self.tool_calls:
            raise ValueError("tool result message must not carry tool_calls")
        return self


def validate_tool_linkage(messages: list[ChatMessage]) -> None:
    """蓝图 §5：结果必须关联已声明的调用。

    校验整段消息序列：每条 tool 消息的 tool_call_id 必须在它之前最近的
    assistant 消息的 tool_calls 中声明过。违反时抛 INVALID（供循环回卷/失败）。
    """
    declared: set[str] = set()
    for message in messages:
        if message.role == "assistant":
            declared = {c.id for c in message.tool_calls}
        elif message.role == "tool" and message.tool_call_id not in declared:
            from agentos.kernel.errors import AgentOSError, ErrorCode

            raise AgentOSError(
                ErrorCode.INVALID,
                f"tool result references undeclared call: {message.tool_call_id}",
            )


class ModelRequest(BoundaryModel):
    messages: list[ChatMessage]
    tools: list[ToolSpec] = Field(default_factory=list)
    max_output_tokens: int = Field(gt=0)
    require_refs: bool = False


class Usage(BoundaryModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    reasoning_tokens: int = Field(ge=0, default=0)
    estimated: bool = False


class KnowledgeHit(BoundaryModel):
    chunk_id: str
    text: str
    refs: list[Ref]
    source_version: str


class CallContext(BoundaryModel):
    run_id: str
    step_id: str
    call_id: str
    actor: AuthContext
    idempotency_key: str
    fence: int
    deadline_at: datetime
    approval_id: str | None = None
    expected_revision: str | None = None


class ToolResult(BoundaryModel):
    call_id: str
    status: Literal["completed", "pending", "input_required", "failed", "uncertain"]
    value: dict[str, Any] = Field(default_factory=dict)
    external_ref: str | None = None
    error_code: str | None = None
    refs: list[Ref] = Field(default_factory=list)


class Attachment(BoundaryModel):
    """入口附件安全落盘记录（FR-ING-03）：落盘路径用自生成 ID，原文件名只作展示。"""

    id: str
    tenant_id: str
    owner: str
    display_name: str
    storage_path: str
    size_bytes: int = Field(ge=0)
    sha256: str
    content_type: str = "application/octet-stream"
