"""One bounded activity workflow; all effects use the shared executor and immutable Assets.

Research/design/review are fixed roles, not a free workflow editor. Supplements resume
this same run atomically, retain design-1, and permit at most two targeted research repairs.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from agentos.kernel.dto import TaskPlan
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.states import RunState, StepState
from agentos.kernel.store import Lease, RunRecord
from agentos.kernel.types import AuthContext
from agentos.runtime.executor import execute_tool_step
from agentos.services.access import AccessService
from agentos.services.ingress import IngressService, canonical_hash
from agentos.services.template_runner import TemplateRunner


class CollaborationRunner:
    def __init__(self, report: TemplateRunner, access: AccessService, ingress: IngressService) -> None:
        self._report, self._access, self._ingress = report, access, ingress
        self._deps = report._deps

    def _save(self, run: RunRecord, cp: dict[str, Any]) -> None:
        self._deps.store.save_checkpoint(run.tenant_id, run.id, json.dumps(cp, ensure_ascii=False))

    @staticmethod
    def _plan(run: RunRecord, version: int, tool: str) -> dict[str, Any]:
        research = "research-1" if version == 1 else f"research-{version}"
        tasks = [
            {"task_id": "research-1", "role_id": "research", "input_asset_ids": [], "output_kinds": ["research"]},
            {
                "task_id": "design-1",
                "role_id": "design",
                "required_os": "windows",
                "required_capabilities": [tool],
                "output_kinds": ["image"],
            },
            {
                "task_id": "aggregate-1",
                "role_id": "research",
                "depends_on": ["research-1", "design-1"],
                "output_kinds": ["proposal"],
            },
            {"task_id": "review-1", "role_id": "review", "depends_on": ["aggregate-1"], "output_kinds": ["issues"]},
        ]
        if version > 1:
            tasks += [
                {"task_id": research, "role_id": "research", "depends_on": ["review-1"], "output_kinds": ["research"]},
                {
                    "task_id": f"review-{version}",
                    "role_id": "review",
                    "depends_on": [research, "design-1"],
                    "output_kinds": ["issues"],
                },
                {
                    "task_id": "deliver-1",
                    "role_id": "research",
                    "depends_on": [f"review-{version}"],
                    "output_kinds": ["report", "image"],
                },
            ]
        if version == 1:
            tasks.append({"task_id": "deliver-1", "role_id": "research", "depends_on": ["review-1"],
                          "output_kinds": ["report", "image"]})
        return TaskPlan.model_validate(
            {"plan_id": f"activity:{run.id}", "version": version, "goal_id": run.id, "goal_version": 1, "tasks": tasks}
        ).model_dump(mode="json")

    async def execute(self, run: RunRecord, lease: Lease, *, finish_run: bool = True) -> RunRecord:
        store, tenant = self._deps.store, run.tenant_id
        payload = json.loads(run.envelope_json)["payload"]
        actor = AuthContext.model_validate(json.loads(run.envelope_json)["actor"])
        cp = json.loads(run.checkpoint_json or "{}")
        state = cp.setdefault("collaboration", {"round": 0, "tasks": {}})
        tasks = state["tasks"]
        version = state["round"] + 1
        specs = await self._deps.tools.list_tools(actor)
        if not state.get("design_tool"):
            choices = []
            for node in store.list_nodes(tenant):
                if node["status"] != "paired" or str(node["os"]).lower() not in {"windows", "win32"}:
                    continue
                for spec in specs:
                    if spec.name.startswith(
                        "node_" + str(node["node_id"]).replace("-", "_") + "_"
                    ) and spec.name.endswith("comfyui_generate"):
                        choices.append((node, spec))
            if len(choices) != 1:
                return self._report._fail(run, "协作需要一个已配对且具有视觉生成能力的 Windows 节点")
            node, spec = choices[0]
            state.update(design_tool=spec.name, design_tool_version=spec.version, node_id=node["node_id"])
        pinned = next((s for s in specs if s.name == state["design_tool"]), None)
        if pinned is None or pinned.version != state["design_tool_version"]:
            return self._report._fail(run, "协作节点能力或工具版本已变化，请核对后重新发起任务")
        plan = self._plan(run, version, state["design_tool"])
        if cp.get("active_plan", {}).get("version") != version:
            cp["active_plan"] = plan
            store.event_append(
                tenant,
                run.id,
                "plan.version_created",
                {
                    "plan": plan,
                    "reason": "initial" if version == 1 else state["reason"],
                    "reused": ["design-1"] if version > 1 else [],
                },
            )
            self._save(run, cp)

        materials = [*payload.get("materials", []), *state.get("additional_materials", [])]
        loaded = []
        for material in materials:
            outcome = await execute_tool_step(
                self._deps,
                store.get_run(tenant, run.id) or run,
                f"collab_read_{material['asset_id']}",
                "material.read",
                {"asset_id": material["asset_id"]},
                lease,
            )
            if outcome.state != StepState.COMPLETED:
                return store.get_run(tenant, run.id) or run
            loaded.append({**material, "content": (outcome.result.value if outcome.result else {}).get("content", "")})
        if not state.get("skill_body"):
            outcome = await execute_tool_step(
                self._deps,
                store.get_run(tenant, run.id) or run,
                "collab_skill",
                "skill.read",
                {"skill_id": "material-report"},
                lease,
            )
            if outcome.state != StepState.COMPLETED:
                return self._report._fail(run, "协作技能读取失败")
            state["skill_body"] = (outcome.result.value if outcome.result else {}).get("body", "")
            self._save(run, cp)

        async def generate(task_id: str, instruction: str) -> bool:
            if task_id in tasks:
                return True
            store.event_append(
                tenant,
                run.id,
                "plan.task_started",
                {
                    "task_id": task_id,
                    "role": "design" if task_id == "design-prompt" else "research",
                    "plan_version": version,
                    "node_id": "local",
                    "input_asset_ids": [m["asset_id"] for m in materials],
                    "skill_version": "1",
                },
            )
            text = await self._report._generate(
                store.get_run(tenant, run.id) or run,
                actor,
                run.goal_text + "。" + instruction,
                loaded,
                skill_body=state["skill_body"],
                generation=f"collaboration:{task_id}", lease=lease,
            )
            if text is None:
                return False
            tasks[task_id] = {"state": "completed", "output": text, "sha256": hashlib.sha256(text.encode()).hexdigest()}
            store.event_append(
                tenant,
                run.id,
                "plan.task_finished",
                {"task_id": task_id, "state": "completed", "output_sha256": tasks[task_id]["sha256"]},
            )
            self._save(run, cp)
            return True

        research = f"research-{version}"
        if not await generate(research, "研究角色：整理授权材料中的活动事实、依据与缺失项，不补造事实。"):
            return store.get_run(tenant, run.id) or run
        if "design-1" not in tasks:
            if not await generate(
                "design-prompt",
                "设计角色：给出春季户外活动主视觉的构图、颜色和画面描述，建议必须标明建议，不在画面写日期和预算。",
            ):
                return store.get_run(tenant, run.id) or run
            state.setdefault(
                "design_arguments",
                {
                    "prompt": tasks["design-prompt"]["output"],
                    "model": "z_image",
                    "width": 768,
                    "height": 768,
                    "seed": 42,
                },
            )
            self._save(run, cp)
            store.event_append(
                tenant,
                run.id,
                "plan.task_started",
                {
                    "task_id": "design-1",
                    "role": "design",
                    "node_id": state["node_id"],
                    "tool": state["design_tool"],
                    "tool_version": pinned.version,
                    "plan_version": version,
                },
            )
            outcome = await execute_tool_step(
                self._deps,
                store.get_run(tenant, run.id) or run,
                "design-1:generate",
                state["design_tool"],
                state["design_arguments"],
                lease,
            )
            if outcome.state != StepState.COMPLETED:
                return store.get_run(tenant, run.id) or run
            value = outcome.result.value if outcome.result else {}
            tasks["design-1"] = {"state": "completed", "output": value}
            store.event_append(
                tenant, run.id, "plan.task_finished", {"task_id": "design-1", "state": "completed", "output": value}
            )
            self._save(run, cp)
        visual = tasks["design-1"]["output"]
        if visual.get("delivery") != "registered":
            received = next(
                (
                    a
                    for a in store.list_assets(tenant, subject=run.subject)
                    if a.get("run_id") == run.id
                    and a.get("sha256") == visual.get("sha256")
                    and a.get("source_node") == state["node_id"]
                ),
                None,
            )
            if received is None:
                store.transition_run(
                    tenant,
                    run.id,
                    {RunState.RUNNING},
                    RunState.PAUSED,
                    event_detail={"reason": "visual_delivery_pending", "generation_reused": True},
                )
                return store.get_run(tenant, run.id) or run
            visual.update(asset_id=received["id"], delivery="registered")
            self._save(run, cp)
        if version == 1 and not await generate(
            "aggregate-1", "汇总角色：形成活动方案，复用已生成视觉，不再次生成图片；建议和材料事实分开。"
        ):
            return store.get_run(tenant, run.id) or run
        review_id = f"review-{version}"
        if review_id not in tasks:
            source = "\n".join(m["content"] for m in loaded)
            issues = []
            if not re.search(r"\d{4}[-年]\d{1,2}[-月]\d{1,2}", source):
                issues.append({"code": "missing_date_source", "message": "缺少活动日期依据", "target": research})
            if not re.search(r"\d+(?:\.\d+)?\s*(?:万|亿)?\s*元", source):
                issues.append({"code": "missing_budget_source", "message": "缺少活动预算依据", "target": research})
            tasks[review_id] = {"state": "completed", "issues": issues}
            store.event_append(
                tenant,
                run.id,
                "plan.task_finished",
                {"task_id": review_id, "role": "review", "state": "completed", "issues": issues, "deterministic": True},
            )
            self._save(run, cp)
        issues = tasks[review_id]["issues"]
        if issues:
            if state["round"] >= 2:
                return self._report._fail(run, "协作已达到两轮返修上限，仍缺必要来源")
            store.transition_run(
                tenant,
                run.id,
                {RunState.RUNNING},
                RunState.AWAITING_INPUT,
                event_detail={
                    "issues": issues,
                    "rework_target": research,
                    "reused": ["design-1"],
                    "round": state["round"],
                },
            )
            return store.get_run(tenant, run.id) or run
        # Final report generation/validation/write approval and registration reuse the existing template.
        result: RunRecord = await self._report.execute(
            store.get_run(tenant, run.id) or run, lease, finish_run=finish_run)
        if (result.state == RunState.SUCCEEDED
                or not finish_run and json.loads(result.checkpoint_json or "{}").get("delivery")):
            # TemplateRunner has updated the checkpoint; preserve its refs and registration.
            final_cp = json.loads(result.checkpoint_json or "{}")
            final_cp["collaboration"]["tasks"]["deliver-1"] = {"state": "completed"}
            self._save(result, final_cp)
            store.event_append(
                tenant,
                run.id,
                "plan.task_finished",
                {
                    "task_id": "deliver-1",
                    "state": "completed",
                    "reused_visual": tasks["design-1"]["output"],
                    "plan_version": version,
                },
            )
        return result

    def supplement(self, actor: AuthContext, run_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self._access.require_write_allowed(actor, "task.supplement")
        self._access.require(actor, "task.read", kind="task", resource_id=run_id)
        store = self._deps.store
        run = store.get_run(actor.tenant_id, run_id)
        if run is None:
            raise AgentOSError(ErrorCode.INVALID, "task not found")
        cp = json.loads(run.checkpoint_json or "{}")
        state = cp.get("collaboration")
        if state is None:
            raise AgentOSError(ErrorCode.INVALID, "task does not accept collaboration supplements")
        key, reason = str(body.get("idempotency_key") or ""), str(body.get("message") or "").strip()
        ids = body.get("material_asset_ids")
        if (
            not key
            or not reason
            or len(reason) > 2000
            or not isinstance(ids, list)
            or not ids
            or not all(isinstance(i, str) for i in ids)
        ):
            raise AgentOSError(ErrorCode.INVALID, "supplement requires a stable key, reason and materials")
        digest = canonical_hash({"message": reason, "material_asset_ids": ids})
        prior = state.get("supplements", {}).get(key)
        if prior:
            if prior != digest:
                raise AgentOSError(ErrorCode.CONFLICT, "supplement key reused with different input")
            return {"run_id": run_id, "accepted": True, "replayed": True}
        if run.state != RunState.AWAITING_INPUT or state["round"] >= 2:
            raise AgentOSError(ErrorCode.CONFLICT, "task is not awaiting a bounded supplement")
        original = json.loads(run.envelope_json)["payload"].get("materials", [])
        all_ids = list(dict.fromkeys([m["asset_id"] for m in original + state.get("additional_materials", [])] + ids))
        resolved = self._ingress._resolve_materials(actor, all_ids)
        state["additional_materials"] = [m for m in resolved if m["asset_id"] not in {m["asset_id"] for m in original}]
        state["round"] += 1
        state["reason"] = reason
        state.setdefault("supplements", {})[key] = digest
        if not store.resume_with_checkpoint(
            actor.tenant_id,
            run_id,
            run.version,
            json.dumps(cp, ensure_ascii=False),
            {
                "reason": reason,
                "round": state["round"],
                "target": f"research-{state['round'] + 1}",
                "reused": ["design-1"],
            },
        ):
            raise AgentOSError(ErrorCode.CONFLICT, "task changed while accepting supplement; retry same key")
        return {"run_id": run_id, "accepted": True, "replayed": False}
