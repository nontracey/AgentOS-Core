/* U5-04/U5-06 对话投影：把服务端真实的 run / steps / events 组装成对话条目。
 *
 * 边界（计划 §3.2、U4 计划 §3）：
 * - 只写有证据的内容；缺字段写「未记录」，不补造进度、百分比或模型内部推理；
 * - 原始 JSON 不进对话（管理员高级详情另行提供）；游客面只渲染白名单标量；
 * - 终态由服务端 run.state 决定，前端不自行判定"完成"。
 */
"use strict";

import { capabilityLabel } from "./workspace-data.js";
import { esc } from "./safe-export.js";
import { approvalCard, assetTitle, humanError, icon, modelZh, stateBadge, timeShort } from "./ui.js";

const TOOL_LABELS = {
  fs_write_text: "保存成果文件", fs: "写入沙箱文件", read: "读取授权材料",
  math_add: "核算数值", image: "生成图片", video: "生成视频", knowledge: "检索授权知识",
};
const ROLE_LABELS = {
  coordinator: "协调", research: "研究", design: "设计", review: "评审", executor: "执行",
};

/** 工具名呈现：服务端名为证据，中文标签仅换读法，不描述未记录的能力。 */
export function toolLabel(name) {
  const tool = String(name || "");
  if (!tool) return "工具调用";
  const label=capabilityLabel({name:tool}); if(/^(artifact\.|data\.|material\.)/.test(tool) && label!==tool)return label;
  if (/^skill\./i.test(tool)) return "加载任务技能";
  if (/video/i.test(tool)) return "生成视频";
  if (/image|comfyui/i.test(tool)) return "生成图片";
  if (/write/i.test(tool)) return "保存成果文件";
  if (/read/i.test(tool)) return "读取授权材料";
  if (/knw|knowledge|search/i.test(tool)) return "检索授权知识";
  if (/math/i.test(tool)) return "核算数值";
  if (/node\.(push|fetch)|upload|download/i.test(tool)) return "与执行设备传递文件";
  if (/report|template/i.test(tool)) return "生成报告正文";
  for (const [key, label] of Object.entries(TOOL_LABELS)) {
    if (tool.includes(key)) return label;
  }
  return tool;
}

function toolTitle(step) {
  if (String(step.id).startsWith("tpl1_context_")) return "读取上一版报告";
  return toolLabel(step.tool_name || step.id);
}

/** 检查明细：服务端英文固定文案换成中文结论，判定不变。 */
export function checkDetailZh(kind, detail) {
  const text = String(detail || "");
  if (/all required sections present/i.test(text)) return "必备章节齐备";
  if (/missing sections/i.test(text)) return `缺少章节：${text.replace(/^missing sections: ?/i, "")}`;
  if (/citations within authorized materials/i.test(text)) return "引用均在授权材料内";
  if (/no citation to any authorized material/i.test(text)) return "有材料但报告未引用";
  if (/citations outside authorized materials/i.test(text)) return "存在授权材料之外的引用";
  if (/material does not exist/i.test(text)) return "引用了不存在的材料";
  return text ? text.slice(0, 120) : "结论已记录，明细见执行事件";
}

const APPROVAL_STATES = {
  pending: "等待裁定", approved: "已批准（仅本次调用）", rejected: "已拒绝",
  consumed: "已按本次授权执行", expired: "已过期",
};

/** 审批摘要人话化：服务端 summary 形如 tool=fs.write_text risks=['write']。 */
export function approvalZh(a) {
  const summary = String(a.summary || "");
  const toolMatch = /\btool=([^\s]+)/.exec(summary);
  const tool = toolLabel(a.tool || (toolMatch ? toolMatch[1] : ""));
  const risksMatch = /risks=\[([^\]]*)\]/.exec(summary);
  const raw = String(a.risks || (risksMatch ? risksMatch[1] : "") || "");
  const risks = raw.replace(/[ '"[\]]/g, "").split(",").filter(Boolean)
    .map((r) => ({ write: "本地写入", egress: "对外发送", exec: "执行命令",
      cost: "产生用量", gpu: "设备占用" }[r] || r));
  return {
    what: tool ? `执行「${tool}」` : "本次调用需要裁定",
    risks,
    state: APPROVAL_STATES[a.state] || a.state || "已记录",
  };
}

function roleOf(step, events) {
  const prepared = events.findLast((e) => e.kind === "tool.prepared" && e.payload?.step_id === step.id);
  const fromEvent = prepared?.payload?.role || step.role_id || "";
  if (!fromEvent) return "";
  return ROLE_LABELS[fromEvent] || String(fromEvent);
}

function deviceOf(step, events) {
  const route = events.findLast((e) => e.kind === "node.dispatched" && e.payload?.step_id === step.id);
  if (!route) {
    const planned = events.findLast(e => e.kind === "plan.task_started" && e.payload?.tool === step.tool_name)?.payload;
    return planned?.node_id ? `计划指定 ${planned.node_id} · 尚未派发` : "本地设备";
  }
  const os = route.payload?.os || "远程";
  return `${os} 执行设备`;
}

/**
 * 组装对话条目。
 * @param {object} detail 服务端任务详情（admin: /v1/tasks/{id}；guest: /v1/demo/runs/{id}）
 * @param {Array} events 已按游标累积的事件（admin: trace；guest: 授权投影）
 * @param {object} opts { guest:false, showPrompts:true }
 */
export function projectConversation(detail, events, opts = {}) {
  const guest = Boolean(opts.guest);
  const run = detail.run || {};
  const steps = detail.steps || [];
  const artifacts = detail.artifacts || [];
  const approvals = detail.approvals || [];
  const refs = (detail.refs || []).filter((r,i,all) => all.findIndex(x => x.source_id === r.source_id
    && (x.locator || x.chunk_id) === (r.locator || r.chunk_id) && x.quote === r.quote) === i);
  const plan = detail.plan;
  const items = [];

  items.push({ type: "user", text: run.goal_text || run.goal || run.id || "（目标未记录）" });

  // 受理叙述：只说服务端确实做的事，不预测结果
  const planned = plan && (plan.tasks || []).length;
  const intro = guest
    ? "这次演示任务由管理员发布，以下为脱敏执行链路。"
    : `已受理目标${planned ? `，按 ${plan.source === "template" ? "固定模板" : "已保存计划"} 的 ${plan.tasks.length} 个步骤执行` : "；本次未保存结构化计划，按实际工具调用展开"}。`;
  items.push({ type: "narration", text: intro, tone: "intro" });

  // 计划摘要（有真实计划才显示）
  if (planned) {
    items.push({
      type: "plan", source: plan.source === "template" ? "固定模板" : "已保存计划",
      steps: (plan.tasks || []).map((t) => t.title || t.task_id || "步骤"),
    });
  }

  // 工具与交接：按事件时间顺序穿插，避免"先列表后叙述"的重复
  const order = [];
  for (const step of steps) {
    const first = events.findIndex((e) => e.payload?.step_id === step.id);
    order.push({ kind: "step", ref: step, at: first < 0 ? 1e9 : first });
  }
  for (const usage of events.filter((e) => e.kind === "model.usage")) {
    order.push({ kind: "usage", ref: usage, at: events.indexOf(usage) });
  }
  for (const check of [...new Map(events.filter((e) => e.kind.startsWith("report.") && e.kind.endsWith("_check")).map(e => [e.kind,e])).values()]) {
    order.push({ kind: "check", ref: check, at: events.indexOf(check) });
  }
  order.sort((a, b) => a.at - b.at);

  for (const entry of order) {
    if (entry.kind === "step") {
      const step = entry.ref;
      const prepared = events.findLast((e) => e.kind === "tool.prepared" && e.payload?.step_id === step.id);
      const route = events.findLast((e) => e.kind === "node.dispatched" && e.payload?.step_id === step.id);
      items.push({
        type: "tool",
        id: step.id,
        title: toolTitle(step),
        sub: [roleOf(step, events), deviceOf(step, events)].filter(Boolean).join(" · "),
        policy: { allowed: "策略裁定：允许执行", needs_approval: "策略裁定：需人工确认",
                  denied: "策略裁定：拒绝执行" }[prepared?.payload?.policy] || "策略裁定未记录",
        state: step.state,
        attempt: Number(step.attempt || 1),
        time: step.finished_at || step.started_at,
      });
      if (route) {
        items.push({ type: "handoff", role: roleOf(step, events) || "执行",
                     device: deviceOf(step, events), tool: route.payload?.tool || "",
                     basis: route.payload?.basis || "路由依据未记录" });
      }
    } else if (entry.kind === "usage") {
      const p = entry.ref.payload || {};
      items.push({
        type: "metric",
        label: `模型调用 · ${modelZh(p.model)}`,
        detail: `输入 ${p.input_tokens ?? "未记录"} / 输出 ${p.output_tokens ?? "未记录"} tokens`,
      });
    } else {
      const p = entry.ref.payload || {};
      items.push({
        type: "check",
        title: ({ "report.structure_check": "报告结构检查", "report.citation_check": "来源引用检查",
                  "report.fact_check": "日期与金额检查", "report.length_check": "字数检查" })[entry.ref.kind] || "报告检查",
        passed: Boolean(p.passed), detail: entry.ref.kind === "report.length_check"
          ? `${p.chars} / ${p.limit} 字` : entry.ref.kind === "report.fact_check"
            ? "核对授权材料中的日期和金额" : checkDetailZh(entry.ref.kind, p.detail),
      });
    }
  }

  // 审批：嵌在任务内（不是独立菜单）；游客只看状态，无操作按钮
  for (const a of approvals) {
    items.push({ type: "approval", data: a, guest });
  }

  // 成果：有版本才有卡；媒体与文本分别用可核对的信息描述
  for (const a of artifacts) {
    items.push({
      type: "artifact", data: a, guest,
      downloadable: guest ? Boolean(a.download) : true,
      previewable: guest ? Boolean(a.preview) : true,
    });
  }

  // 引用片段：游客只见已发布片段；管理员见 run 记录的引用（不跳整库）
  if (refs.length) {
    items.push({ type: "sources", refs: refs.map((r) => ({
      source: r.source_id, locator: r.chunk_id || "", quote: r.quote || "",
    })) });
  }

  // 生成成功但回传失败：只提供"补传已有文件"，不重跑生成
  const failedDelivery = steps.filter((s) => {
    let value;
    try { value = JSON.parse(s.result_json || "{}").value; } catch { return false; }
    return value?.delivery === "pending";
  });
  if (failedDelivery.length) {
    items.push({ type: "failure",
      title: "成果已生成，回传未完成",
      body: "生成结果已确认。补传使用原文件，不重新执行生成。",
      action: "deliver" });
  }

  // 终态叙述：只按服务端状态说事实
  if (run.state === "succeeded") {
    const final = guest ? "" : (detail.messages?.at(-1)?.content || events.findLast((e) => e.payload?.final)?.payload?.final || "");
    if (final) items.push({ type: "answer", text: final });
  } else if (run.state === "failed") {
    items.push({ type: "narration", tone: "err",
      text: `任务未完成：${safeError(run.error_json) || "服务端记录了失败原因"}` });
  } else if (run.state === "awaiting_approval") {
    items.push({ type: "narration", tone: "warn",
      text: guest ? "这一步等待管理员裁定，演示为只读。" : "等待你的确认后继续；批准只执行本次调用。" });
  } else if (run.state === "paused") {
    const unresolved = steps.some((step) => step.state === "uncertain");
    items.push({ type: "narration", tone: "warn", text: unresolved
      ? "外部执行结果未知，需要先核对再决定下一步。"
      : run.error_json ? humanError(run.error_json) : "任务已暂停，等待处理后继续。" });
  } else if (run.state === "cancelled") {
    items.push({ type: "narration", text: "服务端已确认停止。" });
  } else if (["created", "planning", "running", "cancel_requested"].includes(run.state)) {
    items.push({ type: "live", text: run.state === "cancel_requested"
      ? "已请求取消，等待服务端确认停止。" : "任务进行中，进展按事件持续写入。" });
  }
  return items;
}

function safeError(raw) { return raw ? humanError(raw) : ""; }

/* ---------------- 渲染（全部经 esc，不内嵌原始 JSON） ---------------- */

export function renderConversation(items) {
  return items.map(renderItem).join("");
}

function renderItem(item) {
  switch (item.type) {
    case "user":
      return `<div class="msg-user-label">你</div><div class="msg-user">${esc(item.text)}</div>`;
    case "narration":
      return `<div class="msg-agent"><span class="agent-mark">${icon("flow", 18)}</span><span>AgentOS</span></div>
        <p class="msg-body ${esc(item.tone || "")}">${esc(item.text)}</p>`;
    case "plan":
      return `<div class="plan-summary"><b>${esc(item.source)}</b>${item.steps.slice(0, 6)
        .map((s) => `<span>${esc(s)}</span>`).join("")}${
        item.steps.length > 6 ? `<span>+${item.steps.length - 6}</span>` : ""}</div>`;
    case "tool":
      return `<button class="tool-row" data-tool="${esc(item.id)}">
        <span class="tool-glyph">${icon("cpu", 14)}</span>
        <span class="tool-text"><b>${esc(item.title)}</b>
          <small>${esc(item.sub || "本地执行")}${item.attempt > 1 ? ` · 第 ${item.attempt} 次尝试` : ""}
            · ${esc(item.policy)}</small></span>
        ${stateBadge(item.state)}${item.time ? `<small class="tool-time">${timeShort(item.time)}</small>` : ""}
      </button>`;
    case "handoff":
      return `<div class="handoff-row">${icon("arrow", 14)}
        <span>交接：<b>${esc(item.role)}</b> → <b>${esc(item.device)}</b>
        ${item.tool ? ` · 工具 ${esc(item.tool)}` : ""}</span>
        <small>${esc(item.basis)}</small></div>`;
    case "metric":
      return `<div class="note-row">${icon("sparkles", 14)}<span>${esc(item.label)}</span>
        <small>${esc(item.detail)}</small></div>`;
    case "check":
      return `<div class="note-row ${item.passed ? "ok" : "err"}">
        ${icon(item.passed ? "check" : "warnsign", 14)}
        <span>${esc(item.title)}${item.passed ? "通过" : "未通过"}</span>
        <small>${esc(item.detail)}</small></div>`;
    case "approval":
      if (item.guest) {
        const zh = approvalZh(item.data);
        return `<div class="confirm-readonly">${icon("shield", 15)}
          <span>这一步由管理员裁定：<b>${esc(zh.what)}</b>
            ${zh.risks.length ? ` · 风险：${esc(zh.risks.join(" / "))}` : ""}
            （${esc(zh.state)}）· 演示空间为只读</span></div>`;
      }
      return approvalCard(item.data);
    case "artifact": {
      const a = item.data;
      const label = esc(assetTitle(a));
      const meta = `${esc(a.mime || "—")} · ${Number(a.size || 0).toLocaleString()} 字节`
        + (a.version ? ` · v${esc(String(a.version))}` : "");
      const actions = [
        item.previewable
          ? `<button class="btn small" data-preview-artifact="${esc(a.id)}">${icon("eye", 14)} 预览</button>` : "",
        item.downloadable
          ? `<button class="btn small" data-artifact-download="${esc(a.id)}" data-name="${esc(a.name || a.id)}">${
              icon("download", 14)} 下载</button>`
          : `<span class="muted small">${item.guest ? "下载未开放" : ""}</span>`,
      ].join("");
      return `<button class="inline-artifact" data-open-artifact="${esc(a.id)}">
        <span class="file-symbol">${icon(item.guest ? "doc" : "doc", 18)}</span>
        <span class="artifact-body"><b>${label}</b><small>${meta}</small></span>
        ${icon("arrow", 16)}
      </button>${actions ? `<div class="artifact-actions">${actions}</div>` : ""}`;
    }
    case "sources":
      return `<div class="sources">${item.refs.map((r) => `<button class="source-chip"
        data-source="${esc(r.source)}" data-locator="${esc(r.locator)}"
        data-quote="${esc(r.quote)}">${icon("quote", 12)} 来源片段 ${esc(r.source)}${
        r.locator ? ` · ${esc(r.locator)}` : ""}</button>`).join("")}</div>`;
    case "failure":
      return `<div class="failure-card"><b>${esc(item.title)}</b><p>${esc(item.body)}</p>
        <button class="btn small" data-action="${esc(item.action)}">${icon("refresh", 14)} 补传已有成果</button></div>`;
    case "answer":
      return `<div class="msg-body final-answer-text"><b>任务答复</b><span>${esc(item.text)}</span></div>`;
    case "live":
      return `<div class="live-row"><span class="live-dot"></span><span>${esc(item.text)}</span></div>`;
    default:
      return "";
  }
}
