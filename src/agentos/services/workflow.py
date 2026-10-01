"""自定义工作流（FR-WFL-01/02/03，M1 基础版）。

- 定义版本化：发布后不可变（重发同 hash 幂等返回既有版本），运行固定版本（FR-WFL-02）。
- 旧任务集兼容 TaskPlan。新图由 Temporal 持有路由、计时与重试；
  AgentOS 持有权限、节点输入和副作用账本。
- 触发器：手动（M1 基础）；定时/事件/Webhook 在后续阶段接入同一 accept 路径。
- 导出不含密钥（定义本身无凭据字段）；导入按 hash 幂等（FR-WFL-01 干净环境复现）。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError as ModelValidationError

from agentos.kernel.dto import TaskPlan
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.store import StoreProtocol
from agentos.kernel.types import AuthContext, Receipt
from agentos.kernel.workflow_graph import FlowGraph
from agentos.services.access import AccessService


def _canonical_hash(definition: dict[str, Any]) -> str:
    canonical = json.dumps(definition, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class WorkflowService:
    def __init__(self, store: StoreProtocol, access: AccessService | None = None, budget: int = 0) -> None:
        self._store = store
        self._access, self._budget = access, budget

    def publish(self, actor: AuthContext, definition: dict[str, Any]) -> dict[str, Any]:
        """发布版本化定义；同 hash 幂等。返回 {workflow_id, version, hash, reused}。"""
        if self._access:
            self._access.require_write_allowed(actor, "workflow.publish")
        if not actor.has_scope("write"):
            raise AgentOSError(ErrorCode.DENIED, "workflow publishing requires write scope")
        if definition.get("nodes"):
            try:
                graph = FlowGraph.model_validate(definition)
            except ModelValidationError as exc:
                raise AgentOSError(ErrorCode.INVALID, "invalid workflow graph") from exc
            definition = graph.model_dump(mode="json")
        else:
            tasks = definition.get("tasks")
            if not isinstance(tasks, list) or not tasks:
                raise AgentOSError(ErrorCode.INVALID, "definition.nodes or definition.tasks required")
            try:
                plan = TaskPlan.model_validate(
                    {"plan_id": definition.get("id", "wf"), "version": 1,
                     "goal_id": definition.get("id", "wf"), "goal_version": 1, "tasks": tasks})
            except ModelValidationError as exc:
                raise AgentOSError(ErrorCode.INVALID, "invalid legacy workflow plan") from exc
            definition = dict(definition)
            definition["tasks"] = [t.model_dump(mode="json") for t in plan.tasks]
        definition_hash = _canonical_hash(definition)

        wf_id = str(definition.get("id") or "wf")
        existing = self._store.find_workflow_by_hash(actor.tenant_id, wf_id, definition_hash)
        if existing is not None:
            return {"workflow_id": existing["id"], "version": existing["version"],
                    "hash": definition_hash, "reused": True}
        version = self._store.next_workflow_version_atomic(actor.tenant_id, wf_id)
        self._store.insert_workflow_definition(
            tenant_id=actor.tenant_id, id=wf_id, version=version,
            name=str(definition.get("name", wf_id)),
            definition_json=json.dumps(definition, ensure_ascii=False),
            definition_hash=definition_hash, status="published",
        )
        self._store.audit_append(
            actor.tenant_id, "workflow.published", subject=actor.subject, run_id=None,
            detail={"workflow_id": wf_id, "version": version, "hash": definition_hash},
        )
        return {"workflow_id": wf_id, "version": version, "hash": definition_hash, "reused": False}

    def start(
        self, actor: AuthContext, workflow_id: str, version: int,
        inputs: dict[str, Any], idempotency_key: str,
    ) -> dict[str, Any]:
        """按固定版本启动：创建内部 run（计划注入 payload），登记 workflow_run 映射。"""
        if self._access:
            self._access.require_write_allowed(actor, "workflow.start")
        if not isinstance(inputs, dict):
            raise AgentOSError(ErrorCode.INVALID, "workflow inputs must be an object")
        definition_row = self._store.get_workflow_definition(actor.tenant_id, workflow_id, version)
        if definition_row is None or definition_row["status"] != "published":
            raise AgentOSError(ErrorCode.INVALID, f"workflow version not published: {workflow_id}@{version}")
        definition = json.loads(definition_row["definition_json"])
        from jsonschema import SchemaError, ValidationError, validate

        try:
            validate(inputs, definition.get("input_schema", {}))
        except (ValidationError, SchemaError) as exc:
            raise AgentOSError(ErrorCode.INVALID, "workflow inputs do not match published schema") from exc
        execution = ({"workflow_graph": definition} if definition.get("nodes") else
                     {"plan": {"plan_id": f"wf_{workflow_id}", "version": 1,
                               "goal_id": workflow_id, "goal_version": 1, "tasks": definition["tasks"]}})
        receipt: Receipt = self._store.accept_once(
            envelope_json=json.dumps({
                "id": "wf", "thread_id": None,
                "actor": json.loads(actor.model_dump_json()),
                "payload": {"goal": f"workflow:{workflow_id}@{version}", "inputs": inputs,
                            "workflow_version": version, **execution},
            }, ensure_ascii=False),
            envelope_id=f"wf_{workflow_id}_{version}",
            idempotency_key=idempotency_key,
            payload_hash=_canonical_hash({"workflow_id": workflow_id, "version": version, "inputs": inputs}),
            tenant_id=actor.tenant_id, subject=actor.subject, channel="workflow",
            run_budget_microusd=self._budget, attachment_ids=[],
        )
        wf_run_id = f"wfr_{receipt.run_id[4:]}"
        if self._store.get_workflow_run(actor.tenant_id, wf_run_id) is None:
            self._store.insert_workflow_run(
                tenant_id=actor.tenant_id, id=wf_run_id, workflow_id=workflow_id,
                definition_version=version, internal_run_id=receipt.run_id,
            )
        self._store.audit_append(
            actor.tenant_id, "workflow.started", subject=actor.subject, run_id=receipt.run_id,
            detail={"workflow_id": workflow_id, "version": version, "wf_run_id": wf_run_id},
        )
        return {"wf_run_id": wf_run_id, "internal_run_id": receipt.run_id,
                "replayed": receipt.replayed}

    def status(self, actor: AuthContext, wf_run_id: str) -> dict[str, Any]:
        row = self._store.get_workflow_run(actor.tenant_id, wf_run_id)
        if row is None or row["internal_run_id"] is None:
            raise AgentOSError(ErrorCode.INVALID, f"workflow run not found: {wf_run_id}")
        internal = self._store.get_run(actor.tenant_id, str(row["internal_run_id"]))
        if internal is None:
            raise AgentOSError(ErrorCode.INVALID, "internal run missing")
        if self._access:
            self._access.require(actor, "task.read", kind="task", resource_id=internal.id)
        elif internal.subject != actor.subject:
            raise AgentOSError(ErrorCode.DENIED, "workflow belongs to another subject")
        checkpoint = json.loads(internal.checkpoint_json) if internal.checkpoint_json else {}
        return {
            "wf_run_id": wf_run_id, "workflow_id": row["workflow_id"],
            "definition_version": row["definition_version"],
            "state": internal.state.value,
            "delivery": checkpoint.get("delivery"),
        }

    def export(self, actor: AuthContext, workflow_id: str, version: int) -> dict[str, Any]:
        row = self._store.get_workflow_definition(actor.tenant_id, workflow_id, version)
        if row is None:
            raise AgentOSError(ErrorCode.INVALID, f"workflow not found: {workflow_id}@{version}")
        return {"definition": json.loads(row["definition_json"]), "hash": row["definition_hash"]}

    def import_definition(self, actor: AuthContext, payload: dict[str, Any]) -> dict[str, Any]:
        definition = payload.get("definition")
        if not isinstance(definition, dict):
            raise AgentOSError(ErrorCode.INVALID, "payload.definition required")
        imported_hash = payload.get("hash")
        canonical = (FlowGraph.model_validate(definition).model_dump(mode="json")
                     if definition.get("nodes") else definition)
        if imported_hash is not None and imported_hash != _canonical_hash(canonical):
            raise AgentOSError(
                ErrorCode.CONFLICT,
                "imported workflow hash mismatch",
            )
        return self.publish(actor, definition)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()
