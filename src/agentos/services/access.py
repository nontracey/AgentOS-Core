"""U4-01 统一资源授权（计划 §2.2）：可信身份 + tenant + action + 资源范围 + grant/版本 + 资源关系。

规则：

- **默认拒绝**：任何判定都必须显式落到 allow，其余一律 DENIED 并记审计。
- 访问角色只有 `admin` / `guest`；执行角色（research/design/review）不是用户账号，
  子步骤委托不能从演示 grant 取得权限，也不能冒充 admin 全量上下文。
- 资源关系由**服务端读取**：客户端提供的 run_id/asset_id/owner/role 不能作为归属证明。
- 演示 grant 只能授予只读投影，**永远不能被传成执行权限**；一条任务不等于其关联资源已授权。
- 模型建议、Skill 文本与外部内容没有授权效力；本模块的输入只有 actor/action/resource。
"""

from __future__ import annotations

import json
from typing import Any, Literal

from agentos.kernel.dto import DemoManifestPayload
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.store import StoreProtocol
from agentos.kernel.types import AuthContext, utc_now
from agentos.services.audit import AuditService

ROLE_ADMIN = "admin"
ROLE_GUEST = "guest"
ROLE_MEMBER = "member"

#: 会改变世界的动作集合：guest 一律拒绝，且不能有任何入队/副作用。
WRITE_ACTIONS: frozenset[str] = frozenset({
    "task.create", "task.cancel", "task.reconcile", "task.retry",
    "material.upload", "asset.push", "asset.fetch", "asset.register",
    "approval.decide", "workflow.publish", "workflow.start", "workflow.signal",
    "tool.call", "skill.install", "skill.remove", "skill.activate",
    "node.pair", "node.revoke", "mcp.register", "mcp.enable",
    "memory.write", "config.write", "export.restore", "demo.publish",
    "demo.invite", "demo.revoke", "auth.password",
})

#: guest 显式允许的只读动作白名单（R-E）：**放行只来自这份名单**，
#: 不再以"不在黑名单"作为依据——未知/拼写错误的动作一律拒绝并审计。
#: 动作与资源类型**成对**判定（Q5）：错配组合（如资产动作配 task 资源）拒绝；
#: 白名单动作还必须落到具体资源关系（kind=task/asset 的逐项核对）才最终 allow；
#: 全局读取面（检索/记忆/清单/审计/配置等）对游客整体关闭。
GUEST_ALLOWED_READS: dict[str, ResourceKind] = {
    "task.read": "task",            # 已发布任务详情/事件/流（逐任务核对 manifest）
    "demo.asset.preview": "asset",  # 已发布版本预览（逐版本核对开关）
    "demo.asset.download": "asset",  # 已发布版本下载（独立判据，可预览≠可下载）
}

ResourceKind = Literal["task", "asset", "ref", "run_list", "global"]


class AccessDecision:
    """判定结果。allow 只有显式给出才成立。"""

    __slots__ = ("allowed", "reason", "grant_id", "manifest")

    def __init__(
        self,
        allowed: bool,
        reason: str,
        *,
        grant_id: str | None = None,
        manifest: DemoManifestPayload | None = None,
    ) -> None:
        self.allowed = allowed
        self.reason = reason
        self.grant_id = grant_id
        self.manifest = manifest

    def __bool__(self) -> bool:
        return self.allowed


def role_of(actor: AuthContext) -> str:
    """访问角色只由可信身份层写入的 roles 决定，不接受客户端自报字段。"""
    if ROLE_ADMIN in actor.roles:
        return ROLE_ADMIN
    if ROLE_GUEST in actor.roles:
        return ROLE_GUEST
    return ROLE_MEMBER


class AccessService:
    """统一资源授权服务：gateway、服务查询、下载/预览/引用、事件与非 HTTP 入口共用。"""

    def __init__(self, store: StoreProtocol, audit: AuditService, *, tenant_id: str = "default") -> None:
        self._store = store
        self._audit = audit
        self._default_tenant = tenant_id

    # ------------------------------------------------------------ 基础判定
    @staticmethod
    def role(actor: AuthContext) -> str:
        return role_of(actor)

    def is_admin(self, actor: AuthContext) -> bool:
        return role_of(actor) == ROLE_ADMIN

    def active_grant(self, actor: AuthContext) -> dict[str, Any] | None:
        """游客当前生效的 grant（每次读请求重新核对，不缓存、不信任会话快照）。"""
        if role_of(actor) != ROLE_GUEST:
            return None
        row = self._store.get_demo_grant_by_subject(actor.tenant_id, actor.subject)
        if row is None:
            return None
        if str(row.get("status")) != "active":
            return None
        expires_at = str(row.get("expires_at") or "")
        if not expires_at or expires_at <= utc_now().isoformat():
            return None
        if actor.is_expired:
            return None
        return row

    def manifest_for(self, actor: AuthContext) -> DemoManifestPayload | None:
        grant = self.active_grant(actor)
        if grant is None:
            return None
        payload = self._load_manifest(grant)
        return payload

    def _load_manifest(self, grant: dict[str, Any]) -> DemoManifestPayload | None:
        manifest_id = str(grant.get("manifest_id") or "")
        revision = int(grant.get("manifest_revision") or 0)
        if not manifest_id or revision <= 0:
            return None
        row = self._store.get_demo_manifest(grant["tenant_id"], manifest_id, revision)
        if row is None:
            return None
        return DemoManifestPayload.model_validate_json(str(row["payload_json"]))

    # ------------------------------------------------------------ 统一入口
    def check(
        self,
        actor: AuthContext,
        action: str,
        *,
        kind: ResourceKind = "global",
        resource_id: str = "",
        version: int | None = None,
    ) -> AccessDecision:
        """默认拒绝的确定性判定；只依赖服务端读到的资源关系。"""
        role = role_of(actor)
        if actor.is_expired:
            return AccessDecision(False, "auth context expired")

        if role == ROLE_GUEST:
            if action in WRITE_ACTIONS:
                return AccessDecision(False, f"guest cannot perform write action '{action}'")
            grant = self.active_grant(actor)
            if grant is None:
                return AccessDecision(False, "no active demo grant for this guest session")
            manifest = self._load_manifest(grant)
            if manifest is None:
                return AccessDecision(False, "demo manifest missing for active grant")
            # 默认拒绝（R-E/Q5）：动作必须在白名单内**且与资源类型成对**，
            # 否则（含未知动作/错配组合/未知 kind）一律拒绝
            expected_kind = GUEST_ALLOWED_READS.get(action)
            if expected_kind is None:
                return AccessDecision(False, f"action '{action}' is not an allowed guest read")
            if kind != expected_kind:
                return AccessDecision(
                    False, f"action '{action}' requires resource kind '{expected_kind}', got '{kind}'")
            if kind == "task":
                return self._guest_task(grant, manifest, resource_id)
            if kind == "asset":
                return self._guest_asset(grant, manifest, resource_id, version, action)
            return AccessDecision(False, f"unknown resource kind '{kind}'")

        if role == ROLE_ADMIN:
            # 管理员管理个人部署内全部业务资源；仍受 tenant 边界与治理规则约束。
            if kind == "task" and resource_id:
                run = self._store.get_run(actor.tenant_id, resource_id)
                if run is None:
                    return AccessDecision(False, "run not found")
                if run.tenant_id != actor.tenant_id:
                    return AccessDecision(False, "run belongs to another tenant")
                return AccessDecision(True, "admin owns deployment resources")
            if kind == "asset" and resource_id:
                row = self._store.get_asset(actor.tenant_id, resource_id)
                if row is None:
                    return AccessDecision(False, "asset not found")
                return AccessDecision(True, "admin owns deployment resources")
            return AccessDecision(True, "admin deployment scope")

        # 其他主体（同租户普通主体/服务身份）：按既有 scope 与归属裁定，不自动继承 admin。
        if action in {"demo.publish", "demo.invite", "demo.revoke", "config.write", "export.restore"}:
            return AccessDecision(False, f"'{action}' requires admin role")
        if kind == "task" and resource_id:
            run = self._store.get_run(actor.tenant_id, resource_id)
            if run is None or run.subject != actor.subject:
                return AccessDecision(False, "not your task")
            return AccessDecision(True, "own resource")
        if kind == "asset" and resource_id:
            row = self._store.get_asset(actor.tenant_id, resource_id)
            if row is None or str(row.get("subject") or "") != actor.subject:
                return AccessDecision(False, "not your asset")
            return AccessDecision(True, "own resource")
        if kind == "global":
            # 既有行为：普通主体仍可按 subject 过滤读取自己的资源清单（服务层已过滤）
            return AccessDecision(True, "member reads own scope")
        return AccessDecision(False, "member scope: default deny")

    def _guest_task(
        self, grant: dict[str, Any], manifest: DemoManifestPayload, run_id: str
    ) -> AccessDecision:
        if run_id not in manifest.run_ids():
            return AccessDecision(False, "run not published to this guest")
        run = self._store.get_run(str(grant["tenant_id"]), run_id)
        if run is None or run.tenant_id != str(grant["tenant_id"]):
            return AccessDecision(False, "run not found in grant tenant")
        return AccessDecision(True, "published run", grant_id=str(grant["id"]), manifest=manifest)

    def _guest_asset(
        self,
        grant: dict[str, Any],
        manifest: DemoManifestPayload,
        asset_id: str,
        version: int | None,
        action: str,
    ) -> AccessDecision:
        row = self._store.get_asset(str(grant["tenant_id"]), asset_id)
        if row is None:
            return AccessDecision(False, "asset not found")
        actual_version = int(row.get("version") or 0)
        rule = manifest.asset_rule(asset_id, actual_version if version is None else version)
        if rule is None:
            # 版本不符（含 v2 未发布）或与清单不一致 → 拒绝，不回退到"同 id 任一版本"
            return AccessDecision(False, "asset version not published to this guest")
        if version is not None and version != actual_version:
            return AccessDecision(False, "asset version mismatch with published manifest")
        if action.endswith(".download") and not rule.allow_download:
            return AccessDecision(False, "download not granted for this asset version")
        if action.endswith(".preview") and not rule.allow_preview:
            return AccessDecision(False, "preview not granted for this asset version")
        return AccessDecision(True, "published asset", grant_id=str(grant["id"]), manifest=manifest)

    # ------------------------------------------------------------ 强制入口
    def require(
        self,
        actor: AuthContext,
        action: str,
        *,
        kind: ResourceKind = "global",
        resource_id: str = "",
        version: int | None = None,
        audit_kind: str = "access.denied",
    ) -> AccessDecision:
        """判定 + 拒绝即审计（主体/动作/资源标识/原因，不记被拒资源正文）。"""
        decision = self.check(actor, action, kind=kind, resource_id=resource_id, version=version)
        if not decision.allowed:
            self._audit.record(
                actor.tenant_id, audit_kind, subject=actor.subject,
                run_id=resource_id if kind == "task" else None,
                detail={
                    "action": action, "kind": kind,
                    "resource_id": resource_id[:64],
                    "version": version, "reason": decision.reason,
                    "role": role_of(actor),
                },
            )
            raise AgentOSError(ErrorCode.DENIED, decision.reason)
        return decision

    def require_admin(self, actor: AuthContext, action: str) -> None:
        if role_of(actor) != ROLE_ADMIN:
            self._audit.record(
                actor.tenant_id, "access.denied", subject=actor.subject,
                detail={"action": action, "reason": "requires admin role", "role": role_of(actor)},
            )
            raise AgentOSError(ErrorCode.DENIED, f"'{action}' requires admin role")

    def require_write_allowed(self, actor: AuthContext, action: str) -> None:
        """写动作的统一前置：guest 一律拒绝；其余主体仍受既有 scope/policy/审批约束。

        用于 ingress 等所有入口共用点——拒绝发生在受理前，不会入队也不会产生副作用。
        """
        if actor.is_expired:
            raise AgentOSError(ErrorCode.DENIED, "auth context expired")
        if role_of(actor) == ROLE_GUEST:
            self._audit.record(
                actor.tenant_id, "access.denied", subject=actor.subject,
                detail={"action": action, "reason": "guest is read-only", "role": ROLE_GUEST},
            )
            raise AgentOSError(ErrorCode.DENIED, f"guest cannot perform '{action}'")

    @staticmethod
    def assert_not_guest(actor: AuthContext, action: str) -> None:
        """非 HTTP 入口（CLI/Webhook/voice/工作流）共用：guest 不获得任何业务写权限。"""
        if role_of(actor) == ROLE_GUEST and action in WRITE_ACTIONS:
            raise AgentOSError(ErrorCode.DENIED, f"guest cannot perform '{action}'")

    # ------------------------------------------------------------ 投影工具
    @staticmethod
    def project(row: dict[str, Any], fields: list[str]) -> dict[str, Any]:
        """白名单投影：不在名单的字段一律丢弃（不是"删掉看起来敏感的键"）。"""
        return {key: row.get(key) for key in fields}

    @staticmethod
    def public_event(event: dict[str, Any], *, live: bool = False) -> dict[str, Any] | None:
        """事件投影（R-A）：**逐事件种类定义可显示字段与值的白名单**。

        - 白名单未收录的事件种类（提示词/工具选择等含原文或隐私参数的）整条丢弃；
        - 收录种类只保留点名字段，值必须是标量——任意新键、嵌套列表/字典一律不下发；
        - answer 正文流（start/delta/end）默认不进游客投影：发布快照不含未审阅正文；
          直播必须由管理员在 manifest 中显式开启（DemoRunEntry.live）。
        """
        kind = str(event.get("kind") or "")
        fields = GUEST_EVENT_FIELDS.get(kind)
        if fields is None:
            fields = GUEST_LIVE_EVENT_FIELDS.get(kind) if live else None
            if fields is None:
                return None
        allowed = {"seq", "kind", "created_at", "run_id"}
        out = {k: v for k, v in event.items() if k in allowed}
        payload = event.get("payload", event.get("payload_json"))
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        if isinstance(payload, dict):
            projected: dict[str, Any] = {}
            for key in fields:
                if key not in payload:
                    continue
                value = _public_scalar(payload[key])
                if value is None and payload[key] is not None:
                    continue  # 容器/复杂结构不下发，不降级为 null
                projected[key] = value
            out["payload"] = projected
        return out


#: 逐事件种类的可显示 payload 字段白名单（R-A）。未收录种类对游客整条丢弃：
#: 新增事件种类默认不可见，必须显式加入白名单并点名可显示字段后才投影。
GUEST_EVENT_FIELDS: dict[str, frozenset[str]] = {
    "run.created": frozenset({"channel"}),
    "run.state_changed": frozenset({"to"}),  # event_detail（final/delivery/error 原文）不下发
    "step.state_changed": frozenset({"step_id", "to"}),
    "step.prepared": frozenset({"step_id", "tool", "policy"}),
    "tool.prepared": frozenset({"step_id", "tool", "policy"}),  # arguments/risks 属隐私参数
    "node.dispatched": frozenset({"step_id", "tool", "node_id", "os", "basis"}),
    "asset.delivered": frozenset({"step_id", "asset_id", "node_id", "sha256", "size", "mime"}),
    "asset.delivery_failed": frozenset({"step_id", "node_id", "code"}),
    "model.provider": frozenset({"name"}),
    "model.fallback": frozenset({"from", "to", "reason"}),
    "model.completed": frozenset({"model", "latency_ms"}),
    "model.usage": frozenset({"model", "input_tokens", "output_tokens"}),
    "message.completed": frozenset({"chars", "persisted"}),
    "plan.task_finished": frozenset({"task_id", "state"}),
    "report.structure_check": frozenset({"passed", "detail"}),
    "report.citation_check": frozenset({"passed", "detail"}),
    "report.delivered": frozenset({"kind", "template", "template_version", "asset_id",
                                   "sha256", "version", "parent_asset_id", "revision_reason"}),
}

#: 正文流事件（U4-04）：仅当管理员对该任务显式开启直播时才进入游客投影。
GUEST_LIVE_EVENT_FIELDS: dict[str, frozenset[str]] = {
    "answer.start": frozenset(),
    "answer.delta": frozenset({"delta", "seq_in_call"}),
    "answer.end": frozenset({"deltas", "chars"}),
}

#: 单个字符串值的上限：界定可传播内容，防止把整段正文塞进任意白名单字段。
GUEST_EVENT_VALUE_MAX = 2000


def _public_scalar(value: Any) -> Any:
    """值级约束：只放行标量；字符串截断到上限；列表/字典/复杂结构一律丢弃。"""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:GUEST_EVENT_VALUE_MAX]
    return None
