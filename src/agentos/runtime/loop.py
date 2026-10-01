"""Agent Loop：显式状态机驱动的 ReAct 执行（FR-ORC-02/06）。

- run/checkpoint 持久化：kill/restart 后同一 runId 继续；已确认步骤不重新执行副作用。
- 三重护栏（FR-ORC-06）：步数预算、token 预算、重复动作检测；注入诱导也必须在预算内
  以可读原因终止。
- 模型建议没有授权效力（FR-GOV-05）：工具执行一律经过 execute_tool_step 的确定性
  Policy/审批闸门。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

from agentos.kernel.errors import AgentOSError
from agentos.kernel.ports import ModelPort
from agentos.kernel.states import RUN_TERMINAL, RunState, StepState
from agentos.kernel.store import Lease, RunRecord, StoreProtocol
from agentos.kernel.types import (
    AuthContext,
    ChatMessage,
    ModelRequest,
    ModelToolCall,
    ToolSpec,
    Usage,
)
from agentos.runtime.agent_driver import AgentRuntimeDriver
from agentos.runtime.executor import execute_tool_step
from agentos.runtime.workflow_driver import WorkflowDriver
from agentos.services.observability import prompt_evidence, public_value

if TYPE_CHECKING:
    from agentos.runtime.executor import RuntimeDeps

DEFAULT_TENANT = "default"

GUARD_STEP_LIMIT = "step_budget_exhausted"
GUARD_TOKEN_LIMIT = "token_budget_exhausted"
GUARD_REPEAT = "repeated_action_loop"


class LoopLimits:
    def __init__(
        self,
        *,
        max_steps: int = 24,
        max_replans: int = 2,
        token_budget: int = 64_000,
        max_output_tokens: int = 1024,
        repeat_window: int = 3,
        lease_ttl_s: int = 120,
        microusd_per_1k_input: int = 0,
        microusd_per_1k_output: int = 0,
    ) -> None:
        self.max_steps = max_steps
        self.max_replans = max_replans
        self.token_budget = token_budget
        self.max_output_tokens = max_output_tokens
        self.repeat_window = repeat_window
        self.lease_ttl_s = lease_ttl_s
        self.microusd_per_1k_input = microusd_per_1k_input
        self.microusd_per_1k_output = microusd_per_1k_output


class AgentLoop:
    def __init__(self, deps: RuntimeDeps, model: ModelPort, limits: LoopLimits) -> None:
        self._deps = deps
        self._model = model
        self._limits = limits
        self._collaboration_runner: Any = None
        self._router: Any = None  # ModelRouter（配置降级链/隐私路由时注入）
        self._engine_driver: AgentRuntimeDriver | None = None
        self._workflow_driver: WorkflowDriver | None = None
        self._owner_id: str | None = None  # 唯一 worker owner（组合根注入，P0-1）

    def use_engine(self, driver: AgentRuntimeDriver) -> None:
        self._engine_driver = driver

    def use_router(self, router: Any) -> None:
        self._router = router

    def use_template_runner(self, runner: Any) -> None:
        """注入内置模板执行器（组合根装配；payload.template 命中时接管驱动）。"""
        self._template_runner = runner

    def set_owner(self, owner: str) -> None:
        self._owner_id = owner

    # ------------------------------------------------------------------ API
    async def drive_run(
        self, run_id: str, owner: str | None = None, *, tenant_id: str = DEFAULT_TENANT
    ) -> RunRecord | None:
        store: StoreProtocol = self._deps.store
        run = store.get_run(tenant_id, run_id)
        if run is None:
            return None
        if run.state in RUN_TERMINAL:
            self._complete_jobs(run, "done")
            return run

        resolved_owner = owner or self._owner_id or "worker"
        lease = store.claim_run_lease(tenant_id, run_id, resolved_owner, self._limits.lease_ttl_s)
        if lease is None:
            # 其他 worker 持有租约：不双跑（租约防止本系统双 worker 提交）
            return store.get_run(tenant_id, run_id)
        driving = asyncio.create_task(self._drive_claimed(run_id, resolved_owner, lease, tenant_id))

        async def heartbeat() -> None:
            while not driving.done():
                await asyncio.sleep(max(0.05, self._limits.lease_ttl_s / 3))
                if driving.done():
                    break
                if not store.renew_run_lease(
                    tenant_id, run_id, resolved_owner, lease.fence, self._limits.lease_ttl_s
                ):
                    driving.cancel()
                    return

        renewing = asyncio.create_task(heartbeat())
        try:
            return await driving
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                raise
            store.event_append(tenant_id, run_id, "lease.lost", {"fence": lease.fence})
            return store.get_run(tenant_id, run_id)
        finally:
            driving.cancel()
            renewing.cancel()
            await asyncio.gather(driving, renewing, return_exceptions=True)
            # 阻塞/终态一律释放租约（P0-1 配套）：否则 TTL 窗口内新 owner 无法接管
            store.release_run_lease(tenant_id, run_id, resolved_owner)

    async def _drive_claimed(
        self, run_id: str, resolved_owner: str, lease: Lease, tenant_id: str
    ) -> RunRecord | None:
        store: StoreProtocol = self._deps.store
        run = store.get_run(tenant_id, run_id)
        if run is None:
            return None
        if run.state == RunState.AWAITING_INPUT or (run.state == RunState.AWAITING_RESOURCES
                and json.loads(run.checkpoint_json or "{}").get("workflow_engine")):
            return run
        if run.state == RunState.CANCEL_REQUESTED:
            if (self._workflow_driver and self._workflow_driver.engine
                    and json.loads(run.checkpoint_json or "{}").get("workflow_engine")):
                await self._workflow_driver.engine.cancel(self._workflow_driver.handle(run))
            return self._cancel(run)
        if run.state in RUN_TERMINAL:
            return run
        # FR-ORC-08：存在 uncertain 步骤的 run 只能经 reconcile 端点恢复——
        # 排队 job 不得把待对账 run 拉回 RUNNING：回卷截断会绕过 uncertain 步骤
        # 重新调模型，出现「run succeeded 而 step 永远 uncertain」的账本不一致
        # （D 盲跑第3轮 comfyui_stop 实测复现，14:45:57.938 paused→.942 被残留 job 唤醒）。
        if any(s.state == StepState.UNCERTAIN for s in store.list_steps(tenant_id, run_id)):
            store.transition_run(
                tenant_id,
                run_id,
                {RunState.RUNNING, RunState.CREATED, RunState.AWAITING_APPROVAL},
                RunState.PAUSED,
            )
            return store.get_run(tenant_id, run_id) or run
        store.transition_run(
            tenant_id,
            run_id,
            {RunState.CREATED, RunState.PLANNING, RunState.AWAITING_APPROVAL, RunState.PAUSED},
            RunState.RUNNING,
        )
        refreshed = store.get_run(tenant_id, run_id)
        if refreshed is None:
            return run
        envelope = json.loads(refreshed.envelope_json)
        if (envelope.get("payload", {}).get("workflow_graph")
                or self._engine_driver is not None and self._workflow_driver is not None
                and self._workflow_driver.engine is not None and envelope.get("payload", {}).get("template")):
            assert self._workflow_driver
            return await self._workflow_driver.dispatch(refreshed)
        if isinstance(envelope.get("payload", {}).get("plan"), dict):
            from agentos.kernel.dto import TaskPlan

            TaskPlan.model_validate(envelope["payload"]["plan"])  # 确定性可执行性前检
            planner = self._deps.planner
            assert planner is not None, "planner required for plan runs"
            planned: RunRecord | None = await planner.drive_plan(refreshed, lease)
            return planned
        if envelope.get("payload", {}).get("template") == "activity-collaboration":
            collaborative: RunRecord = await self._collaboration_runner.execute(refreshed, lease)
            return collaborative
        # 内置模板（N3a）：payload.template 命中时由模板执行器接管（确定性步骤）
        tpl_runner = getattr(self, "_template_runner", None)
        if tpl_runner is not None and envelope.get("payload", {}).get("template"):
            tpl_result: RunRecord = await tpl_runner.execute(refreshed, lease)
            return tpl_result
        if self._engine_driver is not None:
            saved = json.loads(refreshed.checkpoint_json or "{}")
            if saved.get("steps_taken") and not saved.get("runtime_nodes"):
                store.transition_run(tenant_id, run_id, {RunState.RUNNING}, RunState.PAUSED,
                    error_json=json.dumps({"code": "legacy_runtime_requires_migration"}))
                return store.get_run(tenant_id, run_id)
            return await self._engine_driver.execute(refreshed, lease)
        return await self._loop(refreshed, lease)

    # ----------------------------------------------------------------- loop
    async def _loop(self, run: RunRecord, lease: Lease) -> RunRecord:
        store = self._deps.store
        tenant = run.tenant_id
        actor = self._actor_from_run(run)
        checkpoint = json.loads(run.checkpoint_json) if run.checkpoint_json else {}
        if not checkpoint:
            context = json.loads(run.envelope_json).get("payload", {}).get("context") or {}
            context_messages = []
            if context:
                context_messages.append({"role": "user", "content": str(context.get("goal") or "")})
                if context.get("answer"):
                    context_messages.append({"role": "assistant", "content": str(context["answer"])})
            skills_brief = ""
            skills_svc = getattr(self._deps, "skills", None)
            if skills_svc is not None:
                metas = json.loads(run.envelope_json).get("payload", {}).get("skills", [])
                if metas:
                    listed = "; ".join(f"{m['id']} v{m['version']}" for m in metas)
                    skills_brief = f" 可用技能：{listed}。需要时调用 skill.read 读取正文。"
            checkpoint = {
                "messages": [
                    {
                        "role": "system",
                        "content": "你是 AgentOS 的执行 Agent。只能调用已声明的工具。"
                        "用户目标中指名要调用的工具必须实际发起 tool_calls 执行，"
                        "禁止只用文字描述而不调用工具；任务全部完成后才输出纯文字总结。" + skills_brief,
                    },
                    *context_messages,
                    {"role": "user", "content": run.goal_text or "完成目标"},
                ],
                "steps_taken": 0,
                "tokens_used": 0,
                "action_hashes": [],
                "seen_steps": [],
                "model_call_seq": 0,
            }
        messages = [ChatMessage.model_validate(m) for m in checkpoint["messages"]]
        # 先做链接校验（蓝图 §5）：篡改/幽灵调用立即失败；合法崩溃的部分结果引用
        # 的是已声明调用，不会误伤——随后才执行未完成轮次的回卷。
        from agentos.kernel.errors import ErrorCode
        from agentos.kernel.types import validate_tool_linkage

        try:
            validate_tool_linkage(messages)
        except AgentOSError as exc:
            return self._fail_guard(run, str(ErrorCode.INVALID), f"checkpoint linkage invalid: {exc.message}")
        # 恢复必须执行已经持久化的调用：审批/崩溃后不能重问真实模型，
        # 否则同 step ID 的参数会漂移，破坏审批指纹与幂等语义。
        last_assistant_idx = max(
            (i for i, m in enumerate(checkpoint["messages"]) if m.get("role") == "assistant"),
            default=-1,
        )
        pending_entry = None
        resumed_ids: set[str] = set()
        if last_assistant_idx >= 0 and checkpoint["messages"][last_assistant_idx].get("tool_calls"):
            entry = checkpoint["messages"][last_assistant_idx]
            expected_ids = {c["id"] for c in entry["tool_calls"]}
            resumed_ids = {
                m.get("tool_call_id")
                for m in checkpoint["messages"][last_assistant_idx + 1 :]
                if m.get("role") == "tool"
            }
            if resumed_ids != expected_ids:
                pending_entry = entry
        specs: list[ToolSpec] = await self._deps.tools.list_tools(actor)

        while True:
            if self._cancelled(run.tenant_id, run.id):
                return self._cancel(store.get_run(tenant, run.id) or run)

            if checkpoint["steps_taken"] >= self._limits.max_steps:
                return self._fail_guard(run, GUARD_STEP_LIMIT, checkpoint["steps_taken"])
            if checkpoint["tokens_used"] >= self._limits.token_budget:
                return self._fail_guard(run, GUARD_TOKEN_LIMIT, checkpoint["tokens_used"])
            hashes = checkpoint["action_hashes"]
            window = hashes[-self._limits.repeat_window :]
            if len(window) == self._limits.repeat_window and len(set(window)) == 1:
                return self._fail_guard(run, GUARD_REPEAT, f"same action repeated {self._limits.repeat_window}x")

            if pending_entry is not None:
                tool_calls = [ModelToolCall.model_validate(c) for c in pending_entry["tool_calls"]]
                turn_no = sum(1 for m in checkpoint["messages"][:last_assistant_idx] if m.get("role") == "assistant")
                pending_entry = None
            else:
                resumed_ids = set()
                # ---- 模型调用：预算先预留、按 usage 结算（FR-GOV-04）----
                checkpoint["model_call_seq"] += 1
                model_call_id = f"{run.id}:model:{checkpoint['model_call_seq']}"
                estimate = (
                    self._limits.max_output_tokens
                    * (self._limits.microusd_per_1k_input + self._limits.microusd_per_1k_output)
                    // 1000
                )
                try:
                    self._deps.budget.reserve(tenant, run.id, model_call_id, estimate)
                except AgentOSError as exc:
                    self._deps.audit.record(
                        tenant,
                        "budget.exceeded",
                        subject=actor.subject,
                        run_id=run.id,
                        detail={"call_id": model_call_id},
                    )
                    return self._fail_guard(run, str(exc.code), "model call reservation denied")

                events: list[dict[str, Any]] = []
                usage: Usage | None = None
                request = ModelRequest(
                    messages=messages,
                    tools=specs,
                    max_output_tokens=self._limits.max_output_tokens,
                )
                store.event_append(tenant, run.id, "model.request", prompt_evidence(messages, model_call_id))
                started = time.perf_counter()
                try:
                    if self._router is not None:
                        envelope = json.loads(run.envelope_json)
                        data_class = str(envelope.get("payload", {}).get("data_class", "approved_private"))

                        async def _on_fallback(frm: str, to: str, reason: str) -> None:
                            store.event_append(
                                tenant,
                                run.id,
                                "model.fallback",
                                {"from": frm, "to": to, "reason": reason},
                            )
                            self._deps.audit.record(
                                tenant,
                                "model.fallback",
                                subject=actor.subject,
                                run_id=run.id,
                                detail={"from": frm, "to": to, "reason": reason},
                            )

                        async def _on_provider(name: str) -> None:
                            store.event_append(tenant, run.id, "model.provider", {"name": name})

                        store.event_append(tenant, run.id, "answer.start", {"call_id": model_call_id})
                        delta_count = 0
                        async for event in self._router.stream_events(
                            request,
                            actor,
                            data_class=data_class,
                            on_fallback=_on_fallback,
                            on_provider=_on_provider,
                        ):
                            events.append(event)
                            if event.get("type") == "text_delta" and event.get("delta"):
                                delta_count += 1
                                store.event_append(
                                    tenant,
                                    run.id,
                                    "answer.delta",
                                    {
                                        "call_id": model_call_id,
                                        "delta": str(event["delta"]),
                                        "seq_in_call": delta_count,
                                    },
                                )
                        store.event_append(
                            tenant,
                            run.id,
                            "answer.end",
                            {
                                "call_id": model_call_id,
                                "deltas": delta_count,
                            },
                        )
                    else:
                        # U4-04：增量**到达即持久化**（answer.start/delta/end），流式订阅端可 tail；
                        # 只有供应商真的逐字返回才有 delta——不给非流式供应商补假动画。
                        store.event_append(
                            tenant,
                            run.id,
                            "answer.start",
                            {
                                "call_id": model_call_id,
                                "run_id": run.id,
                            },
                        )
                        delta_count = 0
                        delta_chars = 0
                        async for event in self._model.stream(request, actor):
                            events.append(event)
                            if event.get("type") == "text_delta" and event.get("delta"):
                                delta_count += 1
                                delta_chars += len(str(event["delta"]))
                                store.event_append(
                                    tenant,
                                    run.id,
                                    "answer.delta",
                                    {
                                        "call_id": model_call_id,
                                        "delta": str(event["delta"]),
                                        "seq_in_call": delta_count,
                                    },
                                )
                        store.event_append(
                            tenant,
                            run.id,
                            "answer.end",
                            {
                                "call_id": model_call_id,
                                "deltas": delta_count,
                                "chars": delta_chars,
                            },
                        )
                except AgentOSError as exc:
                    self._deps.budget.release(tenant, model_call_id)
                    return self._fail_guard(run, str(exc.code), f"model call failed: {exc.message}")

                latency_ms = round((time.perf_counter() - started) * 1000)
                model_name = None
                for event in events:
                    model_name = event.get("model") or model_name
                    if event["type"] == "usage":
                        usage = event["usage"]

                store.event_append(
                    tenant,
                    run.id,
                    "model.completed",
                    {
                        "call_id": model_call_id,
                        "model": model_name,
                        "latency_ms": latency_ms,
                    },
                )
                actual = self._cost(usage)
                self._deps.budget.settle(tenant, model_call_id, actual)
                if usage is not None:
                    checkpoint["tokens_used"] += usage.input_tokens + usage.output_tokens
                    # 模型名随 usage 记录：账本/工作台必须能回答"这步用的什么模型"（禁静默降级）
                    usage_payload = usage.model_dump(mode="json")
                    usage_payload["call_id"] = model_call_id
                    if model_name:
                        usage_payload["model"] = model_name
                    store.event_append(tenant, run.id, "model.usage", usage_payload)

                tool_calls = []
                content = ""
                for event in events:
                    if event["type"] == "text_delta":
                        content += event["delta"]
                    elif event["type"] == "assistant_message":
                        content = event.get("content") or content
                        tool_calls = list(event.get("tool_calls", []))

                if not tool_calls:
                    checkpoint["messages"].append({"role": "assistant", "content": content})
                    self._save_checkpoint(run.tenant_id, run.id, checkpoint)
                    # 正文结束即持久化完成：断连/刷新可据此恢复已完成消息（不重复拼接）
                    store.event_append(
                        tenant,
                        run.id,
                        "message.completed",
                        {
                            "call_id": model_call_id,
                            "chars": len(content),
                            "persisted": True,
                        },
                    )
                    store.transition_run(
                        tenant,
                        run.id,
                        {RunState.RUNNING},
                        RunState.SUCCEEDED,
                        event_detail={"final": content[:2000]},
                    )
                    self._complete_jobs(store.get_run(tenant, run.id) or run, "done")
                    return store.get_run(tenant, run.id) or run

                store.event_append(
                    tenant,
                    run.id,
                    "model.tool_selection",
                    {
                        "call_id": model_call_id,
                        "tools": [{"name": c.name, "arguments": public_value(c.arguments)} for c in tool_calls],
                        "basis": "模型从已声明工具中返回 tool_calls；授权由服务端另行裁定",
                    },
                )

                # ---- 先持久化 assistant 消息（含 tool_calls），再执行工具 ----
                # 轮次号取追加前的 assistant 计数：崩溃重启回卷后可复现同一步骤 ID。
                turn_no = sum(1 for m in checkpoint["messages"] if m.get("role") == "assistant")
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [c.model_dump() for c in tool_calls],
                }
                checkpoint["messages"].append(assistant_msg)
                messages.append(ChatMessage(role="assistant", content=content, tool_calls=tool_calls))
                self._save_checkpoint(run.tenant_id, run.id, checkpoint)

            for i, call in enumerate(tool_calls):
                if call.id in resumed_ids:
                    continue
                step_id = f"turn{turn_no}_call{i}_{call.name}"
                # 同一步重试（崩溃恢复/审批唤醒）不重复累计步数与动作指纹
                if step_id not in checkpoint["seen_steps"]:
                    checkpoint["seen_steps"].append(step_id)
                    checkpoint["action_hashes"].append(
                        f"{call.name}:{json.dumps(call.arguments, sort_keys=True, ensure_ascii=False)}"
                    )
                    checkpoint["steps_taken"] += 1
                self._save_checkpoint(run.tenant_id, run.id, checkpoint)

                current = store.get_run(tenant, run.id) or run
                outcome = await execute_tool_step(self._deps, current, step_id, call.name, call.arguments, lease)
                if outcome.state in {
                    StepState.AWAITING_APPROVAL,
                    StepState.UNCERTAIN,
                }:
                    # 阻塞/待对账：控制权交回（审批决定或对账后 enqueue 唤醒重入）。
                    # FAILED 不阻塞：失败回执喂回模型，由模型决定补救或收尾（模型自愈）。
                    return store.get_run(tenant, run.id) or run

                result_payload = outcome.result.value if outcome.result else {}
                tool_msg: dict[str, Any] = {
                    "role": "tool",
                    "content": json.dumps(result_payload, ensure_ascii=False),
                    "tool_call_id": call.id,
                }
                checkpoint["messages"].append(tool_msg)
                messages.append(ChatMessage(role="tool", content=tool_msg["content"], tool_call_id=call.id))
                self._save_checkpoint(run.tenant_id, run.id, checkpoint)

    # --------------------------------------------------------------- helpers
    def _cancelled(self, tenant_id: str, run_id: str) -> bool:
        run = self._deps.store.get_run(tenant_id, run_id)
        return run is not None and run.state == RunState.CANCEL_REQUESTED

    def _cancel(self, run: RunRecord) -> RunRecord:
        store = self._deps.store
        tenant = run.tenant_id
        for step in store.list_steps(tenant, run.id):
            if step.state in {StepState.PREPARED, StepState.AWAITING_APPROVAL, StepState.DISPATCHED}:
                store.transition_step(
                    tenant,
                    run.id,
                    step.id,
                    {StepState.PREPARED, StepState.AWAITING_APPROVAL, StepState.DISPATCHED},
                    StepState.CANCELLED,
                )
        store.transition_run(
            tenant,
            run.id,
            {RunState.CANCEL_REQUESTED, RunState.RUNNING, RunState.AWAITING_APPROVAL},
            RunState.CANCELLED,
        )
        self._deps.audit.record(tenant, "run.cancelled", subject=run.subject, run_id=run.id, detail={})
        self._complete_jobs(store.get_run(tenant, run.id) or run, "done")
        return store.get_run(tenant, run.id) or run

    def _fail_guard(self, run: RunRecord, code: str, detail: Any) -> RunRecord:
        store = self._deps.store
        error = json.dumps({"code": code, "message": str(detail)}, ensure_ascii=False)
        store.transition_run(
            run.tenant_id,
            run.id,
            {RunState.RUNNING, RunState.AWAITING_APPROVAL, RunState.CREATED},
            RunState.FAILED,
            error_json=error,
        )
        self._deps.audit.record(
            run.tenant_id,
            "loop.guard_triggered",
            subject=run.subject,
            run_id=run.id,
            detail={"code": code, "detail": str(detail)},
        )
        self._complete_jobs(store.get_run(run.tenant_id, run.id) or run, "done")
        return store.get_run(run.tenant_id, run.id) or run

    def _save_checkpoint(self, tenant_id: str, run_id: str, checkpoint: dict[str, Any]) -> None:
        self._deps.store.save_checkpoint(tenant_id, run_id, json.dumps(checkpoint, ensure_ascii=False))

    def _complete_jobs(self, run: RunRecord, state: str) -> None:
        store = self._deps.store
        for _ in range(50):
            job = store.claim_job_for_run(run.tenant_id, run.id, "worker", ttl_s=5)
            if job is None:
                return
            store.complete_job(run.tenant_id, job.id, "worker", job.fence, state)

    def _actor_from_run(self, run: RunRecord) -> AuthContext:
        envelope = json.loads(run.envelope_json)
        return AuthContext.model_validate(envelope["actor"])

    def _cost(self, usage: Usage | None) -> int:
        if usage is None:
            return 0
        return (
            usage.input_tokens * self._limits.microusd_per_1k_input // 1000
            + usage.output_tokens * self._limits.microusd_per_1k_output // 1000
        )
