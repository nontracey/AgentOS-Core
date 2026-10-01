"""Decision Port 契约（蓝图 §11）。

三种题型原语：noul（是概率，无独立 confidence 字段）、choice（选中项+全概率分布+confidence）、
score（有序等级加权分数+legend+分布）。内部契约必须能表达三者，不得简化为 prompt/options/choice。
规则后端只覆盖声明的业务规则，未知问题返回弃权（abstained），不得伪造通用校准概率。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator


class ChoiceQuestion(BaseModel):
    id: str
    kind: Literal["choice"] = "choice"
    instructions: str
    criteria: dict[str, str]


class ScoreQuestion(BaseModel):
    id: str
    kind: Literal["score"] = "score"
    instructions: str
    criteria: list[str]


class NoulQuestion(BaseModel):
    id: str
    kind: Literal["noul"] = "noul"
    instructions: str
    criteria: dict[Literal["true", "false"], str] | None = None


DecisionQuestion = Annotated[
    ChoiceQuestion | ScoreQuestion | NoulQuestion, Field(discriminator="kind")
]


class DecisionResult(BaseModel):
    """按 kind/status 的判别约束：

    - choice: value=标签, probabilities=全分布(归一), confidence 必填
    - score:  value=加权分数, legend=有序等级, probabilities=分布
    - noul:   value=是概率(0-1)，不使用独立 confidence 字段
    - abstained: value/probabilities 为空，reason 必填
    """

    question_id: str
    status: Literal["answered", "abstained"]
    kind: Literal["choice", "score", "noul"]
    value: str | float | None = None
    probabilities: dict[str, float] | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    legend: list[str] | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> DecisionResult:
        if self.status == "abstained":
            if self.value is not None or self.probabilities is not None:
                raise ValueError("abstained result must not carry value/probabilities")
            if not self.reason:
                raise ValueError("abstained result requires reason")
            return self
        if self.kind == "choice":
            if not isinstance(self.value, str):
                raise ValueError("choice result requires a label value")
            if self.confidence is None:
                raise ValueError("choice result requires confidence")
            probs = self.probabilities or {}
            if not probs:
                raise ValueError("choice result requires full probability distribution")
            total = sum(probs.values())
            if abs(total - 1.0) > 1e-6:
                raise ValueError("choice probabilities must sum to 1")
            if self.value not in probs:
                raise ValueError("chosen label must appear in probability distribution")
        elif self.kind == "score":
            if not isinstance(self.value, (int, float)):
                raise ValueError("score result requires numeric weighted score")
            if not self.legend:
                raise ValueError("score result requires ordered legend")
        elif self.kind == "noul":
            if not isinstance(self.value, (int, float)) or not (0.0 <= float(self.value) <= 1.0):
                raise ValueError("noul result requires a 0-1 yes-probability in value")
            if self.confidence is not None:
                raise ValueError("noul must not carry an independent confidence field")
        return self


def calibration_snapshot(profile: dict[str, Any]) -> dict[str, Any]:
    """FR-DEC-07：暴露校准档案；未配置阈值时只允许影子模式。"""
    thresholds = profile.get("confidence_thresholds")
    shadow_only = not thresholds
    return {"shadow_only": shadow_only, **profile}
