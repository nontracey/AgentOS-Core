"""Port 边界与最小接口（蓝图 §6）。

Port 只覆盖能力变化或跨进程边界；Policy/Budget/Audit 是强制领域服务，不作为可替换插件。
缺失能力返回 UNSUPPORTED_CAPABILITY，不得吞错伪装成功（kernel.errors.unsupported_capability）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from agentos.kernel.decision import DecisionQuestion, DecisionResult
from agentos.kernel.types import (
    AuthContext,
    CallContext,
    KnowledgeHit,
    ModelRequest,
    Ref,
    ToolResult,
    ToolSpec,
)


@runtime_checkable
class ModelPort(Protocol):
    async def capabilities(self) -> dict[str, Any]: ...

    def stream(self, request: ModelRequest, actor: AuthContext) -> AsyncIterator[dict[str, Any]]: ...


@runtime_checkable
class ToolPort(Protocol):
    async def list_tools(self, actor: AuthContext) -> list[ToolSpec]: ...

    async def resolve(self, actor: AuthContext, name: str) -> ToolSpec: ...

    async def call(self, name: str, args: dict[str, Any], ctx: CallContext) -> ToolResult: ...

    async def poll(self, external_ref: str, ctx: CallContext) -> ToolResult: ...

    async def cancel(self, external_ref: str, ctx: CallContext) -> ToolResult: ...


@runtime_checkable
class KnowledgePort(Protocol):
    async def ingest(self, actor: AuthContext, source_id: str) -> str: ...

    async def search(self, actor: AuthContext, query: str, limit: int) -> list[KnowledgeHit]: ...

    async def resolve_ref(self, actor: AuthContext, ref: Ref) -> dict[str, Any]: ...

    async def delete(self, actor: AuthContext, source_id: str) -> None: ...


@runtime_checkable
class MemoryPort(Protocol):
    async def query(self, actor: AuthContext, query: str) -> list[dict[str, Any]]: ...

    async def put(self, actor: AuthContext, entry: dict[str, Any]) -> str: ...

    async def forget(self, actor: AuthContext, entry_id: str) -> None: ...


@runtime_checkable
class SkillPort(Protocol):
    async def list(self, actor: AuthContext) -> list[dict[str, Any]]: ...

    async def activate(self, actor: AuthContext, skill_id: str, version: str) -> dict[str, Any]: ...

    async def install(self, actor: AuthContext, source: str, revision: str) -> str: ...

    async def remove(self, actor: AuthContext, skill_id: str) -> None: ...


@runtime_checkable
class NodePort(Protocol):
    async def schedule(self, task: dict[str, Any], ctx: CallContext) -> str: ...

    async def poll(self, handle: str, ctx: CallContext) -> ToolResult: ...

    async def cancel(self, handle: str, ctx: CallContext) -> ToolResult: ...


@runtime_checkable
class ExportPort(Protocol):
    async def private_backup(self, actor: AuthContext) -> str: ...

    async def public_export(self, actor: AuthContext, manifest_id: str) -> str: ...

    async def restore(self, actor: AuthContext, backup_id: str) -> str: ...


@runtime_checkable
class WorkflowPort(Protocol):
    """蓝图 §9.4：发布版本不可变；一次运行只有一个 checkpoint/retry 所有者。"""

    async def publish(self, actor: AuthContext, definition: dict[str, Any]) -> dict[str, Any]: ...

    async def start(
        self,
        actor: AuthContext,
        workflow_id: str,
        version: int,
        inputs: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, Any]: ...

    async def status(self, actor: AuthContext, handle: dict[str, Any]) -> dict[str, Any]: ...

    async def signal(self, actor: AuthContext, handle: dict[str, Any], signal: str) -> None: ...

    async def cancel(self, actor: AuthContext, handle: dict[str, Any]) -> dict[str, Any]: ...


@runtime_checkable
class DecisionPort(Protocol):
    """蓝图 §11：快决策层只输出建议；Policy 的 deny/ask 不可由模型放宽。"""

    async def decide(
        self, state: dict[str, Any], questions: list[DecisionQuestion]
    ) -> list[DecisionResult]: ...

    async def calibration_profile(self) -> dict[str, Any]: ...


@runtime_checkable
class IdentityPort(Protocol):
    """入口身份解析（FR-ING-03/05）：未经验证不得形成可执行 Envelope。"""

    async def authenticate(self, credentials: dict[str, Any]) -> AuthContext: ...

    async def refresh(self, actor: AuthContext) -> AuthContext:
        """每次敏感读写与派发前重新核验当前权限，不信任 run 创建时的快照。"""


@runtime_checkable
class SecretPort(Protocol):
    async def get(self, ref: str) -> str: ...
