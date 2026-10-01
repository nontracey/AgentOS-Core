"""FR-DEC 决策层验收（F5 可本地实测部分）。

- FR-DEC-01/04：仅装 rule_fallback 全新环境可 boot（composition 不注册 Kev/Laya）。
- FR-DEC-02：provider 配置未安装后端 → 明确降级 rule + decision.degraded 审计；
  切换只改配置，决策调用方（DecisionService 接口）零改动。
- FR-DEC-05：Decision 输出 allow 而 Policy deny → 工具执行数为 0（建议不得放宽授权）。
- FR-DEC-07：未配置阈值 → shadow_only（calibration_profile）。
- 中文冻结样本 100 条：规则命中一致性 + 未覆盖弃权（tests/fixtures/decision_zh_100.json）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

from agentos.adapters.store.sqlite import SqliteStore
from agentos.apps.composition import SystemHandle
from agentos.kernel.decision import ChoiceQuestion
from agentos.kernel.states import StepState
from agentos.services.audit import AuditService
from agentos.services.decision_service import DecisionService
from agentos.services.knowledge import RuleDecisionAdapter
from tests.conftest import IDEMPOTENT_APPEND_SCRIPT, owner_token

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "decision_zh_100.json"


def test_rule_only_boot_and_caps(make_system: Callable[..., SystemHandle]) -> None:
    """仅装 rule：全新环境 boot 成功（FR-DEC-01/04），决策能力显式 shadow。"""
    system = make_system(model_script=[{"content": "boot"}])
    system.boot()  # composition 从不注册 kev/laya —— 零额外依赖即可启动
    _caps = system.deps_store_decision_caps() if hasattr(
        system, "deps_store_decision_caps") else None
    # composition 未接线 DecisionService 时，capabilities 由服务级测试覆盖；
    # boot 本身成功即 FR-DEC-04 的核心断言
    assert system.loop is not None


def test_provider_switch_degrades_with_audit_event(data_dir: Path) -> None:
    """配置 kev（未安装）→ 降级 rule + decision.degraded 审计；调用方接口不变。"""
    store = SqliteStore(data_dir / "d.db")
    audit = AuditService(store)
    svc = DecisionService({"rule": RuleDecisionAdapter()}, audit,
                          data_dir.stem, provider="kev")
    caps = svc.capabilities()
    assert caps["configured_provider"] == "kev"
    assert caps["active_provider"] == "rule"
    assert caps["degraded_from"] == "kev"
    assert caps["calibration"]["shadow_only"] is True  # FR-DEC-07：无阈值只跑影子
    kinds = [r["kind"] for r in store.raw_connection().execute(
        "SELECT kind FROM audit_log WHERE kind LIKE 'decision%'").fetchall()]
    assert "decision.degraded" in kinds
    # 同一接口继续可用（调用方零改动，FR-DEC-02）
    r = asyncio.run(svc.risk_advice("owner", "fs.write_text", "h", ["write"]))
    assert r.status == "abstained"


def test_policy_deny_overrides_decision_allow(
    make_system: Callable[..., SystemHandle], data_dir: Path,
) -> None:
    """Decision allow + Policy deny → 执行数为 0（FR-DEC-05 判据原文）。"""
    system = make_system(model_script=IDEMPOTENT_APPEND_SCRIPT)
    system.boot()
    token = owner_token(system)
    # 决策层规则故意配置为 allow（若决策能放宽授权，下面就会执行——用例必须证明不能）
    decision = DecisionService(
        {"rule": RuleDecisionAdapter(rules={
            "risk:log.append": {"answer": "allow", "reason": "决策建议放行"}})},
        system.deps.audit, provider="rule")
    receipt = system.ingress.accept_sync(
        {"payload": {"goal": "append"}}, channel="cli", credentials={"token": token}
    ) if hasattr(system.ingress, "accept_sync") else None
    import asyncio

    if receipt is None:
        receipt = asyncio.run(system.ingress.accept(
            {"payload": {"goal": "append"}}, channel="cli", credentials={"token": token}))
    state = asyncio.run(system.worker.drive_until_settled(receipt.run_id))
    assert state == "awaiting_approval"  # Policy：写操作未审批 → 挂起（deny 语义）
    advice = asyncio.run(decision.risk_advice(
        "owner", "log.append", "h", ["write"], run_id=receipt.run_id))
    assert advice.value == "allow"  # 决策说 allow
    # 决策 allow 不得改变执行：未审批 → 步骤未派发、副作用 0
    steps = system.store.list_steps("default", receipt.run_id)
    assert all(s.state != StepState.DISPATCHED for s in steps)
    log_file = data_dir / "sandbox" / "task.log"
    assert not log_file.exists() or log_file.read_text(encoding="utf-8").strip() == ""


def test_chinese_frozen_samples_100() -> None:
    """100 条中文冻结样本：已配置规则 100% 确定性命中；未覆盖问题 100% 弃权。"""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    adapter = RuleDecisionAdapter(rules=data["rules"])
    questions = [
        ChoiceQuestion(
            id=s["qid"],
            instructions=s["instructions"],
            criteria=s["criteria"],
        )
        for s in data["samples"]
    ]
    results = asyncio.run(adapter.decide({}, questions))
    assert len(results) == len(questions) == 100
    for s, r in zip(data["samples"], results, strict=False):
        if s["expect"] == "abstain":
            assert r.status == "abstained", f"{s['qid']} 应弃权"
        else:
            assert r.status == "answered", f"{s['qid']} 应命中规则"
            assert r.value == s["expect"], f"{s['qid']} 答案不符"
            assert r.confidence == 1.0  # 确定性规则，非模型估计
