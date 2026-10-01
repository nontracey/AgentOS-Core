"""工具步骤执行（蓝图 §9.1 的可运行实现）。

状态边界：
- prepare_once：同一步重试沿用 ID；completed 直接返回既有结果（不重放副作用）。
- dispatched/pending_external/uncertain：重启、重投及租约接管都不得越过恢复分支。
- awaiting_approval：审批唤醒重新走同一 step/call；有效已批准状态由 Policy 识别，
  不能新建审批；派发时 CAS 消费（原子、复核参数 hash 与有效期）。
- 错误分类：可能已产生效果 → uncertain，禁止盲重试；确定性错误 → failed。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.ports import ToolPort
from agentos.kernel.states import ApprovalState, RunState, StepState
from agentos.kernel.store import Lease, RunRecord, StepRecord, StoreProtocol
from agentos.kernel.telemetry import MetadataTracePort
from agentos.kernel.types import AuthContext, CallContext, ToolResult, ToolSpec, utc_now
from agentos.services.approvals import ApprovalService
from agentos.services.audit import AuditService
from agentos.services.budget import BudgetService
from agentos.services.identity import IdentityService
from agentos.services.observability import public_value
from agentos.services.policy import PolicyService, Reconciler


@dataclass
class RuntimeDeps:
    """组合根注入的实现集合；runtime 只面向这些接口（NFR-PRT）。"""

    store: StoreProtocol
    identity: IdentityService
    policy: PolicyService
    approvals: ApprovalService
    audit: AuditService
    budget: BudgetService
    tools: ToolPort
    skills: Any | None = None  # SkillService（渐进加载；可选注入）
    planner: Any | None = None  # PlanRunner（计划模式执行器；可选注入）
    telemetry: MetadataTracePort | None = None
    concurrency: int = 2  # 每任务并行度上限（来自 tools_per_run 配置）
    reconciler: Reconciler = field(init=False)

    def __post_init__(self) -> None:
        self.reconciler = Reconciler(self.store)


@dataclass
class StepOutcome:
    state: StepState
    result: ToolResult | None = None
    error: dict[str, Any] | None = None
    approval_id: str | None = None
    blocked: bool = False


def args_hash(args: dict[str, Any]) -> str:
    from agentos.services.ingress import canonical_hash

    return canonical_hash(args)


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False)


async def _refresh_actor(deps: RuntimeDeps, run: RunRecord) -> AuthContext:
    """每次派发前重新核验权限（蓝图 §5：不信任 run 创建时的快照）。"""
    envelope = json.loads(run.envelope_json)
    actor = AuthContext.model_validate(envelope["actor"])
    refreshed = await deps.identity.refresh(actor)
    if refreshed.tenant_id != run.tenant_id or refreshed.subject != run.subject:
        raise AgentOSError(ErrorCode.DENIED, "execution identity does not match task ownership")
    return refreshed


async def _resolve_or_fail(
    deps: RuntimeDeps, run: RunRecord, step_id: str, tool_name: str, actor: AuthContext
) -> ToolSpec | StepOutcome:
    """未知工具：确定性失败，无任何副作用（FR-GOV-05 注入面）。"""
    try:
        return await deps.tools.resolve(actor, tool_name)
    except AgentOSError as exc:
        deps.store.transition_step(
            run.tenant_id, run.id, step_id,
            {StepState.PREPARED, StepState.AWAITING_APPROVAL, StepState.DISPATCHED},
            StepState.FAILED, error_json=_json({"code": "denied", "message": exc.message}),
        )
        deps.store.transition_run(
            run.tenant_id, run.id, {RunState.RUNNING, RunState.CREATED}, RunState.FAILED,
            error_json=_json({"code": "denied", "message": f"unknown tool: {tool_name}"}),
        )
        deps.audit.record(
            run.tenant_id, "policy.denied", subject=actor.subject, run_id=run.id,
            detail={"tool": tool_name, "reason": "tool not registered", "step_id": step_id},
        )
        return StepOutcome(state=StepState.FAILED, error={"code": "denied", "message": exc.message})


def _fail_step_denied(
    deps: RuntimeDeps, run: RunRecord, step_id: str, reason: str
) -> StepOutcome:
    deps.store.transition_step(
        run.tenant_id, run.id, step_id,
        {StepState.PREPARED, StepState.AWAITING_APPROVAL, StepState.DISPATCHED},
        StepState.FAILED, error_json=_json({"code": "denied", "message": reason}),
    )
    deps.store.transition_run(
        run.tenant_id, run.id, {RunState.RUNNING, RunState.CREATED}, RunState.FAILED,
        error_json=_json({"code": "denied", "message": reason}),
    )
    return StepOutcome(state=StepState.FAILED, error={"code": "denied", "message": reason})


async def execute_tool_step(
    deps: RuntimeDeps,
    run: RunRecord,
    step_id: str,
    tool_name: str,
    step_args: dict[str, Any],
    lease: Lease,
) -> StepOutcome:
    tenant = run.tenant_id
    actor = await _refresh_actor(deps, run)
    hashed = args_hash(step_args)

    call = deps.store.prepare_once(tenant, run.id, step_id, tool_name, hashed, lease.fence)

    if call.state == StepState.COMPLETED:
        # 同 step 重放：仅有一次已确认效果（FR-ORC-08）
        return StepOutcome(
            state=call.state,
            result=ToolResult.model_validate_json(call.result_json or "{}"),
        )

    if call.state == StepState.UNCERTAIN:
        # 阻断依赖步骤：等待人工对账，不得盲重试；run 归位到 paused（待对账）
        deps.store.transition_run(
            tenant, run.id, {RunState.RUNNING, RunState.CREATED, RunState.AWAITING_APPROVAL},
            RunState.PAUSED,
        )
        return StepOutcome(
            state=call.state,
            error={"code": str(ErrorCode.UNCERTAIN), "message": "awaiting reconciliation"},
        )

    if call.state == StepState.PENDING_EXTERNAL:
        # M0 本地工具无外部句柄；远程轮询由 Node/MCP 实现承接
        return StepOutcome(
            state=call.state,
            error={"code": "pending_external", "message": "external handle outstanding"},
        )

    if call.state == StepState.CANCELLED:
        return StepOutcome(state=call.state, error={"code": "cancelled", "message": "step cancelled"})

    if call.state == StepState.FAILED:
        # 确定性失败重放（崩溃恢复/模型重试）：回放既有失败回执（无新副作用），
        # 交还模型决定补救或收尾——曾缺此分支导致重驱动时 commit_dispatch CONFLICT。
        prior = ToolResult.model_validate_json(call.result_json or "{}")
        return StepOutcome(state=StepState.FAILED, result=prior)

    # ---- dispatched：崩溃/租约接管后的恢复分支 ----
    if call.state == StepState.DISPATCHED:
        resolved = await _resolve_or_fail(deps, run, step_id, tool_name, actor)
        if isinstance(resolved, StepOutcome):
            return resolved
        return await _resume_dispatched(deps, run, call, resolved, step_args, actor, lease)

    # ---- awaiting_approval：审批唤醒分支 ----
    if call.state == StepState.AWAITING_APPROVAL:
        approval = (
            deps.store.get_approval(tenant, call.approval_id) if call.approval_id else None
        )
        if approval is not None and approval.state == ApprovalState.APPROVED:
            resolved = await _resolve_or_fail(deps, run, step_id, tool_name, actor)
            if isinstance(resolved, StepOutcome):
                return resolved
            assert resolved is not None
            return await _dispatch_and_call(deps, run, call, resolved, step_args, actor, lease)
        if approval is not None and approval.state == ApprovalState.PENDING and approval.expires_at <= utc_now():
            # 批准已过期：标记 expired 并 fail-closed（步骤失败，不得派发）
            deps.store.expire_approval(tenant, approval.id)
            deps.audit.record(
                tenant, "policy.denied", subject=actor.subject, run_id=run.id,
                detail={"tool": tool_name, "reason": "approval expired", "step_id": step_id},
            )
            return _fail_step_denied(deps, run, step_id, "approval expired")
        if approval is not None and approval.state == ApprovalState.REJECTED:
            deps.audit.record(
                tenant, "policy.denied", subject=actor.subject, run_id=run.id,
                detail={"tool": tool_name, "reason": "approval rejected", "step_id": step_id},
            )
            return _fail_step_denied(deps, run, step_id, "approval rejected")
        # 仍 pending / 过期未决：继续阻塞；run 状态同步回 awaiting_approval
        # （drive 开始时 run 已被置为 RUNNING，此处必须归位，否则 worker 会反复重入）
        deps.store.transition_run(
            tenant, run.id, {RunState.RUNNING, RunState.CREATED}, RunState.AWAITING_APPROVAL,
        )
        return StepOutcome(
            state=StepState.AWAITING_APPROVAL, approval_id=call.approval_id, blocked=True
        )

    # ---- prepared：先过确定性 Policy ----
    resolved = await _resolve_or_fail(deps, run, step_id, tool_name, actor)
    if isinstance(resolved, StepOutcome):
        return resolved
    spec = resolved

    decision = deps.policy.check(actor, spec, step_args)
    deps.store.event_append(tenant, run.id, "tool.prepared", {
        "step_id": step_id, "tool": tool_name, "arguments": public_value(step_args),
        "policy": decision.outcome, "reason": public_value(decision.reason),
        "risks": spec.risks, "recovery": spec.recovery,
    })
    if decision.outcome == "denied":
        deps.audit.record(
            tenant, "policy.denied", subject=actor.subject, run_id=run.id,
            detail={"tool": tool_name, "reason": decision.reason, "step_id": step_id},
        )
        return _fail_step_denied(deps, run, step_id, decision.reason)

    if decision.outcome == "needs_approval":
        approval = deps.approvals.request_once(
            tenant_id=tenant,
            call_id=call.call_id,
            subject=actor.subject,
            args_hash=hashed,
            policy_version=deps.policy.policy_version,
            summary=f"tool={tool_name} risks={decision.needs_approval_risks}",
            # 审批窗口覆盖工具执行期：长任务（节点生成等）审批不能先于任务过期
            min_ttl_s=max(300, spec.timeout_ms // 1000 + 300),
        )
        deps.store.transition_step(
            tenant, run.id, step_id, {StepState.PREPARED}, StepState.AWAITING_APPROVAL,
            approval_id=approval.id,
        )
        deps.store.transition_run(
            tenant, run.id, {RunState.RUNNING, RunState.CREATED}, RunState.AWAITING_APPROVAL,
        )
        deps.audit.record(
            tenant, "policy.needs_approval", subject=actor.subject, run_id=run.id,
            detail={"tool": tool_name, "approval_id": approval.id, "step_id": step_id},
        )
        return StepOutcome(state=StepState.AWAITING_APPROVAL, approval_id=approval.id, blocked=True)

    # ---- allowed：派发 ----
    return await _dispatch_and_call(deps, run, call, spec, step_args, actor, lease)


async def _resume_dispatched(
    deps: RuntimeDeps,
    run: RunRecord,
    call: StepRecord,
    spec: ToolSpec,
    step_args: dict[str, Any],
    actor: AuthContext,
    lease: Lease,
) -> StepOutcome:
    """崩溃/租约接管后的恢复：同一 call（同幂等键）继续或转 uncertain。

    不重复 commit_dispatch：步骤已处于 dispatched，批准已在原派发时原子消费。
    """
    tenant = run.tenant_id
    if spec.recovery == "manual":
        # 对账后的重放授权（FR-ORC-08）：reconcile 端点已探针外部状态、确认无副作用
        # 才会把步骤置回 dispatched 并在 error_json 打授权标记；否则仍转 uncertain。
        replay_authorized = False
        import contextlib

        replay_authorized = False
        with contextlib.suppress(json.JSONDecodeError):
            replay_authorized = bool(
                json.loads(call.error_json or "{}").get("replay_authorized"))
        if not replay_authorized:
            deps.store.transition_step(
                tenant, run.id, call.id, {StepState.DISPATCHED}, StepState.UNCERTAIN,
                error_json='{"code":"uncertain","message":"manual recovery required"}',
            )
            deps.store.transition_run(
                tenant, run.id, {RunState.RUNNING, RunState.CREATED, RunState.AWAITING_APPROVAL},
                RunState.PAUSED,
            )
            deps.audit.record(
                tenant, "step.uncertain", subject=actor.subject, run_id=run.id,
                detail={"step_id": call.id, "reason": "manual recovery after interruption"},
            )
            return StepOutcome(state=StepState.UNCERTAIN, error={"code": "uncertain"})
    # pure / idempotency_key：以相同幂等键安全续跑（外部效果由工具幂等性保证唯一）
    # manual + replay_authorized：对账确认无外部效果，重放一次
    return await _dispatch_and_call(deps, run, call, spec, step_args, actor, lease, resuming=True)


async def _dispatch_and_call(
    deps: RuntimeDeps,
    run: RunRecord,
    call: StepRecord,
    spec: ToolSpec,
    step_args: dict[str, Any],
    actor: AuthContext,
    lease: Lease,
    *,
    resuming: bool = False,
) -> StepOutcome:
    tenant = run.tenant_id
    if not resuming:
        deps.store.commit_dispatch(
            tenant,
            run.id,
            call.id,
            lease.fence,
            approval_id=call.approval_id,
            expected_args_hash=call.args_hash if call.approval_id else None,
        )
    ctx = CallContext(
        run_id=run.id,
        step_id=call.id,
        call_id=call.call_id,
        actor=actor,
        idempotency_key=f"{run.id}:{call.id}",  # 同一步重试沿用同一幂等键（FR-ORC-08）
        fence=lease.fence,
        deadline_at=utc_now() + timedelta(milliseconds=spec.timeout_ms),
    )
    deps.audit.record(
        tenant, "tool.called", subject=actor.subject, run_id=run.id,
        detail={"tool": spec.name, "step_id": call.id, "call_id": call.call_id, "fence": lease.fence},
    )
    try:
        result = await deps.tools.call(spec.name, step_args, ctx)
    except AgentOSError as exc:
        state, err = deps.reconciler.classify(spec.recovery, exc)
        deps.store.transition_step(
            tenant, run.id, call.id, {StepState.DISPATCHED},
            StepState(state),
            error_json=_json(err),
        )
        if state == "uncertain":
            deps.audit.record(
                tenant, "step.uncertain", subject=actor.subject, run_id=run.id,
                detail={"step_id": call.id, "reason": str(err.get("message"))},
            )
        _settle_run_after_step(deps, run, state)
        return StepOutcome(state=StepState(state), error=err)
    except Exception as exc:  # 未知异常按不可用处理；无副作用证据不宣称结果
        state, err = deps.reconciler.classify(spec.recovery, exc)
        deps.store.transition_step(
            tenant, run.id, call.id, {StepState.DISPATCHED},
            StepState(state),
            error_json=_json(err),
        )
        if state == "uncertain":
            deps.audit.record(
                tenant, "step.uncertain", subject=actor.subject, run_id=run.id,
                detail={"step_id": call.id, "reason": str(err.get("message"))},
            )
        _settle_run_after_step(deps, run, state)
        return StepOutcome(state=StepState(state), error=err)

    if result.status == "completed":
        deps.store.transition_step(
            tenant, run.id, call.id, {StepState.DISPATCHED}, StepState.COMPLETED,
            result_json=result.model_dump_json(),
        )
        deps.audit.record(
            tenant, "tool.completed", subject=actor.subject, run_id=run.id,
            detail={"tool": spec.name, "step_id": call.id, "status": "completed"},
        )
        return StepOutcome(state=StepState.COMPLETED, result=result)
    if result.status in {"pending", "input_required"}:
        deps.store.transition_step(
            tenant, run.id, call.id, {StepState.DISPATCHED}, StepState.PENDING_EXTERNAL,
            result_json=result.model_dump_json(), external_ref=result.external_ref,
        )
        return StepOutcome(state=StepState.PENDING_EXTERNAL, result=result)
    if result.status == "uncertain":
        deps.store.transition_step(
            tenant, run.id, call.id, {StepState.DISPATCHED}, StepState.UNCERTAIN,
            result_json=result.model_dump_json(),
        )
        deps.store.transition_run(tenant, run.id, {RunState.RUNNING}, RunState.PAUSED)
        return StepOutcome(state=StepState.UNCERTAIN, result=result)
    # failed
    deps.store.transition_step(
        tenant, run.id, call.id, {StepState.DISPATCHED}, StepState.FAILED,
        result_json=result.model_dump_json(),
        error_json=_json({"code": result.error_code, "value": result.value}),
    )
    return StepOutcome(state=StepState.FAILED, result=result)


def _settle_run_after_step(deps: RuntimeDeps, run: RunRecord, state: str) -> None:
    if state == "uncertain":
        deps.store.transition_run(
            run.tenant_id, run.id,
            {RunState.RUNNING, RunState.CREATED, RunState.AWAITING_APPROVAL}, RunState.PAUSED,
        )
    else:
        deps.store.transition_run(
            run.tenant_id, run.id, {RunState.RUNNING, RunState.CREATED}, RunState.FAILED,
            error_json='{"code":"tool_failed"}',
        )
