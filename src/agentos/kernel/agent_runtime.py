"""Agent execution boundary: the engine owns reasoning, the platform owns authority."""
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from agentos.kernel.types import ToolSpec

ToolHandler = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class RuntimeRequest:
    run_id: str
    prompt: str
    workspace: str
    tenant_id: str = 'default'
    node_id: str = 'main'
    tools: list[ToolSpec] = field(default_factory=list)
    thread_id: str | None = None
    compact: bool = False


class AgentRuntimePort(Protocol):
    def stream(self, request: RuntimeRequest, handler: ToolHandler) -> AsyncIterator[dict[str, Any]]: ...
