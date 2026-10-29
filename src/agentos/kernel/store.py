"""账本存储契约：services/runtime 只面向 StoreProtocol，不接触 SQL（蓝图 §21）。

所有写路径满足：
- accept_once 原子写：幂等登记 + run/job + 附件关联 + outbox（FR-ING-03/FR-ORC-08）。
- claim/complete 用版本与 fence 条件更新，返回 0 行即失去所有权。
- events/audit 只追加；由存储层触发器强制 append-only（FR-GOV-03 M0 基线）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import Field

from agentos.kernel.dto import PolicyGrant
from agentos.kernel.states import ApprovalState, RunState, StepState
from agentos.kernel.types import AuthContext, BoundaryModel, Receipt

__all__ = [
    "ApprovalRecord",
    "AuditEvent",
    "JobRecord",
    "Lease",
    "OutboxEvent",
    "Receipt",
    "RunRecord",
    "StepRecord",
    "StoreProtocol",
]

class RunRecord(BoundaryModel):
    tenant_id: str
    id: str
    subject: str
    state: RunState
    version: int = 0
    channel: str = ""
    thread_id: str | None = None
    goal_text: str = ""
    envelope_json: str = "{}"
    checkpoint_json: str = "{}"
    error_json: str | None = None
    budget_microusd: int = 0
    cancel_requested_at: datetime | None = None
    lease_owner: str | None = None
    lease_until: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class StepRecord(BoundaryModel):
    tenant_id: str
    run_id: str
    id: str
    call_id: str
    tool_name: str | None = None
    args_hash: str = ""
    input_ref: str | None = None
    state: StepState = StepState.PREPARED
    approval_id: str | None = None
    external_ref: str | None = None
    result_json: str | None = None
    error_json: str | None = None
    fence: int = 0
    attempt: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None


class JobRecord(BoundaryModel):
    tenant_id: str
    id: str
    run_id: str
    state: Literal["ready", "claimed", "blocked", "done"] = "ready"
    available_at: datetime | None = None
    lease_owner: str | None = None
    lease_until: datetime | None = None
    fence: int = 0
    attempts: int = 0


class ApprovalRecord(BoundaryModel):
    tenant_id: str
    id: str
    call_id: str
    subject: str
    args_hash: str
    policy_version: str
    state: ApprovalState = ApprovalState.PENDING
    expires_at: datetime
    decided_by: str | None = None
    decided_at: datetime | None = None
    summary: str = ""


class AuditEvent(BoundaryModel):
    seq: int
    tenant_id: str
    kind: str
    subject: str | None = None
    run_id: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class OutboxEvent(BoundaryModel):
    tenant_id: str
    id: str
    event_key: str
    topic: str
    payload_json: str
    delivered_at: datetime | None = None


class Lease(BoundaryModel):
    """run 级租约：CAS + 到期时间 + 单调 fencing token（取自 runs.version）。"""

    run_id: str
    owner: str
    fence: int
    until: datetime


class StoreProtocol(Protocol):
    # ---- 受理（FR-ING-02/03）----
    def accept_once(
        self,
        envelope_json: str,
        envelope_id: str,
        idempotency_key: str,
        payload_hash: str,
        tenant_id: str,
        subject: str,
        channel: str,
        run_budget_microusd: int,
        attachment_ids: list[str],
        parent_run_id: str | None = None,
        parent_reservation_id: str | None = None,
    ) -> Receipt: ...

    def find_idempotent_receipt(
        self, tenant_id: str, subject: str, channel: str, idempotency_key: str
    ) -> tuple[str, str] | None:
        """返回 (payload_hash, receipt_json)；用于冲突检测与重放。"""

    # ---- run/step 账本 ----
    def get_run(self, tenant_id: str, run_id: str) -> RunRecord | None: ...

    def list_runs(self, tenant_id: str, subject: str | None = None) -> list[RunRecord]: ...

    def list_engine_runs(self, engine: str) -> list[RunRecord]:
        """Unfinished runs of every tenant whose flow is owned by the named workflow engine."""


    def create_step(self, step: StepRecord) -> StepRecord: ...

    def prepare_once(
        self, tenant_id: str, run_id: str, step_id: str, tool_name: str, args_hash: str, fence: int
    ) -> StepRecord:
        """同一步重试沿用 call/step ID：已 completed 返回既有结果，不重复创建。"""

    def get_step(self, tenant_id: str, run_id: str, step_id: str) -> StepRecord | None: ...

    def list_steps(self, tenant_id: str, run_id: str) -> list[StepRecord]: ...

    def transition_run(
        self,
        tenant_id: str,
        run_id: str,
        from_states: set[RunState],
        to_state: RunState,
        *,
        expected_version: int | None = None,
        error_json: str | None = None,
        event_detail: dict[str, Any] | None = None,
    ) -> RunRecord | None:
        """CAS 迁移；版本不符或状态不合法返回 None（调用方决定如何处理）。"""

    def request_cancel(self, tenant_id: str, run_id: str) -> RunRecord | None: ...

    def commit_dispatch(
        self,
        tenant_id: str,
        run_id: str,
        step_id: str,
        fence: int,
        approval_id: str | None,
        expected_args_hash: str | None = None,
    ) -> StepRecord:
        """原子：消费批准（CAS）+ 步骤进入 dispatched + 审计与事件同事务。"""

    def transition_step(
        self,
        tenant_id: str,
        run_id: str,
        step_id: str,
        from_states: set[StepState],
        to_state: StepState,
        *,
        expected_fence: int | None = None,
        result_json: str | None = None,
        error_json: str | None = None,
        external_ref: str | None = None,
        approval_id: str | None = None,
    ) -> StepRecord | None: ...

    # ---- 租约与作业 ----
    def resume_with_checkpoint(self, tenant_id: str, run_id: str, expected_version: int,
                               checkpoint_json: str, event_detail: dict[str, Any]) -> bool: ...

    def resume_runtime(self, tenant_id: str, run_id: str, expected_version: int, subject: str) -> bool:
        """CAS paused runtime + clear error + wake-up; unresolved effects block recovery."""
        ...

    def claim_run_lease(self, tenant_id: str, run_id: str, owner: str, ttl_s: int) -> Lease | None: ...

    def renew_run_lease(self, tenant_id: str, run_id: str, owner: str, fence: int, ttl_s: int) -> bool: ...

    def release_run_lease(self, tenant_id: str, run_id: str, owner: str) -> None: ...

    def enqueue_job(self, tenant_id: str, run_id: str) -> str: ...

    def claim_job(self, tenant_id: str, owner: str, ttl_s: int) -> JobRecord | None: ...

    def list_job_tenants(self) -> list[str]:
        """Trusted scheduler inventory; never exposed as a user resource listing."""
        ...

    def claim_job_for_run(self, tenant_id: str, run_id: str, owner: str, ttl_s: int) -> JobRecord | None: ...

    def save_checkpoint(self, tenant_id: str, run_id: str, checkpoint_json: str) -> bool: ...

    def complete_job(self, tenant_id: str, job_id: str, owner: str, fence: int, state: str) -> bool: ...

    # ---- 审批（FR-GOV-02）----
    def request_approval_once(self, approval: ApprovalRecord) -> ApprovalRecord: ...

    def decide_approval(
        self, tenant_id: str, approval_id: str, decision: Literal["approved", "rejected"], decided_by: str
    ) -> ApprovalRecord | None:
        """CAS pending→approved/rejected；缺失/过期/终态返回 None。"""

    def consume_approval(self, tenant_id: str, approval_id: str, expected_args_hash: str) -> ApprovalRecord | None:
        """CAS approved→consumed，同时校验参数 hash 与有效期；原子消费。"""

    def get_approval(self, tenant_id: str, approval_id: str) -> ApprovalRecord | None: ...

    def list_approvals(self, tenant_id: str, state: ApprovalState | None = None) -> list[ApprovalRecord]: ...

    def list_grants(self, tenant_id: str, subject: str) -> list[PolicyGrant]: ...

    def upsert_grant(self, grant: PolicyGrant) -> None: ...

    # ---- 审计与事件（FR-GOV-03）----
    def audit_append(
        self, tenant_id: str, kind: str, subject: str | None, run_id: str | None, detail: dict[str, Any]
    ) -> AuditEvent: ...

    def audit_query(
        self,
        tenant_id: str,
        *,
        run_id: str | None = None,
        kind_prefix: str | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]: ...

    def event_append(self, tenant_id: str, run_id: str, kind: str, payload: dict[str, Any]) -> int: ...

    def event_list(self, tenant_id: str, run_id: str, after_seq: int = 0, limit: int = 100) -> list[dict[str, Any]]: ...

    def event_high_water(self, tenant_id: str, run_id: str) -> int: ...

    def outbox_append(self, tenant_id: str, event_key: str, topic: str, payload_json: str) -> None: ...

    def outbox_drain(self, tenant_id: str, limit: int = 50) -> list[OutboxEvent]: ...

    def insert_attachment(self, record: dict[str, Any]) -> None: ...

    def count_active_runs(self, tenant_id: str) -> int:
        """非终态 run 数（背压队列上限依据，NFR-THR）。"""

    def upsert_skill(self, record: dict[str, Any]) -> None: ...

    def list_skills(self, tenant_id: str, enabled_only: bool = False) -> list[dict[str, Any]]: ...

    def delete_skills(self, tenant_id: str, skill_id: str, version: str | None = None) -> None: ...

    def upsert_mcp_server(self, record: dict[str, Any]) -> None: ...

    def list_mcp_servers(self, tenant_id: str) -> list[dict[str, Any]]: ...

    def get_mcp_server(self, tenant_id: str, server_id: str) -> dict[str, Any] | None: ...

    def update_mcp_server_discovery(
        self, tenant_id: str, server_id: str, *, protocol_version: str, tools_json: str
    ) -> None: ...

    def delete_mcp_server(self, tenant_id: str, server_id: str) -> None: ...

    def insert_mcp_proposal(self, record: dict[str, Any]) -> bool: ...

    def list_connection_drafts(self, tenant_id: str) -> list[dict[str, Any]]: ...

    def list_drafts(self, tenant_id: str, subject: str, kind: str) -> list[dict[str, Any]]: ...

    def get_draft(self, tenant_id: str, subject: str, kind: str, id: str) -> dict[str, Any] | None: ...

    def save_draft(self, tenant_id: str, subject: str, kind: str, id: str,
                   payload_json: str, expected_revision: int) -> bool: ...

    def delete_draft(self, tenant_id: str, subject: str, kind: str, id: str,
                     expected_revision: int) -> bool: ...

    def next_workflow_version(self, tenant_id: str, workflow_id: str) -> int: ...

    def insert_workflow_definition(
        self, *, tenant_id: str, id: str, version: int, name: str,
        definition_json: str, definition_hash: str, status: str,
    ) -> None: ...

    def find_workflow_by_hash(
        self, tenant_id: str, workflow_id: str, definition_hash: str
    ) -> dict[str, Any] | None: ...

    def get_workflow_definition(
        self, tenant_id: str, workflow_id: str, version: int
    ) -> dict[str, Any] | None: ...

    def insert_workflow_run(
        self, *, tenant_id: str, id: str, workflow_id: str,
        definition_version: int, internal_run_id: str,
    ) -> None: ...

    def get_workflow_run(self, tenant_id: str, wf_run_id: str) -> dict[str, Any] | None: ...

    def list_workflow_definitions(self, tenant_id: str) -> list[dict[str, Any]]: ...

    def list_workflow_runs(self, tenant_id: str) -> list[dict[str, Any]]: ...

    def next_workflow_version_atomic(self, tenant_id: str, workflow_id: str) -> int:
        ...

    def gc_attachments(self, tenant_id: str, ttl_minutes: int) -> list[str]:
        """清理超时未被任何 run 引用的暂存附件（蓝图 §8）；返回已删除的 id。"""

    def expire_approval(self, tenant_id: str, approval_id: str) -> ApprovalRecord | None:
        """CAS pending→expired；过期批准 fail-closed（蓝图 §5 批准状态）。"""


    def get_attachment(self, tenant_id: str, attachment_id: str) -> dict[str, Any] | None: ...

    # ---- 预算（FR-GOV-04 M1；M0 预留结算已随执行生效）----
    def budget_reserve(self, tenant_id: str, run_id: str, call_id: str, amount_microusd: int) -> bool: ...

    def budget_settle(self, tenant_id: str, call_id: str, actual_microusd: int) -> bool: ...

    def budget_release(self, tenant_id: str, call_id: str) -> bool: ...

    def budget_totals(self, tenant_id: str, run_id: str) -> dict[str, int]: ...

    # ---- 节点注册与 Asset 引用（M1 跨机主链；M4 深化）----
    def pair_node(
        self, tenant_id: str, node_id: str, endpoint: str, token_hash: str, token_plain: str,
        *, os: str = "", arch: str = "", capabilities_json: str = "[]",
    ) -> None: ...

    def get_node(self, tenant_id: str, node_id: str) -> dict[str, Any] | None: ...

    def list_nodes(self, tenant_id: str) -> list[dict[str, Any]]: ...

    def node_heartbeat(
        self, tenant_id: str, node_id: str, *, os: str, arch: str,
        capabilities_json: str, version: str = "",
    ) -> bool:
        """更新心跳与能力快照；仅 paired 节点生效。返回是否命中已配对节点。"""

    def revoke_node(self, tenant_id: str, node_id: str) -> bool: ...

    def upsert_asset(
        self, tenant_id: str, asset_id: str, *, sha256: str, size: int, mime: str,
        source_node: str, path: str, meta_json: str = "{}",
        subject: str = "", run_id: str | None = None, name: str = "",
        version: int = 0, parent_asset_id: str | None = None, revision_reason: str = "",
    ) -> None: ...

    def get_asset(self, tenant_id: str, asset_id: str) -> dict[str, Any] | None: ...

    def list_assets(self, tenant_id: str, subject: str | None = None) -> list[dict[str, Any]]: ...

    def find_asset_by_content(
        self, tenant_id: str, subject: str, sha256: str, mime: str,
    ) -> dict[str, Any] | None: ...

    def next_asset_version(self, tenant_id: str, parent_asset_id: str) -> int: ...

    def find_material_idem(
        self, tenant_id: str, subject: str, idempotency_key: str,
    ) -> tuple[str, str] | None: ...

    def insert_material_idem(
        self, tenant_id: str, subject: str, idempotency_key: str,
        content_sha256: str, asset_id: str,
    ) -> None: ...

    def insert_auth_session(
        self, tenant_id: str, subject: str, session_id: str,
        refresh_hash: str, expires_at: str,
    ) -> None: ...

    def find_auth_session_by_hash(
        self, tenant_id: str, refresh_hash: str,
    ) -> dict[str, Any] | None: ...

    def revoke_auth_session(self, tenant_id: str, session_id: str) -> None: ...

    def revoke_all_auth_sessions(self, tenant_id: str, subject: str) -> None: ...

    def set_mcp_server_enabled(self, tenant_id: str, server_id: str, enabled: bool) -> bool: ...

    def add_tombstone(self, tenant_id: str, resource_id: str) -> None: ...

    def list_tombstones(self, tenant_id: str) -> list[str]: ...

    def set_skill_enabled(
        self, tenant_id: str, skill_id: str, version: str, enabled: bool
    ) -> bool: ...

    # ---- U4：演示访问（admin 发布 / guest 只读）----
    def insert_demo_grant(self, record: dict[str, Any]) -> None: ...

    def get_demo_grant(self, tenant_id: str, grant_id: str) -> dict[str, Any] | None: ...

    def get_demo_grant_by_subject(
        self, tenant_id: str, subject: str
    ) -> dict[str, Any] | None: ...

    def list_demo_grants(
        self, tenant_id: str, *, subject: str | None = None, status: str | None = None,
        kind: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def expire_demo_grants(self, tenant_id: str, kind: str, now_iso: str) -> int: ...

    def set_demo_grant(
        self,
        tenant_id: str,
        grant_id: str,
        *,
        status: str | None = None,
        manifest_id: str | None = None,
        manifest_revision: int | None = None,
        revoked_at: str | None = None,
        clear_revoked_at: bool = False,
        expires_at: str | None = None,
    ) -> dict[str, Any] | None: ...

    def insert_demo_manifest(self, record: dict[str, Any]) -> None: ...

    def get_demo_manifest(
        self, tenant_id: str, manifest_id: str, revision: int
    ) -> dict[str, Any] | None: ...

    def list_demo_manifests(self, tenant_id: str, grant_id: str) -> list[dict[str, Any]]: ...

    def insert_demo_invite(self, record: dict[str, Any]) -> None: ...

    def find_demo_invite_by_hash(
        self, tenant_id: str, code_hash: str
    ) -> dict[str, Any] | None: ...

    def mark_demo_invite_redeemed(
        self, tenant_id: str, invite_id: str, redeemed_at: str
    ) -> dict[str, Any] | None: ...


def ensure_actor_valid(actor: AuthContext) -> None:
    """每次敏感读写与派发前重新核验（蓝图 §5：不能永久信任 run 创建时的快照）。"""
    if actor.is_expired:
        from agentos.kernel.errors import AgentOSError, ErrorCode

        raise AgentOSError(ErrorCode.DENIED, "auth context expired")
