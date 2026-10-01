"""授权判定（FR-GOV-01/02、FR-TOOL-08、NFR-SEC）。

规则：
- 默认拒绝：未认证、越权、未知工具一律 DENIED。
- 纯只读工具（risks 仅含 read，recovery=pure/query）在主体具备 read scope 时自动允许。
- 任何含 write/delete/exec/egress 风险的工具：需要审批；仅当存在 owner 显式授予、
  未撤销、未过期且覆盖该工具的低风险预授权（PolicyGrant）时自动允许。
- 模型建议没有授权效力：判定输入只有 actor/ToolSpec/args，与模型输出无关（FR-GOV-05）。
"""

from __future__ import annotations

from typing import Any, Literal

from jsonschema import ValidationError
from jsonschema import validate as jsonschema_validate
from pydantic import BaseModel

from agentos.kernel.dto import PolicyGrant
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.store import StoreProtocol
from agentos.kernel.types import AuthContext, ToolSpec, utc_now

WRITE_RISKS: frozenset[str] = frozenset({"write", "delete", "exec", "egress"})
_RISK_SCOPE: dict[str, str] = {
    "write": "write",
    "delete": "write",
    "exec": "exec",
    "egress": "egress",
}


class PolicyDecision(BaseModel):
    outcome: Literal["allowed", "needs_approval", "denied"]
    reason: str
    grant_id: str | None = None
    needs_approval_risks: list[str] = []


class PolicyService:
    def __init__(
        self,
        store: StoreProtocol,
        *,
        policy_version: str,
        default: Literal["deny", "allow"] = "deny",
    ) -> None:
        self._store = store
        self.policy_version = policy_version
        if default != "deny":
            raise AgentOSError(ErrorCode.INVALID, "governance.default must be deny (fail closed)")

    def check(self, actor: AuthContext, spec: ToolSpec, args: dict[str, Any]) -> PolicyDecision:
        """确定性裁决。任何情况下 deny 优先；建议性输出不能放宽。"""
        if actor.is_expired:
            return PolicyDecision(outcome="denied", reason="auth context expired")
        if not actor.has_scope("read"):
            return PolicyDecision(outcome="denied", reason="missing read scope")

        try:
            jsonschema_validate(instance=args, schema=spec.input_schema)
        except ValidationError as exc:
            return PolicyDecision(outcome="denied", reason=f"arguments invalid: {exc.message}")

        risky = [r for r in spec.risks if r in WRITE_RISKS]
        if not risky:
            return PolicyDecision(outcome="allowed", reason="read-only tool within read scope")

        for risk in risky:
            scope = _RISK_SCOPE.get(risk)
            if scope and not actor.has_scope(scope):
                return PolicyDecision(
                    outcome="denied", reason=f"actor lacks '{scope}' scope required by risk '{risk}'"
                )

        grant = self._find_active_grant(actor, spec)
        if grant is not None:
            return PolicyDecision(
                outcome="allowed", reason="covered by explicit low-risk grant", grant_id=grant
            )
        return PolicyDecision(
            outcome="needs_approval",
            reason="write/delete/exec/egress tool requires per-step approval",
            needs_approval_risks=risky,
        )

    def _find_active_grant(self, actor: AuthContext, spec: ToolSpec) -> str | None:
        now = utc_now()
        grants: list[PolicyGrant] = self._store.list_grants(actor.tenant_id, actor.subject)
        for grant in grants:
            if grant.tool_name != spec.name or grant.revoked or grant.expires_at <= now:
                continue
            return grant.id
        return None


class Reconciler:
    """错误分类（FR-ORC-08）：超时/未知一律 uncertain，不得盲重试。"""

    def __init__(self, store: StoreProtocol) -> None:
        self._store = store

    def classify(self, tool_recovery: str, error: Exception) -> tuple[str, dict[str, Any]]:
        """返回 (step_state, error_payload)。"""
        code = error.code if isinstance(error, AgentOSError) else ErrorCode.UNAVAILABLE
        if code.side_effect_unknown or tool_recovery == "manual":
            return "uncertain", {"code": str(code), "message": str(error), "needs_reconciliation": True}
        return "failed", {"code": str(code), "message": str(error)}
