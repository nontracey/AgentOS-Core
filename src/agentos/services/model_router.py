"""模型路由（FR-MDL-03/04/06/07，M2）：

- 降级链（FR-MDL-03）：主 provider 失败（unavailable/timeout）按配置顺序切换备选，
  3s 内启动备选调用；fallback 事件与最终 provider 落事件，不从头重放已确认工具步骤。
- 隐私路由（FR-MDL-07）：run 数据分类 local_only / approved_private / approved_cloud；
  local_only 只允许 egress=local 的 provider，无合规端点时明确阻塞（fail-closed），
  降级不得扩大数据出域。
- 能力协商（FR-MDL-04）：capabilities 缺失的能力编排层自动避开（无 tools 能力→
  不传工具、不期待 tool_calls），不报错不假装。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.ports import ModelPort
from agentos.kernel.types import AuthContext, ModelRequest

RETRYABLE = {ErrorCode.UNAVAILABLE, ErrorCode.TIMEOUT}
DATA_CLASSES = {"local_only", "approved_private", "approved_cloud"}
_EGRESS_LOCAL_MARKERS = ("scripted", "127.0.0.1", "localhost", "ollama")


@dataclass
class ProviderSpec:
    name: str
    adapter: ModelPort
    egress: str = "cloud"  # local | cloud

    def __post_init__(self) -> None:
        if any(m in self.name.lower() for m in _EGRESS_LOCAL_MARKERS):
            self.egress = "local"


class ModelRouter:
    """provider 链 + 数据分类路由；供 Agent Loop 替代单一 model 使用。"""

    def __init__(self, providers: list[ProviderSpec], primary: str) -> None:
        if not providers:
            raise AgentOSError(ErrorCode.INVALID, "model chain empty")
        self._providers = {p.name: p for p in providers}
        self._order = [p.name for p in providers]
        if primary not in self._providers:
            raise AgentOSError(ErrorCode.INVALID, f"unknown primary provider: {primary}")
        self._primary = primary
        self._caps: dict[str, dict[str, Any]] = {}

    @property
    def chain(self) -> list[str]:
        """主 provider 在前的完整链。"""
        rest = [n for n in self._order if n != self._primary]
        return [self._primary, *rest]

    def providers_for(self, data_class: str) -> list[str]:
        """按数据分类过滤：local_only 仅 egress=local；其余不过滤（approved* 均可）。"""
        if data_class not in DATA_CLASSES:
            raise AgentOSError(ErrorCode.INVALID, f"unknown data_class: {data_class}")
        chain = self.chain
        if data_class == "local_only":
            chain = [n for n in chain if self._providers[n].egress == "local"]
        return chain

    async def capabilities(self, name: str) -> dict[str, Any]:
        if name not in self._caps:
            self._caps[name] = await self._providers[name].adapter.capabilities()
        return self._caps[name]

    async def stream_with_fallback(
        self,
        request: ModelRequest,
        actor: AuthContext,
        *,
        data_class: str,
        on_fallback: Any = None,  # Callable[[str, str, str], Awaitable[None]] (from, to, reason)
    ) -> tuple[str, list[dict[str, Any]]]:
        """沿链尝试，返回 (provider_name, events)。可重试错误自动降级；其余错误直接抛出。"""
        events = []
        chosen = ""

        async def record_provider(name: str) -> None:
            nonlocal chosen
            chosen = name

        async for event in self.stream_events(request, actor, data_class=data_class,
                                               on_fallback=on_fallback, on_provider=record_provider):
            events.append(event)
        return chosen, events

    async def stream_events(
        self, request: ModelRequest, actor: AuthContext, *, data_class: str,
        on_fallback: Any = None, on_provider: Any = None,
    ) -> AsyncIterator[dict[str, Any]]:
        chain = self.providers_for(data_class)
        if not chain:
            raise AgentOSError(ErrorCode.DENIED, "no compliant provider for data_class=" + data_class)
        for index, name in enumerate(chain):
            provider = self._providers[name]
            caps = await self.capabilities(name)
            effective_request = self._negotiate(request, caps)
            produced = False
            try:
                async for event in provider.adapter.stream(effective_request, actor):
                    if (event.get("type") == "text_delta" and event.get("delta")) or event.get("tool_calls"):
                        produced = True
                    yield event
                if on_provider is not None:
                    await on_provider(name)
                return
            except AgentOSError as exc:
                # Once output is delivered, a fallback would mix two answers or tool decisions.
                if produced or exc.code not in RETRYABLE or index == len(chain) - 1:
                    raise
                if on_fallback is not None:
                    await on_fallback(name, chain[index + 1], exc.message)
        raise AgentOSError(ErrorCode.UNAVAILABLE, "model chain exhausted")

    @staticmethod
    def _negotiate(request: ModelRequest, caps: dict[str, Any]) -> ModelRequest:
        """FR-MDL-04：缺失能力自动避开——无 tools 能力则不传工具定义。"""
        if caps.get("tools") is False and request.tools:
            return request.model_copy(update={"tools": []})
        return request
