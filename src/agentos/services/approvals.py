"""审批服务（FR-GOV-02）：批准绑定主体、step/call、参数摘要、策略版本与过期时间。

- request_once：同一 call 只创建一次审批（UNIQUE(tenant,call_id)）。
- decide：CAS pending→approved/rejected；过期/终态/缺失一律失败。
- consume：由派发路径原子消费（approved→consumed），并复核参数 hash 与有效期；
  已消费的批准重试复用账本，不产生新的可执行权利。
- 跨主体：只有管理员（部署内全量业务）能代裁受控入口（webhook/cron）的审批；
  decided_by 始终记录真实操作者，审批仍绑定原 call/参数 hash。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.states import ApprovalState
from agentos.kernel.store import ApprovalRecord, StoreProtocol
from agentos.kernel.types import AuthContext, utc_now

DEFAULT_APPROVAL_TTL_S = 3600


class ApprovalService:
    def __init__(self, store: StoreProtocol, *, default_ttl_s: int = DEFAULT_APPROVAL_TTL_S) -> None:
        self._store = store
        self._ttl = default_ttl_s

    def request_once(
        self,
        tenant_id: str,
        call_id: str,
        subject: str,
        args_hash: str,
        policy_version: str,
        summary: str,
        *,
        min_ttl_s: int = 0,
    ) -> ApprovalRecord:
        # TTL 与工具超时对齐：长任务（ComfyUI 生成等）审批窗口必须覆盖执行期，
        # 否则审批在生成完成前过期（fail-closed），任务卡死。
        ttl = max(self._ttl, min_ttl_s)
        record = ApprovalRecord(
            tenant_id=tenant_id,
            id=f"apr_{call_id}",
            call_id=call_id,
            subject=subject,
            args_hash=args_hash,
            policy_version=policy_version,
            state=ApprovalState.PENDING,
            expires_at=utc_now() + timedelta(seconds=ttl),
            summary=summary,
        )
        return self._store.request_approval_once(record)

    def decide(
        self,
        actor: AuthContext,
        approval_id: str,
        decision: Literal["approve", "reject"],
    ) -> ApprovalRecord:
        if not actor.has_scope("approve"):
            raise AgentOSError(ErrorCode.DENIED, "actor lacks approve scope")
        if actor.is_expired:
            raise AgentOSError(ErrorCode.DENIED, "auth context expired")
        existing = self._store.get_approval(actor.tenant_id, approval_id)
        if existing is None:
            raise AgentOSError(ErrorCode.INVALID, f"approval {approval_id} not found")
        if existing.subject != actor.subject and not self._is_admin(actor):
            # Q10：同租户其他主体（如 webhook/cron 受控入口）的任务，管理员可裁定；
            # 普通主体仍严格限本人，游客在写动作前置就被拒绝。
            raise AgentOSError(ErrorCode.DENIED, "approval belongs to another subject")
        target: Literal["approved", "rejected"] = "approved" if decision == "approve" else "rejected"
        # decided_by 写**真实操作者**，不冒充任务 owner（审计可追到谁批的）
        decided = self._store.decide_approval(actor.tenant_id, approval_id, target, actor.subject)
        if decided is None:
            raise AgentOSError(
                ErrorCode.CONFLICT, f"approval {approval_id} not in pending state or already expired"
            )
        # 审批决定唤醒对应 run：call_id 形如 "{run_id}:{step_id}"
        run_id = decided.call_id.split(":", 1)[0]
        self._store.enqueue_job(actor.tenant_id, run_id)
        return decided

    @staticmethod
    def _is_admin(actor: AuthContext) -> bool:
        """访问角色只读可信身份层写入的 roles（不接受客户端自报）。"""
        from agentos.services.access import ROLE_ADMIN

        return ROLE_ADMIN in actor.roles

    def list_pending(self, tenant_id: str) -> list[ApprovalRecord]:
        return self._store.list_approvals(tenant_id, ApprovalState.PENDING)
