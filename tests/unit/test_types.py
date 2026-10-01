"""边界类型校验（蓝图 §5/§6.1/§11）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentos.kernel.decision import DecisionResult, NoulQuestion, ScoreQuestion
from agentos.kernel.dto import PlannedTask, TaskPlan
from agentos.kernel.types import ChatMessage, ModelToolCall, ToolSpec


# ---------------------------------------------------------------- ChatMessage
def test_tool_message_requires_tool_call_id() -> None:
    with pytest.raises(ValidationError):
        ChatMessage(role="tool", content="{}")


def test_only_assistant_may_carry_tool_calls() -> None:
    with pytest.raises(ValidationError):
        ChatMessage(role="user", content="hi", tool_calls=[ModelToolCall(id="x", name="t", arguments={})])


def test_tool_call_ids_must_be_unique() -> None:
    dup = ModelToolCall(id="x", name="t", arguments={})
    with pytest.raises(ValidationError):
        ChatMessage(role="assistant", content="", tool_calls=[dup, dup])


def test_tool_result_must_not_carry_calls() -> None:
    with pytest.raises(ValidationError):
        ChatMessage(
            role="tool",
            content="{}",
            tool_call_id="x",
            tool_calls=[ModelToolCall(id="y", name="t", arguments={})],
        )


# -------------------------------------------------------------------- ToolSpec
def test_tool_spec_timeout_positive() -> None:
    with pytest.raises(ValidationError):
        ToolSpec(name="t", version="1", input_schema={}, timeout_ms=0)


# -------------------------------------------------------------------- TaskPlan
def _task(task_id: str, depends: list[str] | None = None) -> PlannedTask:
    return PlannedTask(task_id=task_id, role_id="worker", depends_on=depends or [])


def test_task_plan_rejects_empty() -> None:
    with pytest.raises(ValidationError):
        TaskPlan(plan_id="p", version=1, goal_id="g", goal_version=1, tasks=[])


def test_task_plan_rejects_unknown_dependency() -> None:
    with pytest.raises(ValidationError, match="unknown task"):
        TaskPlan(plan_id="p", version=1, goal_id="g", goal_version=1, tasks=[_task("a", ["ghost"])])


def test_task_plan_rejects_cycle() -> None:
    with pytest.raises(ValidationError, match="cycle"):
        TaskPlan(
            plan_id="p",
            version=1,
            goal_id="g",
            goal_version=1,
            tasks=[_task("a", ["b"]), _task("b", ["a"])],
        )


def test_task_plan_accepts_diamond_dag() -> None:
    plan = TaskPlan(
        plan_id="p",
        version=1,
        goal_id="g",
        goal_version=1,
        tasks=[_task("a"), _task("b", ["a"]), _task("c", ["a"]), _task("d", ["b", "c"])],
    )
    assert len(plan.tasks) == 4


# -------------------------------------------------------------- DecisionResult
def test_choice_requires_full_distribution_and_confidence() -> None:
    with pytest.raises(ValidationError, match="confidence"):
        DecisionResult(
            question_id="q1", status="answered", kind="choice", value="a",
            probabilities={"a": 1.0},
        )
    with pytest.raises(ValidationError, match="sum"):
        DecisionResult(
            question_id="q1", status="answered", kind="choice", value="a",
            probabilities={"a": 0.6, "b": 0.6}, confidence=0.9,
        )


def test_noul_has_no_independent_confidence() -> None:
    with pytest.raises(ValidationError, match="confidence"):
        DecisionResult(
            question_id="q2", status="answered", kind="noul", value=0.8, confidence=0.7
        )
    ok = DecisionResult(question_id="q2", status="answered", kind="noul", value=0.8)
    assert ok.value == 0.8


def test_abstained_requires_reason_and_no_value() -> None:
    with pytest.raises(ValidationError, match="reason"):
        DecisionResult(question_id="q3", status="abstained", kind="noul")
    with pytest.raises(ValidationError, match="value"):
        DecisionResult(
            question_id="q3", status="abstained", kind="noul", value=0.5, reason="no rule"
        )


def test_score_requires_legend() -> None:
    with pytest.raises(ValidationError, match="legend"):
        DecisionResult(question_id="q4", status="answered", kind="score", value=2.5)
    ok = DecisionResult(
        question_id="q4", status="answered", kind="score", value=2.5, legend=["low", "mid", "high"]
    )
    assert ok.legend == ["low", "mid", "high"]


def test_question_discriminators() -> None:
    assert NoulQuestion(id="n", instructions="i").kind == "noul"
    assert ScoreQuestion(id="s", instructions="i", criteria=["a"]).kind == "score"
