"""Expose immutable workflows as approved asynchronous launch capabilities.

The receipt is a launch result, not workflow completion. Temporal still executes
the graph. One level of delegation prevents recursive job/budget expansion.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from typing import Any

from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import AuthContext, CallContext, ToolResult, ToolSpec
from agentos.services.workflow import WorkflowService


class WorkflowTools:
    def __init__(self, service: WorkflowService, engine_ready: Callable[[], bool]) -> None:
        self.service, self.engine_ready = service, engine_ready

    async def list_tools(self, actor: AuthContext) -> list[ToolSpec]:
        specs = [ToolSpec(name="workflow.status", version="1",
            description=("Read an authorized launched workflow's state and delivery; "
                         "launch receipt is not completion."),
            input_schema={"type": "object", "properties": {"wf_run_id": {"type": "string"}},
                          "required": ["wf_run_id"], "additionalProperties": False},
            risks=["read"], recovery="pure", timeout_ms=1000)]
        for row in self.service._store.list_workflow_definitions(actor.tenant_id):
            if row["status"] != "published" or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", row["id"]):
                continue
            definition = self.service.export(actor, row["id"], row["version"])["definition"]
            # Contract hash pins both node effects and the workflow version.
            specs.append(ToolSpec(name=f"workflow.{row['id']}.v{row['version']}", version=row["definition_hash"],
                description=(f"Launch published workflow: {row['name']} (v{row['version']}). "
                             "Returns a task handle; use workflow.status to inspect it. Allocates from the current "
                             "task budget (at most half of the remainder); "
                             "node effects still require their own approvals."),
                input_schema={"type": "object", "properties": {
                              "inputs": definition.get("input_schema") or {"type": "object"}},
                              "required": ["inputs"], "additionalProperties": False},
                risks=["write"], recovery="idempotency_key", timeout_ms=1000,
                discovery_status="local" if not definition.get("nodes") or self.engine_ready() else "unavailable"))
        return specs

    async def resolve(self, actor: AuthContext, name: str) -> ToolSpec:
        for spec in await self.list_tools(actor):
            if spec.name == name:
                return spec
        raise AgentOSError(ErrorCode.INVALID, "published workflow capability not found")

    async def call(self, name: str, args: dict[str, Any], ctx: CallContext) -> ToolResult:
        spec = await self.resolve(ctx.actor, name)
        if spec.discovery_status == "unavailable":
            raise AgentOSError(ErrorCode.UNAVAILABLE, "workflow engine unavailable")
        if name == "workflow.status":
            result = self.service.status(ctx.actor, args["wf_run_id"])
        else:
            _, workflow_id, version = name.split(".")
            key = "workflow-child-" + hashlib.sha256((ctx.run_id + "\0" + ctx.idempotency_key).encode()).hexdigest()
            result = self.service.start(ctx.actor, workflow_id, int(version[1:]), args["inputs"], key,
                                       parent_run_id=ctx.run_id, parent_reservation_id=key)
            result["launch_only"] = True
            child = self.service._store.get_run(ctx.actor.tenant_id, result["internal_run_id"])
            result["allocated_budget_microusd"] = child.budget_microusd if child else 0
        return ToolResult(call_id=ctx.call_id, status="completed", value=result)
