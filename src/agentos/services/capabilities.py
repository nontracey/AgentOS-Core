"""One governed ToolPort snapshot for models, workflow discovery and dispatch.

Discovery grants no permission. Scope filtering hides capabilities a subject cannot
use; approval, grants, arguments and effects remain the runtime policy's authority.
"""
from __future__ import annotations

from typing import Any

from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.ports import ToolPort
from agentos.kernel.types import AuthContext, CallContext, ToolResult, ToolSpec


def required_scopes(spec: ToolSpec) -> list[str]:
    mapping = {"write": "write", "delete": "write", "exec": "exec", "egress": "egress"}
    return sorted({"read", *(mapping[r] for r in spec.risks if r in mapping)})


class CapabilityCatalog:
    def __init__(self, tools: ToolPort) -> None:
        self._tools = tools

    @staticmethod
    def visible(actor: AuthContext, spec: ToolSpec) -> bool:
        return (not actor.is_expired and "guest" not in actor.roles
                and all(actor.has_scope(s) for s in required_scopes(spec)))

    async def describe(self, actor: AuthContext) -> list[dict[str, Any]]:
        return [{**spec.model_dump(mode="json"), "revision": spec.revision,
                 "required_scopes": required_scopes(spec),
                 "authorization": "policy_at_dispatch",
                 "available": spec.discovery_status != "unavailable"}
                for spec in await self._tools.list_tools(actor) if self.visible(actor, spec)]

    async def list_tools(self, actor: AuthContext) -> list[ToolSpec]:
        return [spec for spec in await self._tools.list_tools(actor)
                if self.visible(actor, spec) and spec.discovery_status != "unavailable"]

    async def resolve(self, actor: AuthContext, name: str) -> ToolSpec:
        spec = await self._tools.resolve(actor, name)
        if not self.visible(actor, spec):
            raise AgentOSError(ErrorCode.DENIED, "capability is outside caller scope")
        if spec.discovery_status == "unavailable":
            raise AgentOSError(ErrorCode.UNAVAILABLE, "capability discovery failed")
        return spec

    async def call(self, name: str, args: dict[str, Any], ctx: CallContext) -> ToolResult:
        spec = await self.resolve(ctx.actor, name)
        if ctx.expected_revision and spec.revision != ctx.expected_revision:
            raise AgentOSError(ErrorCode.CONFLICT, "capability contract changed before dispatch")
        return await self._tools.call(name, args, ctx)

    async def poll(self, external_ref: str, ctx: CallContext) -> ToolResult:
        return await self._tools.poll(external_ref, ctx)

    async def cancel(self, external_ref: str, ctx: CallContext) -> ToolResult:
        return await self._tools.cancel(external_ref, ctx)

    async def underlying_recovery(self, actor: AuthContext, name: str) -> str | None:
        try:
            await self.resolve(actor, name)
        except AgentOSError as exc:
            if exc.code == ErrorCode.INVALID:
                return None
            raise
        if not hasattr(self._tools, "underlying_recovery"):
            return None
        result: str | None = await self._tools.underlying_recovery(actor, name)
        return result

    async def bind_graph(self, actor: AuthContext, definition: dict[str, Any]) -> dict[str, Any]:
        """Server fixes contracts on publication; advanced client fields cannot forge them."""
        import copy

        from pydantic import ValidationError

        from agentos.kernel.workflow_graph import FlowGraph

        bound = copy.deepcopy(definition)
        if bound.get("nodes"):
            try:
                bound = FlowGraph.model_validate(bound).model_dump(mode="json")
            except ValidationError as exc:
                raise AgentOSError(ErrorCode.INVALID, "invalid workflow graph") from exc
        for node in bound.get("nodes", []):
            config = node.get("config", {})
            if node.get("kind") == "tool":
                spec = await self.resolve(actor, str(config.get("tool", "")))
                config["capability_bindings"] = {spec.name: spec.revision}
            elif node.get("kind") == "agent":
                specs = [await self.resolve(actor, str(name)) for name in config.get("tools", [])]
                config["capability_bindings"] = {s.name: s.revision for s in specs}
        return bound
