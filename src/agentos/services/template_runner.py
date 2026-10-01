"""内置资料报告模板（N3a，计划 §2.1/§2.2）。

范围：一个内置、版本固定的"资料整理与报告交付"模板。目标/材料经任务入口进入
（payload.template + payload.materials，N2a 契约），执行复用同一账本/Policy/审批：

- 材料读取走 material.read 工具步骤（execute_tool_step：账本+幂等；只能读本 run
  授权材料——越权读取在 AssetService.read_run_material 服务端拒绝）；
- 模型生成经预算预留/结算与 usage 事件（同 AgentLoop 口径）；
- 报告落盘走 fs.write_text 工具步骤（默认需审批，不旁路写盘）；
- 结构检查与引用检查**分别**确定性执行并记录（模型自评通过不代替检查）；
- Asset 登记由服务端核对已完成步骤后执行，持久关联 run/subject/来源；
  修改（parent_asset_id + revision_reason）产生不可变新版本。

恢复语义：阶段进度存 checkpoint——审批唤醒/重启后已完成阶段不重做；
模型生成文本持久化在 checkpoint，重入不重复调用模型。
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from agentos.kernel.dto import TaskPlan
from agentos.kernel.errors import AgentOSError
from agentos.kernel.states import RunState, StepState
from agentos.kernel.store import Lease, RunRecord
from agentos.kernel.types import AuthContext, ChatMessage, ModelRequest
from agentos.runtime.executor import RuntimeDeps, execute_tool_step
from agentos.services.assets import AssetService
from agentos.services.observability import prompt_evidence

TEMPLATE_ID = "material-report"
TEMPLATE_VERSION = "1"
REQUIRED_SECTIONS = ["## 摘要", "## 关键结论", "## 证据与引用", "## 未决项"]
CITATION_RE = re.compile(r"\bS\d+\b")
# 思考型模型的 reasoning tokens 计入 max_tokens：默认 2048 可能不够，
# 可用 AGENTOS_TEMPLATE_MAX_OUTPUT_TOKENS 调大（真实模型盲跑建议 4096）
MAX_REPORT_OUTPUT_TOKENS = int(os.environ.get("AGENTOS_TEMPLATE_MAX_OUTPUT_TOKENS", "2048"))

_SYSTEM_PROMPT = (
    "你是资料报告助手。只能依据用户提供的授权材料撰写报告，禁止编造金额、日期、"
    "数量或材料中不存在的事实。建议也不得引入材料之外的具体金额、日期、人数或里程，使用待确认代替。"
    "报告必须包含以下四个章节（Markdown 二级标题）：\n"
    "## 摘要\n## 关键结论\n## 证据与引用\n## 未决项\n"
    "引用格式：在正文与引用表中用材料代号及真实行号标注来源（如 S1 L1、S2 L1-L2）。"
    "行号由材料前缀给出，不能编造段落号或定位符。"
    "材料代号在用户消息中给出。用户指定字数时，四章合计（包括标题和引用）都须遵守，尽量留20%的余量。"
    "避免在摘要、结论与引用表反复复述同一事实，引用表可用简洁列表。若材料不足以回答目标中的问题，在相关章节明确写"
    "“材料不足”，并把原问题保留在“未决项”，不得给出编造答案。下一步建议必须标注“建议”。"
)


class TemplateRunner:
    def __init__(
        self, deps: RuntimeDeps, model: Any, assets: AssetService,
        *, microusd_per_1k_input: int = 0, microusd_per_1k_output: int = 0, router: Any = None,
        token_budget: int = 64_000,
    ) -> None:
        self._deps = deps
        self._model = model
        self._assets = assets
        self._in_rate = microusd_per_1k_input
        self._out_rate = microusd_per_1k_output
        self._router = router
        self._token_budget = token_budget
        self._engine_driver: Any = None

    # ------------------------------------------------------------------ 执行
    async def execute(self, run: RunRecord, lease: Lease, *, finish_run: bool = True) -> RunRecord:
        store = self._deps.store
        tenant = run.tenant_id
        envelope = json.loads(run.envelope_json)
        payload = envelope.get("payload", {})
        actor = AuthContext.model_validate(envelope["actor"])
        materials: list[dict[str, Any]] = [*(payload.get("materials") or []),
            *json.loads(run.checkpoint_json or "{}").get("collaboration", {}).get("additional_materials", [])]
        parent_asset_id = payload.get("parent_asset_id") or None
        revision_reason = str(payload.get("revision_reason") or "")

        cp = json.loads(run.checkpoint_json) if run.checkpoint_json else {}
        state = cp.setdefault("template", {})

        if payload.get("skills") and not state.get("skill_body"):
            outcome = await execute_tool_step(
                self._deps, store.get_run(tenant, run.id) or run,
                "tpl0_skill_material_report", "skill.read", {"skill_id": "material-report"}, lease,
            )
            if outcome.state != StepState.COMPLETED:
                return self._fail(run, "report skill could not be loaded")
            state["skill_body"] = str((outcome.result.value if outcome.result else {}).get("body") or "")
            self._save(run, cp)

        # ---- 阶段 1：材料读取（工具步骤；只读本 run 授权材料）----
        if not state.get("materials"):
            loaded: list[dict[str, Any]] = []
            for m in materials:
                step_id = f"tpl1_read_{m['asset_id']}"
                outcome = await execute_tool_step(
                    self._deps, store.get_run(tenant, run.id) or run,
                    step_id, "material.read", {"asset_id": m["asset_id"]}, lease,
                )
                if outcome.state in {StepState.FAILED, StepState.UNCERTAIN}:
                    return self._fail(run, f"material read failed: {m['asset_id']}")
                if outcome.state != StepState.COMPLETED:
                    return store.get_run(tenant, run.id) or run
                value = outcome.result.value if outcome.result else {}
                loaded.append({**m, "content": str(value.get("content") or "")})
            state["materials"] = loaded
            self._save(run, cp)

        if payload.get("context_asset") and not state.get("previous_report"):
            previous = payload["context_asset"]
            outcome = await execute_tool_step(
                self._deps, store.get_run(tenant, run.id) or run,
                f"tpl1_context_{previous['asset_id']}", "material.read",
                {"asset_id": previous["asset_id"]}, lease,
            )
            if outcome.state != StepState.COMPLETED:
                return self._fail(run, "previous report could not be read")
            state["previous_report"] = str((outcome.result.value if outcome.result else {}).get("content") or "")
            self._save(run, cp)

        # ---- 阶段 2：模型生成（文本持久化；重入不重复调用）----
        if not state.get("report_text"):
            report = await self._generate(run, actor, str(payload.get("goal") or ""),
                                          state["materials"], state.get("previous_report", ""),
                                          payload.get("context", {}).get("goal", ""), state.get("skill_body", ""),
                                          lease=lease)
            if report is None:
                return store.get_run(tenant, run.id) or run
            state["raw_report_text"] = report
            state["report_text"] = self._format_report(report)
            self._save(run, cp)

        limit_match = re.search(r"不超过\s*(\d+)\s*字", str(payload.get("goal") or ""))
        if limit_match:
            limit = int(limit_match.group(1))
            while True:
                count = len(re.sub(r"\s", "", state["report_text"]))
                store.event_append(tenant, run.id, "report.length_check",
                                   {"chars": count, "limit": limit, "passed": count <= limit})
                if count <= limit:
                    break
                attempt = int(state.get("length_reworks", 0))
                if attempt >= 2:
                    return self._fail(run, f"report exceeds requested length after two reworks: {count}/{limit}")
                state["length_reworks"] = attempt + 1
                self._save(run, cp)
                store.event_append(tenant, run.id, "report.rework_requested", {
                    "reason": "length_limit", "round": attempt + 1, "chars": count, "limit": limit,
                })
                revised = await self._generate(
                    run, actor, str(payload.get("goal") or "")
                    + f"。检查发现上一稿{count}字超限，请压缩到{max(1, limit * 2 // 3)}字以内，四章和引用保留。",
                    state["materials"], state["report_text"], str(payload.get("goal") or ""),
                    state.get("skill_body", ""), generation=attempt + 2, lease=lease,
                )
                if revised is None:
                    return store.get_run(tenant, run.id) or run
                state["raw_report_text"] = revised
                state["report_text"] = self._format_report(revised)
                self._save(run, cp)

        # ---- 阶段 4：确定性检查（结构 / 引用，分别记录）----
        report_text = state["report_text"]
        struct = self._check_structure(report_text)
        evidence_text = state.get("raw_report_text", report_text)
        cites = self._check_citations(evidence_text, len(materials))
        store.event_append(tenant, run.id, "report.structure_check", struct)
        store.event_append(tenant, run.id, "report.citation_check", cites)
        self._deps.audit.record(
            tenant, "report.checked", subject=run.subject, run_id=run.id,
            detail={"structure": struct["passed"], "citations": cites["passed"]},
        )
        if not struct["passed"]:
            return self._fail(run, f"structure check failed: {struct['detail']}")
        if not cites["passed"]:
            return self._fail(run, f"citation check failed: {cites['detail']}")
        refs: list[dict[str, Any]] = []
        try:
            for alias in cites["found"]:
                material = materials[int(alias[1:]) - 1]
                locators = re.findall(rf"\b{alias}\s+(L\d+(?:-L?\d+)?)", evidence_text)
                # Older reports may cite the material without a line number; resolve the whole source.
                line_count = max(1, len(state["materials"][int(alias[1:]) - 1]["content"].splitlines()))
                locators = locators or [f"L1-L{line_count}"]
                for locator in dict.fromkeys(locators):
                    ref = self._assets.resolve_run_reference(actor, run.id, material["asset_id"], locator)
                    refs.append({**ref, "alias": alias, "chunk_id": locator})
        except AgentOSError as exc:
            return self._fail(run, f"citation locator check failed: {exc.message}")
        source_text = "\n".join(m["content"] for m in state["materials"])
        def dates(text: str) -> set[str]:
            values = set(re.findall(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)", text))
            values.update(f"{int(y):04}-{int(m):02}-{int(d):02}"
                          for y, m, d in re.findall(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日", text))
            return values

        unsupported_dates = dates(evidence_text) - dates(source_text)
        def amounts(text: str) -> set[float]:
            return {float(number) * {"": 1, "万": 10000, "亿": 100000000}[unit]
                    for number, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(万|亿)?\s*元", text)}

        unsupported_amounts = amounts(evidence_text) - amounts(source_text)
        facts = {"passed": not unsupported_dates and not unsupported_amounts,
                 "detail": "exact dates and CNY amounts checked against authorized material",
                 "unsupported_dates": sorted(set(unsupported_dates)),
                 "unsupported_amounts": sorted(unsupported_amounts)}
        store.event_append(tenant, run.id, "report.fact_check", facts)
        if unsupported_dates or unsupported_amounts:
            return self._fail(run, "report contains dates or amounts absent from authorized material")
        store.event_append(tenant, run.id, "report.references", {"refs": refs})
        state["refs"] = refs
        self._save(run, cp)

        # ---- 阶段 3：写盘（fs.write_text 工具步骤，默认需审批 → 阻塞返回）----
        if not state.get("written"):
            path = f"reports/{run.id}/report.md"
            outcome = await execute_tool_step(
                self._deps, store.get_run(tenant, run.id) or run,
                f"tpl3_write_{run.id}", "fs.write_text",
                {"path": path, "content": state["report_text"]}, lease,
            )
            if outcome.state == StepState.AWAITING_APPROVAL:
                store.transition_run(
                    tenant, run.id, {RunState.RUNNING, RunState.CREATED},
                    RunState.AWAITING_APPROVAL,
                )
                return store.get_run(tenant, run.id) or run
            if outcome.state in {StepState.FAILED, StepState.UNCERTAIN}:
                return self._fail(run, "report write failed or uncertain")
            state["written"] = path
            self._save(run, cp)

        # ---- 阶段 5：登记不可变成果（服务端核对已完成步骤后执行）----
        name = "report.md"
        try:
            reg = self._assets.register_report_version(
                actor,
                data=report_text.encode("utf-8"),
                name=name, run_id=run.id,
                parent_asset_id=parent_asset_id,
                revision_reason=revision_reason,
                mime="text/markdown",
                meta={"template": TEMPLATE_ID, "template_version": TEMPLATE_VERSION,
                      "materials": [m["asset_id"] for m in materials], "refs": refs},
            )
        except AgentOSError as exc:
            return self._fail(run, f"asset registration failed: {exc.message}")

        delivery = {
            "kind": "material_report", "template": TEMPLATE_ID,
            "template_version": TEMPLATE_VERSION,
            "asset_id": reg["asset_id"], "sha256": reg["sha256"],
            "version": reg.get("version"), "parent_asset_id": parent_asset_id,
            "revision_reason": revision_reason,
            "materials": [m["asset_id"] for m in materials],
            "checks": {"structure": struct, "citations": cites},
            "cost": store.budget_totals(tenant, run.id),
        }
        cp["delivery"] = delivery
        self._save(run, cp)
        store.event_append(tenant, run.id, "report.delivered", delivery)
        if finish_run:
            store.transition_run(
                tenant, run.id, {RunState.RUNNING}, RunState.SUCCEEDED,
                event_detail={"delivery": delivery},
            )
        return store.get_run(tenant, run.id) or run

    # ------------------------------------------------------------------ 生成
    async def _generate(
        self, run: RunRecord, actor: AuthContext, goal: str,
        materials: list[dict[str, Any]],
        previous_report: str = "", original_goal: str = "", skill_body: str = "", generation: int | str = 1,
        *, lease: Lease,
    ) -> str | None:
        store = self._deps.store
        tenant = run.tenant_id
        # 材料代号：S1..Sn（报告引用检查依据同一代号集）
        mat_blocks = []
        for i, m in enumerate(materials, start=1):
            numbered = "\n".join(f"L{n}: {line}" for n, line in enumerate(m.get("content", "").splitlines(), 1))
            mat_blocks.append(f"材料 S{i}（{m.get('name')}，"
                              f"sha256 {str(m.get('sha256'))[:12]}…）：\n{numbered}")
        user_content = (
            f"[[task:{goal}]]\n目标：{goal}\n\n"
            f"以下是本次授权材料（共 {len(materials)} 份，代号 S1..S{len(materials)}）：\n\n"
            + "\n\n".join(mat_blocks)
            + "\n\n请按系统要求输出完整 Markdown 报告。"
        )
        if previous_report:
            user_content += (f"\n\n原目标：{original_goal}\n上一版报告（仅用于修改，"
                             f"不是新事实来源）：\n{previous_report}\n本次修改要求：{goal}")
        collaboration = json.loads(run.checkpoint_json or "{}").get("collaboration", {})
        visual = collaboration.get("tasks", {}).get("design-1", {}).get("output", {})
        if visual.get("delivery") == "registered":
            user_content += ("\n服务端执行事实：视觉已经由 Windows 节点生成并校验回传，"
                             f"成果 {visual.get('asset_id')}，sha256 {visual.get('sha256')}。"
                             "可以引用这项执行事实，不要再要求生成或谎称尚无设计稿。"
                             "方案中预算分配、人数、里程都用待确认，不填任何新增数值。")
        messages = [
            ChatMessage(role="system", content=_SYSTEM_PROMPT + "\n固定报告技能：\n" + skill_body),
            ChatMessage(role="user", content=user_content),
        ]
        if self._engine_driver is not None:
            node_id = f"template-generation-{generation}"
            await self._engine_driver.execute(run, lease, node_id=node_id,
                goal="\n\n".join(m.content for m in messages), tools=[], finish_run=False)
            current = store.get_run(tenant, run.id) or run
            node = json.loads(current.checkpoint_json or "{}").get("runtime_nodes", {}).get(node_id, {})
            return str(node["answer"]) if node.get("state") == "completed" else None
        caps = await self._model.capabilities()
        context_limit = int(caps.get("max_context_tokens") or 8192)
        upper_bound = sum(len(m.content.encode("utf-8")) + 16 for m in messages)
        if upper_bound + MAX_REPORT_OUTPUT_TOKENS > context_limit:
            self._fail(run, "材料与修改上下文超过当前模型容量，请缩短材料后重新提交；未调用模型")
            return None
        cursor, used_tokens = 0, 0
        while True:
            batch = store.event_list(tenant, run.id, after_seq=cursor, limit=1000)
            if not batch:
                break
            used_tokens += sum(int(e["payload"].get("input_tokens") or 0) +
                               int(e["payload"].get("output_tokens") or 0)
                               for e in batch if e["kind"] == "model.usage")
            cursor = batch[-1]["seq"]
        if used_tokens + upper_bound + MAX_REPORT_OUTPUT_TOKENS > self._token_budget:
            self._fail(run, "任务 token 预算不足，未继续调用模型或生成副作用")
            return None
        call_id = f"{run.id}:tpl:generate:{generation}"
        estimate = MAX_REPORT_OUTPUT_TOKENS * (self._in_rate + self._out_rate) // 1000
        try:
            self._deps.budget.reserve(tenant, run.id, call_id, estimate)
        except AgentOSError as exc:
            self._fail(run, f"budget reservation denied: {exc.message}")
            return None
        store.event_append(tenant, run.id, "model.request", prompt_evidence(messages, call_id))
        usage = None
        model_name = None
        content = ""
        started = time.perf_counter()
        store.event_append(tenant, run.id, "answer.start", {"call_id": call_id})
        delta_count = 0
        try:
            request = ModelRequest(messages=messages, tools=[], max_output_tokens=MAX_REPORT_OUTPUT_TOKENS)
            async def on_fallback(frm: str, to: str, reason: str) -> None:
                store.event_append(tenant, run.id, "model.fallback", {"from": frm, "to": to, "reason": reason})

            async def on_provider(name: str) -> None:
                store.event_append(tenant, run.id, "model.provider", {"name": name})

            data_class = str(json.loads(run.envelope_json).get("payload", {}).get("data_class", "approved_private"))
            if (data_class == "local_only" and self._router is None
                    and getattr(self._model, "provider", "") != "scripted"):
                from agentos.kernel.errors import ErrorCode

                raise AgentOSError(ErrorCode.DENIED, "no compliant provider for local_only report")
            stream = (self._router.stream_events(request, actor, data_class=data_class,
                                                on_fallback=on_fallback, on_provider=on_provider)
                      if self._router else self._model.stream(request, actor))
            async for event in stream:
                model_name = event.get("model") or model_name
                if event["type"] == "text_delta":
                    content += event["delta"]
                    delta_count += 1
                    store.event_append(tenant, run.id, "answer.delta", {
                        "call_id": call_id, "delta": str(event["delta"]),
                        "seq_in_call": delta_count,
                    })
                elif event["type"] == "key_rotation":
                    store.event_append(tenant, run.id, "model.key_rotation",
                                       {k: v for k, v in event.items() if k != "type"})
                elif event["type"] == "usage":
                    usage = event["usage"]
                    if event.get("model"):
                        model_name = event["model"]
        except AgentOSError as exc:
            self._deps.budget.release(tenant, call_id)
            self._fail(run, f"model call failed: {exc.message}")
            return None
        store.event_append(tenant, run.id, "answer.end", {
            "call_id": call_id, "deltas": delta_count, "chars": len(content),
        })
        store.event_append(tenant, run.id, "model.completed", {
            "call_id": call_id, "model": model_name,
            "latency_ms": round((time.perf_counter() - started) * 1000),
        })
        actual = 0
        if usage is not None:
            actual = (usage.input_tokens * self._in_rate + usage.output_tokens * self._out_rate) // 1000
            self._deps.budget.settle(tenant, call_id, actual)
            # 模型名随 usage 记录（禁静默降级）：时间线/工作台必须能回答"用的什么模型"
            usage_payload = usage.model_dump(mode="json")
            usage_payload["call_id"] = call_id
            usage_payload["pricing_configured"] = bool(self._in_rate or self._out_rate)
            if model_name:
                usage_payload["model"] = model_name
            store.event_append(tenant, run.id, "model.usage", usage_payload)
        else:
            self._deps.budget.settle(tenant, call_id, 0)
        if not content.strip():
            self._fail(run, "model returned empty report")
            return None
        return content

    # ------------------------------------------------------------------ 检查
    @classmethod
    def _format_report(cls, text: str) -> str:
        text = re.sub(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日",
                      lambda m: f"{int(m[1]):04}-{int(m[2]):02}-{int(m[3]):02}", text)
        return cls._compact_citation_section(text)

    @staticmethod
    def _compact_citation_section(text: str) -> str:
        """Source bytes are resolved in metadata; avoid quoting them twice in the short report.

        Raw generation is retained and checked, so normalization cannot hide fabricated evidence.
        """
        match = re.search(r"(## 证据与引用[^\n]*\n)(.*?)(?=\n## |\Z)", text, re.DOTALL)
        if not match:
            return text
        cited: list[str] = []
        for alias in sorted(set(CITATION_RE.findall(match.group(2)))):
            lines = re.findall(rf"\b{alias}\s+(L\d+(?:-L?\d+)?)", match.group(2))
            cited.extend(f"{alias} {line}" for line in dict.fromkeys(lines)) if lines else cited.append(alias)
        if not cited:
            return text
        return text[:match.start(2)] + "；".join(cited) + "\n" + text[match.end(2):]

    @staticmethod
    def _check_structure(text: str) -> dict[str, Any]:
        missing = [s for s in REQUIRED_SECTIONS if s not in text]
        return {
            "passed": not missing,
            "missing": missing,
            "detail": "all required sections present" if not missing
                      else f"missing sections: {', '.join(missing)}",
        }

    @staticmethod
    def _check_citations(text: str, material_count: int) -> dict[str, Any]:
        allowed = {f"S{i}" for i in range(1, material_count + 1)}
        found = set(CITATION_RE.findall(text))
        fabricated = sorted(found - allowed)
        if fabricated:
            return {
                "passed": False, "found": sorted(found), "allowed": sorted(allowed),
                "detail": "citations outside authorized materials: " + ", ".join(fabricated),
            }
        if material_count > 0 and not (found & allowed):
            return {
                "passed": False, "found": [], "allowed": sorted(allowed),
                "detail": "no citation to any authorized material while materials exist",
            }
        if material_count == 0 and found:
            return {
                "passed": False, "found": sorted(found), "allowed": [],
                "detail": "citations present but no material was authorized",
            }
        return {
            "passed": True, "found": sorted(found), "allowed": sorted(allowed),
            "detail": "citations within authorized materials",
        }

    # ------------------------------------------------------------------ helpers
    def _fail(self, run: RunRecord, message: str) -> RunRecord:
        store = self._deps.store
        store.transition_run(
            run.tenant_id, run.id,
            {RunState.RUNNING, RunState.CREATED, RunState.AWAITING_APPROVAL},
            RunState.FAILED,
            error_json=json.dumps({"code": "template_failed", "message": message},
                                  ensure_ascii=False),
        )
        self._deps.audit.record(
            run.tenant_id, "template.failed", subject=run.subject, run_id=run.id,
            detail={"message": message},
        )
        return store.get_run(run.tenant_id, run.id) or run

    def _save(self, run: RunRecord, checkpoint: dict[str, Any]) -> None:
        latest = self._deps.store.get_run(run.tenant_id, run.id)
        if latest:
            nodes = json.loads(latest.checkpoint_json or "{}").get("runtime_nodes")
            if nodes is not None:
                checkpoint["runtime_nodes"] = nodes
        self._deps.store.save_checkpoint(
            run.tenant_id, run.id, json.dumps(checkpoint, ensure_ascii=False)
        )


_ = TaskPlan  # 类型引用保留（模板步骤展示复用 plan 结构）
