"""规划 / 角色 / 交付（FR-ORC-03~05、09~12，M1）。

- RoleRegistry：岗位说明书用类 AGENTS.md 开放约定（markdown + frontmatter），
  换底层框架仍可复用（FR-ORC-05）；执行权限来自 Policy，不来自说明书自我声明。
- PlanRunner：确定性执行已验证的 TaskPlan DAG——每任务一个受限 ReAct 子循环，
  复用同一账本/Policy/审批/审计（不建第二套重试引擎）；无依赖任务并行（FR-ORC-04），
  依赖顺序即流水线；reviewer 角色任务构成评审形态。
- CheckEngine：确定性验收检查（exists/contains/output 正则），内容判断保留用户验收
  （FR-ORC-12）。
- 交付：DeliveryReport（任务状态、检查结果、产物、成本、未决项）持久化到事件。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentos.kernel.dto import TaskPlan
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.ports import ModelPort
from agentos.kernel.states import RunState, StepState
from agentos.kernel.store import Lease, RunRecord
from agentos.kernel.types import (
    AuthContext,
    ChatMessage,
    ModelRequest,
    ModelToolCall,
    ToolSpec,
    Usage,
)
from agentos.runtime.executor import RuntimeDeps, args_hash, execute_tool_step
from agentos.services.observability import prompt_evidence

DEFAULT_ROLES: dict[str, dict[str, Any]] = {
    "worker": {
        "description": "执行角色：完成任务所需的全部本地工具",
        "allowed_tools": ["math.add", "clock.now", "fs.write_text", "log.append"],
        "system_prompt": "你是执行角色（worker）：完成任务并在需要时调用工具。",
    },
    "reviewer": {
        "description": "评审角色：只读检查上游产出",
        "allowed_tools": ["math.add", "clock.now"],
        "system_prompt": "你是评审角色（reviewer）：只读检查上游产出，输出通过/不通过与理由。",
    },
}


class RoleRegistry:
    """角色目录：data/roles/*.md（frontmatter: role_id/allowed_tools/description，
    正文为岗位说明书）；缺省内置 worker/reviewer。"""

    def __init__(self, roles_dir: Path | None = None) -> None:
        self._roles = dict(DEFAULT_ROLES)
        if roles_dir is not None and roles_dir.is_dir():
            for md in sorted(roles_dir.glob("*.md")):
                text = md.read_text(encoding="utf-8")
                role_id = md.stem
                allowed: list[str] = []
                description = ""
                prompt_lines: list[str] = []
                in_front = False
                for line in text.splitlines():
                    if line.strip() == "---":
                        in_front = not in_front
                        continue
                    if in_front:
                        if line.startswith("allowed_tools:"):
                            allowed = [t.strip() for t in line.split(":", 1)[1].split(",") if t.strip()]
                        elif line.startswith("description:"):
                            description = line.split(":", 1)[1].strip()
                    else:
                        prompt_lines.append(line)
                self._roles[role_id] = {
                    "allowed_tools": allowed,
                    "description": description,
                    "system_prompt": "\n".join(prompt_lines).strip() or f"角色：{role_id}",
                }

    def get(self, role_id: str) -> dict[str, Any]:
        role = self._roles.get(role_id)
        if role is None:
            raise AgentOSError(ErrorCode.INVALID, f"unknown role: {role_id}")
        return role

    def list(self) -> list[str]:
        return sorted(self._roles)


class CheckEngine:
    """确定性验收检查：exists:<path> | contains:<text> | output:<regex>。"""

    def __init__(self, sandbox: Path) -> None:
        self._sandbox = sandbox

    def run(self, checks: list[str], output_text: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for check in checks or []:
            kind, _, arg = check.partition(":")
            arg = arg.strip()
            passed, detail = False, ""
            if kind == "exists":
                target = (self._sandbox / arg).resolve()
                passed = target.is_relative_to(self._sandbox.resolve()) and target.is_file()
                detail = str(target)
            elif kind == "contains":
                passed = arg in output_text
                detail = arg
            elif kind == "output":
                passed = re.search(arg, output_text) is not None
                detail = arg
            else:
                passed, detail = False, f"unknown check kind: {kind}"
            results.append({"check": check, "passed": passed, "detail": detail})
        return results


@dataclass
class TaskOutcome:
    task_id: str
    state: str  # completed / failed / uncertain / blocked
    output: str = ""
    checks: list[dict[str, Any]] | None = None
    approval_id: str | None = None


class PlanRunner:
    def __init__(
        self,
        deps: RuntimeDeps,
        model: ModelPort,
        roles: RoleRegistry,
        *,
        router: Any = None,
        input_rate: int = 0,
        output_rate: int = 0,
        token_budget: int = 64000,
    ) -> None:
        self._deps = deps
        self._model = model
        self._roles = roles
        self._router = router
        self._input_rate = input_rate
        self._output_rate = output_rate
        self._token_budget = token_budget
        self._engine_driver: Any = None

    async def drive_plan(self, run: RunRecord, lease: Lease) -> RunRecord:
        """执行 run.envelope 中声明的计划（payload.plan）。同一 runId 可恢复。"""
        store = self._deps.store
        tenant = run.tenant_id
        envelope = json.loads(run.envelope_json)
        payload = envelope.get("payload", {})
        plan = TaskPlan.model_validate(payload["plan"])
        checkpoint = json.loads(run.checkpoint_json) if run.checkpoint_json else {}
        tasks_state: dict[str, dict[str, Any]] = checkpoint.setdefault("plan_tasks", {})

        # 角色目录前检：未知角色确定性失败（FR-ORC-11，模型不可发明角色）
        try:
            for t in plan.tasks:
                self._roles.get(t.role_id)
        except AgentOSError as exc:
            store.transition_run(
                tenant,
                run.id,
                {RunState.RUNNING},
                RunState.FAILED,
                error_json=json.dumps({"code": "denied", "message": exc.message}, ensure_ascii=False),
            )
            self._deps.audit.record(
                tenant,
                "policy.denied",
                subject=run.subject,
                run_id=run.id,
                detail={"reason": exc.message},
            )
            return store.get_run(tenant, run.id) or run

        actor = self._actor(run)
        specs_all = await self._deps.tools.list_tools(actor)

        # 拓扑分层（Kahn levels）：层内并行（有界），层间顺序即流水线
        levels = self._plan_levels(plan)
        unresolved: list[str] = []

        for level in levels:
            runnable = [t for t in level if tasks_state.get(t.task_id, {}).get("state") not in {"completed"}]
            if not runnable:
                continue
            concurrency = 1 if self._engine_driver is not None else max(1, self._deps.concurrency)
            batches = [runnable[i : i + concurrency] for i in range(0, len(runnable), concurrency)]
            for batch in batches:
                outcomes = await asyncio_gather(
                    *[self._execute_task(run, lease, t, specs_all, tasks_state, checkpoint) for t in batch]
                )
                # 先收集全部 outcome 再统一裁决（P1-7：不丢弃兄弟任务结果）
                blocked_outcome = None
                for outcome in outcomes:
                    tasks_state[outcome.task_id] = {
                        "state": outcome.state,
                        "output": outcome.output,
                        "checks": outcome.checks,
                    }
                    store.event_append(
                        tenant,
                        run.id,
                        "plan.task_finished",
                        {"task_id": outcome.task_id, "state": outcome.state, "checks": outcome.checks},
                    )
                    if outcome.state in {"failed", "uncertain"}:
                        unresolved.append(outcome.task_id)
                    elif outcome.state == "blocked":
                        blocked_outcome = outcome

                self._save(run, checkpoint)
                # 整批处理完再决定是否阻塞（不丢兄弟任务状态）
                if blocked_outcome:
                    store.transition_run(tenant, run.id, {RunState.RUNNING}, RunState.AWAITING_APPROVAL)
                    return store.get_run(tenant, run.id) or run

            if unresolved:
                break

        total = len(plan.tasks)
        completed = sum(1 for t in plan.tasks if tasks_state.get(t.task_id, {}).get("state") == "completed")
        delivery = {
            "plan_id": plan.plan_id,
            "plan_version": plan.version,
            "tasks": [
                {
                    "task_id": t.task_id,
                    "state": tasks_state.get(t.task_id, {}).get("state"),
                    "checks": tasks_state.get(t.task_id, {}).get("checks"),
                }
                for t in plan.tasks
            ],
            "completed": completed,
            "total": total,
            "unresolved": unresolved,
            "cost": store.budget_totals(tenant, run.id),
        }
        checkpoint["delivery"] = delivery
        self._save(run, checkpoint)

        if unresolved or completed < total:
            store.transition_run(
                tenant,
                run.id,
                {RunState.RUNNING},
                RunState.FAILED,
                error_json=json.dumps({"code": "plan_incomplete", "unresolved": unresolved}),
                event_detail={"delivery": delivery},
            )
        else:
            store.transition_run(
                tenant,
                run.id,
                {RunState.RUNNING},
                RunState.SUCCEEDED,
                event_detail={"delivery": delivery},
            )
        return store.get_run(tenant, run.id) or run

    # ------------------------------------------------------------ task exec
    async def _execute_task(
        self,
        run: RunRecord,
        lease: Lease,
        task: Any,
        specs_all: list[ToolSpec],
        tasks_state: dict[str, dict[str, Any]],
        checkpoint: dict[str, Any],
    ) -> TaskOutcome:
        store, tenant = self._deps.store, run.tenant_id
        role = self._roles.get(task.role_id)
        allowed = set(role["allowed_tools"])

        def permitted(name: str) -> bool:
            return name in allowed or any(name.startswith(f"{p}:") for p in allowed)

        specs = [s for s in specs_all if permitted(s.name)]
        done_outputs = {
            tid: tasks_state[tid].get("output", "")
            for tid in task.depends_on
            if tasks_state.get(tid, {}).get("state") == "completed"
        }
        if self._engine_driver is not None:
            prompt = (role["system_prompt"] + "\nOriginal goal: " + str(run.goal_text) + "\nTask: "
                      + json.dumps(task.model_dump(mode="json"), ensure_ascii=False)
                      + "\nWorkflow inputs: "
                      + json.dumps(json.loads(run.envelope_json).get("payload", {}).get("inputs", {}))
                      + "\nDependency outputs: " + json.dumps(done_outputs, ensure_ascii=False))
            result = await self._engine_driver.execute(run, lease, node_id="plan-" + task.task_id,
                                                       goal=prompt, tools=specs, finish_run=False)
            nodes = json.loads(result.checkpoint_json or "{}").get("runtime_nodes", {})
            checkpoint["runtime_nodes"] = nodes
            node = nodes.get("plan-" + task.task_id, {})
            if node.get("state") != "completed":
                return TaskOutcome(task.task_id, "uncertain" if result.state == RunState.PAUSED else "blocked")
            content = str(node.get("answer", ""))
            checks = CheckEngine(self._deps_tools_sandbox()).run(task.acceptance_checks, content)
            return TaskOutcome(task.task_id, "completed" if all(c["passed"] for c in checks) else "failed",
                               output=content, checks=checks)
        context = checkpoint.setdefault("plan_contexts", {}).setdefault(
            task.task_id,
            {
                "messages": [
                    {"role": "system", "content": role["system_prompt"]},
                    {
                        "role": "user",
                        "content": (
                            f"[[task:{task.task_id}]] 原目标：{run.goal_text}\n"
                            f"任务：{json.dumps(task.model_dump(mode='json'), ensure_ascii=False)}\n"
                            f"直接依赖输出：{json.dumps(done_outputs, ensure_ascii=False)}"
                        ),
                    },
                ],
                "turn": 0,
            },
        )
        self._save(run, checkpoint)
        while True:
            messages = [ChatMessage.model_validate(m) for m in context["messages"]]
            last_assistant = max((i for i, m in enumerate(messages) if m.role == "assistant"), default=-1)
            pending = messages[last_assistant] if last_assistant >= 0 else None
            responded = {m.tool_call_id for m in messages[last_assistant + 1 :] if m.role == "tool"}
            calls = [c for c in pending.tool_calls if c.id not in responded] if pending else []
            if not calls:
                context["turn"] += 1
                turn = context["turn"]
                if turn > 16 or checkpoint.get("plan_tokens", 0) >= self._token_budget:
                    return TaskOutcome(task.task_id, "failed", output="task loop budget exhausted")
                call_id = f"{run.id}:plan:{task.task_id}:turn{turn}"
                request = ModelRequest(messages=messages, tools=specs, max_output_tokens=1024)
                estimate = (
                    sum(len(m.content.encode("utf-8")) for m in messages) * self._input_rate + 1024 * self._output_rate
                ) // 1000
                content, tool_calls, usage = "", [], None
                try:
                    self._deps.budget.reserve(tenant, run.id, call_id, estimate)
                    store.event_append(tenant, run.id, "model.request", prompt_evidence(messages, call_id))
                    store.event_append(tenant, run.id, "answer.start", {"call_id": call_id, "task_id": task.task_id})
                    data_class = str(
                        json.loads(run.envelope_json).get("payload", {}).get("data_class", "approved_private")
                    )
                    stream = (
                        self._router.stream_events(request, run_actor(run), data_class=data_class)
                        if self._router
                        else self._model.stream(request, run_actor(run))
                    )
                    async for event in stream:
                        if event["type"] == "usage":
                            usage = event["usage"]
                        elif event["type"] == "text_delta":
                            content += event["delta"]
                            store.event_append(
                                tenant,
                                run.id,
                                "answer.delta",
                                {
                                    "call_id": call_id,
                                    "task_id": task.task_id,
                                    "delta": event["delta"],
                                },
                            )
                        elif event["type"] == "assistant_message":
                            content = event.get("content", "") or content
                            tool_calls = [ModelToolCall.model_validate(c) for c in event.get("tool_calls", [])]
                    usage = Usage.model_validate(usage) if usage is not None else Usage(input_tokens=0, output_tokens=0)
                    actual = (usage.input_tokens * self._input_rate + usage.output_tokens * self._output_rate) // 1000
                    self._deps.budget.settle(tenant, call_id, actual)
                    checkpoint["plan_tokens"] = checkpoint.get("plan_tokens", 0) + (
                        usage.input_tokens + usage.output_tokens
                    )
                    store.event_append(
                        tenant, run.id, "model.usage", {"call_id": call_id, **usage.model_dump(mode="json")}
                    )
                    store.event_append(tenant, run.id, "answer.end", {"call_id": call_id, "task_id": task.task_id})
                except AgentOSError as exc:
                    self._deps.budget.release(tenant, call_id)
                    return TaskOutcome(task.task_id, "failed", output=f"model call failed: {exc.message}")
                if not tool_calls:
                    checks = CheckEngine(self._deps_tools_sandbox()).run(task.acceptance_checks, content)
                    return TaskOutcome(
                        task.task_id,
                        "completed" if all(c["passed"] for c in checks) else "failed",
                        output=content,
                        checks=checks,
                    )
                context["messages"].append(
                    ChatMessage(role="assistant", content=content, tool_calls=tool_calls).model_dump(mode="json")
                )
                self._save(run, checkpoint)  # Persist exact approved arguments before the first effect.
                calls = tool_calls
            assistant_entry = next(m for m in reversed(context["messages"]) if m["role"] == "assistant")
            all_calls = [ModelToolCall.model_validate(c) for c in assistant_entry["tool_calls"]]
            for call in calls:
                index = next(i for i, c in enumerate(all_calls) if c.id == call.id)
                step_id = f"{task.task_id}:turn{context['turn']}:{index}:{call.name}"
                if not permitted(call.name):
                    self._deps.audit.record(
                        tenant,
                        "policy.denied",
                        subject=run.subject,
                        run_id=run.id,
                        detail={"tool": call.name, "role": task.role_id, "reason": "outside role allowlist"},
                    )
                    return TaskOutcome(
                        task.task_id, "failed", output=f"tool {call.name} not allowed for role {task.role_id}"
                    )
                outcome = await execute_tool_step(
                    self._deps, store.get_run(tenant, run.id) or run, step_id, call.name, call.arguments, lease
                )
                if outcome.state == StepState.AWAITING_APPROVAL:
                    return TaskOutcome(task.task_id, "blocked", approval_id=outcome.approval_id)
                if outcome.state != StepState.COMPLETED:
                    return TaskOutcome(
                        task.task_id, outcome.state.value, output=json.dumps(outcome.error or {}, ensure_ascii=False)
                    )
                context["messages"].append(
                    ChatMessage(
                        role="tool",
                        content=json.dumps(outcome.result.value if outcome.result else {}, ensure_ascii=False),
                        tool_call_id=call.id,
                    ).model_dump(mode="json")
                )
                self._save(run, checkpoint)

    # -------------------------------------------------------------- helpers
    def _deps_tools_sandbox(self) -> Path:
        local = getattr(self._deps.tools, "_local", None) or self._deps.tools
        return Path(getattr(local, "_sandbox", "."))

    def _actor(self, run: RunRecord) -> AuthContext:
        envelope = json.loads(run.envelope_json)
        return AuthContext.model_validate(envelope["actor"])

    def _save(self, run: RunRecord, checkpoint: dict[str, Any]) -> None:
        self._deps.store.save_checkpoint(run.tenant_id, run.id, json.dumps(checkpoint, ensure_ascii=False))

    @staticmethod
    def _plan_levels(plan: TaskPlan) -> list[list[Any]]:
        indegree = {t.task_id: 0 for t in plan.tasks}
        dependents: dict[str, list[str]] = {t.task_id: [] for t in plan.tasks}
        by_id = {t.task_id: t for t in plan.tasks}
        for t in plan.tasks:
            for dep in t.depends_on:
                indegree[t.task_id] += 1
                dependents[dep].append(t.task_id)
        current = [tid for tid, deg in indegree.items() if deg == 0]
        levels: list[list[Any]] = []
        done = 0
        while current:
            levels.append([by_id[tid] for tid in current])
            done += len(current)
            nxt: list[str] = []
            for tid in current:
                for dep in dependents[tid]:
                    indegree[dep] -= 1
                    if indegree[dep] == 0:
                        nxt.append(dep)
            current = nxt
        if done != len(plan.tasks):
            raise AgentOSError(ErrorCode.INVALID, "plan contains a cycle")
        return levels


def run_actor(run: RunRecord) -> AuthContext:
    envelope = json.loads(run.envelope_json)
    return AuthContext.model_validate(envelope["actor"])


async def asyncio_gather(*awaitables: Any) -> list[Any]:
    import asyncio

    return list(await asyncio.gather(*awaitables))


_ = args_hash  # 保持执行器语义引用（步骤幂等由 execute_tool_step 负责）
