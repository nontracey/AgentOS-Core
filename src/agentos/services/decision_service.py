"""快决策服务（FR-DEC-01～08）：影子模式建议性闸门 + 降级链 + 审计。

定位（需求 §4.3）：
- 决策层输出的是**建议**（路由/风险/成本/质量四类闸门），最终安全授权与审批
  由确定性 Policy 强制裁决——决策后端不得放宽（FR-DEC-05）。
- 默认影子模式：建议只写入审计/事件，与原链路并行，不进关键路径（FR-DEC-06）；
  关闭决策层功能零退化。
- 用户装哪个后端就用哪个，切换只改配置（FR-DEC-02）；配置的后端不可用（未注册/
  无凭据/不可达）时明确降级 rule 并记录降级事件（FR-DEC-02/03/08）。
- 仅装 rule_fallback 时可独立 boot（FR-DEC-01/04：全新环境零依赖可启动）。
"""

from __future__ import annotations

from typing import Any

from agentos.kernel.decision import ChoiceQuestion, DecisionResult
from agentos.kernel.errors import AgentOSError
from agentos.services.audit import AuditService


class DecisionService:
    """影子模式决策门面：provider 选择 + 降级链 + 影子审计。

    - providers: 已注册后端表（name → adapter）。只有 rule 必须内置；
      kev/laya/jev 未注册时按配置请求降级 rule 并审计（FR-DEC-02/08）。
    - shadow=True（默认，由 settings.decision_mode 强制）：输出仅入审计，
      调用方不得据此改变执行路径。
    """

    def __init__(
        self,
        adapters: dict[str, Any],
        audit: AuditService,
        tenant_id: str = "default",
        *,
        provider: str = "rule",
        shadow: bool = True,
    ) -> None:
        self._audit = audit
        self._tenant = tenant_id
        self._shadow = shadow
        self.provider = provider
        self.active: Any | None = None
        self.degraded_from: str | None = None
        if provider in adapters:
            self.active = adapters[provider]
        else:
            # 配置的后端不可用 → 明确降级 rule 并记录（FR-DEC-02/08：不以静默方式换实现）
            self.active = adapters.get("rule")
            self.degraded_from = provider if provider != "rule" else None
            self._audit.record(
                tenant_id, "decision.degraded", subject=None, run_id=None,
                detail={"configured": provider, "fallback": "rule",
                        "reason": "provider not installed"},
            )

    def capabilities(self) -> dict[str, Any]:
        prof: dict[str, Any] = {}
        if self.active is not None and hasattr(self.active, "calibration_profile"):
            # calibration_profile 可能是协程（FR-DEC-07 由各适配器实现）
            import asyncio

            try:
                asyncio.get_running_loop()
            except RuntimeError:
                prof = asyncio.run(self.active.calibration_profile())
        return {
            "configured_provider": self.provider,
            "active_provider": "rule" if self.degraded_from else self.provider,
            "degraded_from": self.degraded_from,
            "shadow": self._shadow,
            "calibration": prof,
        }

    async def risk_advice(
        self, subject: str, tool: str, args_hash: str,
        risk_tags: list[str], run_id: str | None = None,
    ) -> DecisionResult:
        """风险闸门建议（FR-DEC-05）：deny/ask/allow 三选一，影子输出入审计。

        返回值仅供影子分析与测试；调用方（loop）不得据此放行——放行与否
        仍由 Policy/审批决定。
        """
        question = ChoiceQuestion(
            id=f"risk:{tool}",
            instructions="对该工具调用给出风险建议（deny/ask/allow）。仅建议："
                         "最终授权由确定性 Policy 与审批裁决，决策不得放宽。",
            criteria={"deny": "建议拒绝", "ask": "建议人工审批", "allow": "建议放行"},
        )
        result = await self._decide(subject, question, run_id)
        self._audit.record(
            self._tenant, "decision.shadow", subject=subject, run_id=run_id,
            detail={"gate": "risk", "tool": tool, "args_hash": args_hash,
                    "risks": risk_tags,
                    "status": result.status, "value": result.value,
                    "provider": self.provider, "shadow": self._shadow},
        )
        return result

    async def _decide(self, subject: str, question: ChoiceQuestion,
                      run_id: str | None) -> DecisionResult:
        if self.active is None:
            return DecisionResult(
                question_id=question.id, status="abstained", kind=question.kind,
                reason="decision layer disabled",
            )
        try:
            results = await self.active.decide(
                {"subject": subject, "run_id": run_id}, [question])
            first: DecisionResult = results[0]
            return first
        except AgentOSError:
            raise
        except Exception as exc:  # noqa: BLE001 - 后端故障 → 弃权（不阻塞原链路）
            self._audit.record(
                self._tenant, "decision.error", subject=subject, run_id=run_id,
                detail={"question": question.id, "error": str(exc)[:160]},
            )
            return DecisionResult(
                question_id=question.id, status="abstained", kind=question.kind,
                reason=f"backend error: {type(exc).__name__}",
            )
