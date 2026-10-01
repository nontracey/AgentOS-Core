"""全范围契约清单（蓝图 §6.1）：供后续模块消费的类型与 fixture 基线。

与 types.py 共用 ID、时间、错误和授权语义；不允许任何模块另定义一套同名对象。
列表查询使用 cursor/limit；更新带 expected_version；大正文/文件一律用 Asset 引用。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from agentos.kernel.types import AuthContext, BoundaryModel, Ref, utc_now

# ---------------------------------------------------------------------------
# 需求到成果（蓝图 §17.2）
# ---------------------------------------------------------------------------


class GoalSpec(BoundaryModel):
    goal_id: str
    version: int = Field(ge=1)
    original_request: str
    deliverables: list[str]
    acceptance_checks: list[str]
    input_asset_ids: list[str] = Field(default_factory=list)
    budget_microusd: int = Field(ge=0)
    deadline_at: datetime | None = None
    assumptions: list[str] = Field(default_factory=list)


class PlannedTask(BoundaryModel):
    task_id: str
    depends_on: list[str] = Field(default_factory=list)
    role_id: str
    skill_version: str = ""
    required_capabilities: list[str] = Field(default_factory=list)
    required_os: str | None = None
    preferred_node_id: str | None = None
    pinned_node_id: str | None = None
    input_asset_ids: list[str] = Field(default_factory=list)
    output_kinds: list[str] = Field(default_factory=list)
    acceptance_checks: list[str] = Field(default_factory=list)
    budget_microusd: int = Field(ge=0, default=0)


class TaskPlan(BoundaryModel):
    plan_id: str
    version: int = Field(ge=1)
    goal_id: str
    goal_version: int = Field(ge=1)
    tasks: list[PlannedTask]

    @model_validator(mode="after")
    def _check_dag(self) -> TaskPlan:
        if not self.tasks:
            raise ValueError("plan requires at least one task")
        ids = [t.task_id for t in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("task_id must be unique within a plan")
        id_set = set(ids)
        for task in self.tasks:
            for dep in task.depends_on:
                if dep not in id_set:
                    raise ValueError(f"task {task.task_id} depends on unknown task {dep}")
            if task.task_id in task.depends_on:
                raise ValueError(f"task {task.task_id} depends on itself")
        # 环检测：Kahn 拓扑排序
        indegree = {t.task_id: 0 for t in self.tasks}
        for t in self.tasks:
            for _dep in t.depends_on:
                indegree[t.task_id] += 1
        queue = [tid for tid, deg in indegree.items() if deg == 0]
        visited = 0
        dependents: dict[str, list[str]] = {t.task_id: [] for t in self.tasks}
        for t in self.tasks:
            for dep in t.depends_on:
                dependents[dep].append(t.task_id)
        while queue:
            tid = queue.pop()
            visited += 1
            for nxt in dependents[tid]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    queue.append(nxt)
        if visited != len(self.tasks):
            raise ValueError("plan contains a dependency cycle")
        return self


class DispatchBinding(BoundaryModel):
    plan_id: str
    plan_version: int
    task_id: str
    agent_run_id: str
    node_id: str
    capability_revision: str
    reservation_id: str
    call_id: str
    fence: int
    input_manifest_hash: str
    expires_at: datetime


# ---------------------------------------------------------------------------
# Skill / Workflow / Memory / Knowledge
# ---------------------------------------------------------------------------


class SkillManifest(BoundaryModel):
    id: str
    version: str
    digest: str
    description: str = ""
    allowed_tools: list[str] = Field(default_factory=list)
    entrypoints: list[str] = Field(default_factory=list)
    resources: list[str] = Field(default_factory=list)


class WorkflowDefinition(BoundaryModel):
    id: str
    version: int = Field(ge=1)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    triggers: list[str] = Field(default_factory=list)
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    status: Literal["draft", "published", "retired"] = "draft"


class WorkflowRun(BoundaryModel):
    id: str
    workflow_id: str
    definition_version: int
    owner: str
    checkpoint_owner: str
    external_ref: str | None = None
    internal_run_id: str | None = None
    status: Literal["running", "succeeded", "failed", "cancelled", "paused"] = "running"


class MemoryEntry(BoundaryModel):
    id: str
    tenant_id: str
    owner: str
    scope: str
    source_refs: list[Ref] = Field(default_factory=list)
    content_ref: str
    confidence: float = Field(ge=0, le=1, default=0.5)
    created_at: datetime = Field(default_factory=utc_now)
    superseded_by: str | None = None
    version: int = Field(ge=1, default=1)


class KnowledgeSource(BoundaryModel):
    id: str
    version: str
    acl: list[str] = Field(default_factory=list)
    parser: str
    chunker: str
    embedding_model: str
    index_version: str


class IngestJob(BoundaryModel):
    id: str
    source_id: str
    state: Literal["queued", "running", "completed", "failed"] = "queued"
    deduplicated_chunks: int = 0
    embedding_calls: int = 0


# ---------------------------------------------------------------------------
# Node / External App / Identity / Voice / Notification / Export / Eval / Cost
# ---------------------------------------------------------------------------


class NodeTask(BoundaryModel):
    node_id: str
    job_id: str
    call_id: str
    input_hash: str
    input_asset_ids: list[str] = Field(default_factory=list)
    capability: str
    resources: dict[str, Any] = Field(default_factory=dict)
    deadline_at: datetime
    fence: int
    grant_expires_at: datetime | None = None


class NodeReceipt(BoundaryModel):
    job_id: str
    call_id: str
    fence: int
    outcome: Literal["completed", "failed", "uncertain", "rejected"]
    content_hash: str | None = None
    external_ref: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class AppManifest(BoundaryModel):
    id: str
    version: str
    protocol: Literal["mcp", "acp", "http", "webhook", "container", "custom"]
    endpoint: str
    auth: Literal["none", "token", "oauth2", "delegated"]
    data_classes: list[str] = Field(default_factory=list)
    schemas: dict[str, Any] = Field(default_factory=dict)
    health_path: str | None = None
    lifecycle: Literal["registered", "enabled", "disabled"] = "registered"
    recovery: Literal["idempotency_key", "query", "manual"] = "manual"


class AppHandle(BoundaryModel):
    app_id: str
    external_run_id: str | None = None
    external_event_id: str | None = None
    payload_hash: str | None = None


# ---------------------------------------------------------------------------
# U4：演示访问（admin 发布 / guest 只读）——授权表达与版本化 manifest
# ---------------------------------------------------------------------------

#: 游客可见字段白名单（结构化脱敏：不在白名单的字段一律不投影，不只靠正则删 token）
GUEST_RUN_FIELDS: list[str] = [
    "id", "state", "goal_text", "channel", "created_at", "updated_at",
]
GUEST_STEP_FIELDS: list[str] = [
    "id", "tool_name", "state", "attempt", "started_at", "finished_at",
]
GUEST_ASSET_FIELDS: list[str] = [
    "id", "name", "mime", "size", "sha256", "version", "run_id",
]


class DemoRunEntry(BoundaryModel):
    """一条被显式发布的任务：只发布任务本身，不自动扩大到关联资源。

    live=False（默认）时游客只看到完成任务的发布快照，不含未审阅正文流；
    直播正文（answer.*）必须由管理员逐任务显式开启（R-A）。
    """

    run_id: str
    include: list[str] = Field(default_factory=lambda: [
        "summary", "plan", "steps", "artifacts", "approvals",
    ])
    live: bool = False
    #: 默认发布的快照高水位（Q6）：只回发布时刻之前的事件；live=True 不冻结
    event_high_water: int = Field(default=0, ge=0)


class DemoAssetEntry(BoundaryModel):
    """逐资源、逐版本发布：预览与下载是两个独立开关（可预览≠可下载）。"""

    asset_id: str
    version: int = Field(ge=1)
    allow_preview: bool = True
    allow_download: bool = False


class DemoRefEntry(BoundaryModel):
    """引用片段快照：只能看显式发布的片段，不能由引用跳到整库/全文。

    run_id 绑定被发布任务（R-B）：引用只随其来源任务对游客可见，不随清单扩散。
    acl_snapshot 记录发布时的源 ACL（R-C）：来源删除/ACL 收紧后引用随新读失效。
    """

    source_id: str
    chunk_id: str = ""
    quote: str = ""
    run_id: str = ""
    acl_snapshot: list[str] = Field(default_factory=list)
    chunk_acl_snapshot: list[str] = Field(default_factory=list)


class DemoManifestPayload(BoundaryModel):
    """版本化发布清单（不可变）：revision 只增，发布即冻结。"""

    revision: int = Field(ge=1)
    runs: list[DemoRunEntry] = Field(default_factory=list)
    assets: list[DemoAssetEntry] = Field(default_factory=list)
    refs: list[DemoRefEntry] = Field(default_factory=list)
    note: str = ""

    def run_ids(self) -> set[str]:
        return {entry.run_id for entry in self.runs}

    def asset_rule(self, asset_id: str, version: int) -> DemoAssetEntry | None:
        for entry in self.assets:
            if entry.asset_id == asset_id and entry.version == version:
                return entry
        return None

    def has_ref(self, source_id: str, chunk_id: str) -> bool:
        return any(r.source_id == source_id and r.chunk_id == chunk_id for r in self.refs)


class PolicyGrant(BoundaryModel):
    """低风险可撤销预授权（FR-GOV-02）：owner 显式授予，有工具/范围/期限边界。"""

    id: str
    tenant_id: str
    subject: str
    tool_name: str
    risks: list[str] = Field(default_factory=list)
    expires_at: datetime
    issued_by: str
    revoked: bool = False


class Transcription(BoundaryModel):
    audio_asset_id: str
    language: str = "zh"
    segments: list[dict[str, Any]] = Field(default_factory=list)
    model_version: str = ""


class RealtimeEvent(BoundaryModel):
    session_id: str
    turn_id: str
    seq: int = Field(ge=0)
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class AssetRef(BoundaryModel):
    id: str
    sha256: str
    type: str
    size_bytes: int = Field(ge=0)
    owner: str
    acl: list[str] = Field(default_factory=list)
    storage_ref: str


class NotificationDelivery(BoundaryModel):
    event_id: str
    recipient: str
    channel: str
    dedup_key: str
    state: Literal["queued", "sent", "failed", "suppressed"] = "queued"
    deadline_at: datetime | None = None


class BackupManifest(BoundaryModel):
    backup_id: str
    created_at: datetime = Field(default_factory=utc_now)
    includes_tombstones: bool = True
    content_hash: str
    schema_version: str


class EvalCase(BoundaryModel):
    id: str
    fixture_ref: str
    model: str
    prompt_version: str
    metrics: list[str] = Field(default_factory=list)


class CostReservation(BoundaryModel):
    call_id: str
    run_id: str
    reserved_microusd: int = Field(ge=0)
    settled_microusd: int | None = Field(default=None, ge=0)
    state: Literal["reserved", "settled", "released"] = "reserved"


def policy_actor_summary(actor: AuthContext) -> dict[str, str]:
    """审计/日志使用的最小主体摘要：不含任何凭据。"""
    return {"subject": actor.subject, "tenant_id": actor.tenant_id}
