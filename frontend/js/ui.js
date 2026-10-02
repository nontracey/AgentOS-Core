/* 共用 UI 组件（计划 §3.1 基础组件）：全部输出 HTML 字符串，动态文本经 esc 转义。 */
"use strict";

import { esc } from "./safe-export.js";

/* 线条图标（内联 SVG，24 viewBox，stroke=currentColor，无外部依赖） */
const ICON_PATHS = {
  arrow: "M5 12h14M13 6l6 6-6 6",
  refresh: "M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0112-1l2 6M18 17a7 7 0 01-12 1l-2-6",
  node: "M3 4h18v12H3zM8 21h8M12 16v5",
  flow: "M4 3h6v6H4zM14 15h6v6h-6zM7 9v9h7",
  home: "M3 11l9-8 9 8M5 9.5V21h14V9.5",
  plus: "M12 5v14M5 12h14",
  doc: "M7 3h7l5 5v13H7zM14 3v5h5",
  gear: "M12 9a3 3 0 100 6 3 3 0 000-6z M12 2v3M12 19v3M2 12h3M19 12h3M4.6 4.6l2.1 2.1M17.3 17.3l2.1 2.1M19.4 4.6l-2.1 2.1M6.7 17.3l-2.1 2.1",
  clock: "M12 3a9 9 0 100 18 9 9 0 000-18zM12 7v5l3 3",
  download: "M12 3v12M7 11l5 5 5-5M4 21h16",
  upload: "M12 21V9M7 13l5-5 5 5M4 3h16",
  shield: "M12 3l8 3v6c0 5-3.5 7.5-8 9-4.5-1.5-8-4-8-9V6z",
  // U5 工作区新增（对话/执行图/能力中心/通知）
  layers: "M4 7l8-4 8 4-8 4zM4 12l8 4 8-4M4 17l8 4 8-4",
  files: "M6 3h8l4 4v14H6zM14 3v4h4M3 8v13h3",
  blocks: "M4 4h7v7H4zM13 4h7v4h-7zM13 11h7v9h-7zM4 14h7v6H4z",
  bell: "M7 17a5 5 0 0110 0M6 17h12l1 3H5zM12 5v-2",
  info: "M12 3a9 9 0 100 18 9 9 0 000-18zM12 10v8M12 7.5v.5",
  quote: "M9 8H5v6h4M19 8h-4v6h4M9 14c0 2-1 3-3 4M19 14c0 2-1 3-3 4",
  cpu: "M8 8h8v8H8zM9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3",
  sparkles: "M12 3l1.8 4.7L18 9.5l-4.2 1.8L12 16l-1.8-4.7L6 9.5l4.2-1.8zM18 15l.9 2.1L21 18l-2.1.9L18 21l-.9-2.1L15 18l2.1-.9z",
  branch: "M7 4v9a3 3 0 003 3h4M7 4a2 2 0 100 .01M17 17a2 2 0 100 .01",
  image: "M4 5h16v14H4zM8 13l3-3 4 4 2-2 3 3",
  play: "M8 5l11 7-11 7z",
  folder: "M3 7h6l2 2h10v10H3z",
  search: "M11 4a7 7 0 100 14 7 7 0 000-14zM16.5 16.5L21 21",
  send: "M12 20V4M5 11l7-7 7 7",
  menu: "M4 7h16M4 12h16M4 17h16",
  close: "M6 6l12 12M18 6L6 18",
  check: "M5 13l4 4L19 7",
  warnsign: "M12 4l8 15H4zM12 10v4M12 16.5v.5",
  eye: "M2 12s4-6 10-6 10 6 10 6-4 6-10 6-10-6-10-6zM12 10a2 2 0 100 4 2 2 0 000-4z",
  plug: "M9 3v6M15 3v6M7 9h10v3a5 5 0 01-10 0zM12 17v4",
};

export function icon(name, size = 20) {
  const d = ICON_PATHS[name] || ICON_PATHS.doc;
  return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none"
    stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"
    aria-hidden="true">${d.split(" M").map((seg, i) =>
      `<path d="${i === 0 ? seg : "M" + seg}"/>`).join("")}</svg>`;
}

export const STATE_LABELS = {
  created: "已受理", planning: "规划中", running: "运行中",
  awaiting_approval: "待审批", awaiting_input: "待补充资料", awaiting_review: "待评审", paused: "已暂停", cancel_requested: "取消中",
  succeeded: "已完成", failed: "失败", cancelled: "已取消", completed: "已完成",
  // 步骤级状态（执行步骤面板）
  prepared: "准备中", dispatched: "执行中", pending_external: "等待外部结果",
};

export const STATE_ICONS = {
  succeeded: "✓", completed: "✓", failed: "✕", awaiting_approval: "⏸", paused: "⏸",
  running: "spinner", planning: "spinner", cancel_requested: "spinner",
  dispatched: "spinner", prepared: "spinner",
};

export function stateBadge(state) {
  const label = STATE_LABELS[state] || state || "—";
  const icon = STATE_ICONS[state];
  const inner = icon === "spinner"
    ? `<span class="spin"></span>${esc(label)}`
    : `<span class="dot"></span>${esc(label)}`;
  return `<span class="state-badge ${esc(state || "")}">${inner}</span>`;
}

/** 会话/授权状态的文字标签（不用"运行中"这类执行状态词误报访问状态）。 */
export function accessBadge(status) {
  const map = { active: ["有效", "good"], expired: ["已过期", ""], revoked: ["已撤销", ""] };
  const [label, tone] = map[status] || [status || "未知", ""];
  return `<span class="badge ${tone}">${esc(label)}</span>`;
}

/** 模型名呈现：脚本模型说清是验证环境，不伪装成真模型调用。 */
export function modelZh(name) {
  const model = String(name || "");
  if (!model) return "模型名称未记录";
  if (/scripted/i.test(model)) return "脚本模型（验证环境）";
  return model;
}

export function timeShort(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return esc(String(iso));
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
    return sameDay ? hm
      : `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
  } catch (_) { return esc(String(iso)); }
}

export function taskCard(run, { showCost = true } = {}) {
  const cost = run.cost?.settled;
  return `
  <div class="task-card" data-run="${esc(run.id)}" role="button" tabindex="0"
       aria-label="打开任务：${esc(run.goal)}">
    <span class="task-type-icon">${icon("flow",18)}</span>
    <div style="min-width:0;flex:1">
      <div class="goal">${esc(run.goal)}</div>
      <div class="meta">
        <span>${esc(run.id)}</span><span>最近活动 ${timeShort(run.updated_at || run.created_at)}</span>
        ${showCost && cost ? `<span>成本 ${fmtCost(cost)}</span>` : ""}
      </div>
    </div>
    <div class="right">${stateBadge(run.state)}</div>
  </div>`;
}

export function timelineItem({ tone = "", title, time, detail = "" }) {
  return `
  <div class="tl-row">
    <div class="tl-dot ${esc(tone)}"></div>
    <div class="tl-body">
      <div class="tl-title">${title}${time ? `<span class="time">${timeShort(time)}</span>` : ""}</div>
      ${detail ? `<div class="tl-detail">${detail}</div>` : ""}
    </div>
  </div>`;
}

export function approvalCard(a) {
  // 真实 API 的 summary 形如 "tool=fs.write_text risks=['write']"——解析成人话展示
  const toolMatch = /\btool=([^\s]+)/.exec(a.summary || "");
  const tool = a.tool || (toolMatch ? toolMatch[1] : "—");
  const risksMatch = /risks=\[([^\]]*)\]/.exec(a.summary || "");
  const rawRisks = a.risks || (risksMatch ? risksMatch[1] : "");
  const risks = String(rawRisks).replace(/[ '"[\]]/g, "").split(",").filter(Boolean)
    .map((r) => ({ write: "本地写入", egress: "对外发送", exec: "执行命令",
      cost: "产生用量", gpu: "设备占用" }[r.trim()] || r.trim()))
    .join(" / ");
  const friendly = ["fs.write_text", "artifact.create"].includes(tool) ? "保存成果文件" : /video.*generate/.test(tool) ? "生成视频" : /image.*generate/.test(tool) ? "生成图片" : tool;
  const what = `执行「${friendly}」需要你的确认`;
  return `
  <div class="approval-card" data-approval="${esc(a.id)}">
    <div class="what">${esc(what)}</div>
    <div class="impact">批准后实际执行并继续任务${risks ? ` · 风险：${esc(risks)}` : ""}。</div>
    <div class="why">${a.reason ? `${esc(a.reason)} ｜ ` : ""}服务端策略要求人工确认 ｜ 到期：${timeShort(a.expires_at)}</div>
    <div class="actions">
      <button class="btn primary small" data-decide="approve" data-id="${esc(a.id)}">批准执行</button>
      <button class="btn danger small" data-decide="reject" data-id="${esc(a.id)}">拒绝</button>
    </div>
  </div>`;
}

export function assetTitle(a) {
  const name = String(a.name || a.id || '成果');
  const generated = name.match(/^[a-f0-9-]{12}_(.+)_[a-f0-9]{10}\.(png|mp4)$/i);
  if (!generated) return name;
  return `${generated[2].toLowerCase() === 'mp4' ? '视频成果' : '图片成果'} · ${generated[1]}`;
}

export function artifactCard(a, { previewable = false } = {}) {
  const isMd = String(a.mime || "").includes("markdown") || String(a.mime || "").includes("text");
  return `
  <div class="artifact-card">
    <div class="icon">${isMd ? "📄" : "📦"}</div>
    <div style="min-width:0">
      <div class="name">${esc(assetTitle(a))}</div>
      <div class="meta">${esc(a.mime || "—")} · ${Number(a.size || 0).toLocaleString()} 字节
        ${a.sha256 ? `· sha256 ${esc(String(a.sha256).slice(0, 12))}…` : ""}</div>
    </div>
    <div class="actions">
      ${previewable ? `<a class="btn small" href="#/artifacts/${esc(a.id)}">预览</a>` : ""}
      <button class="btn small" data-download="${esc(a.id)}">下载</button>
    </div>
  </div>`;
}

export function emptyState(icon, title, desc, action = "") {
  return `
  <div class="empty-state">
    <div class="icon">${icon}</div>
    <div class="title">${esc(title)}</div>
    <div class="desc">${esc(desc)}</div>
    ${action}
  </div>`;
}

export function errorState(message, retry = true) {
  return `
  <div class="error-state">
    <div class="icon">⚠️</div>
    <div class="title">出错了</div>
    <div class="desc">${esc(message)}</div>
    ${retry ? `<button class="btn" onclick="location.reload()">重试</button>` : ""}
  </div>`;
}

export function skeletonRows(n = 3) {
  return Array.from({ length: n }, () =>
    `<div class="task-card" aria-hidden="true"><div style="flex:1">
       <div class="skeleton" style="height:16px;width:60%;margin-bottom:8px"></div>
       <div class="skeleton" style="height:12px;width:40%"></div>
     </div></div>`).join("");
}

let toastWrap = null;
export function toast(message, kind = "") {
  if (!toastWrap) {
    toastWrap = document.createElement("div");
    toastWrap.className = "toast-wrap";
    document.body.appendChild(toastWrap);
  }
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  toastWrap.appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

export function confirmDialog({ title, body, confirmText = "确认", danger = false }) {
  return new Promise((resolve) => {
    const mask = document.createElement("div");
    mask.className = "dialog-mask";
    mask.innerHTML = `
      <div class="dialog" role="dialog" aria-modal="true" aria-label="${esc(title)}">
        <h3>${esc(title)}</h3>
        <p class="muted small">${esc(body)}</p>
        <div class="dialog-actions">
          <button class="btn" data-act="cancel">取消</button>
          <button class="btn ${danger ? "danger" : "primary"}" data-act="ok">${esc(confirmText)}</button>
        </div>
      </div>`;
    mask.addEventListener("click", (e) => {
      const act = e.target.closest("[data-act]")?.dataset.act;
      if (act === "ok") { mask.remove(); resolve(true); }
      else if (act === "cancel" || e.target === mask) { mask.remove(); resolve(false); }
    });
    document.body.appendChild(mask);
  });
}

export function fmtCost(microusd) {
  // 账本单位为 microusd（百万分之一美元）；展示为可读美元金额
  if (microusd == null) return "未记录";
  const usd = microusd / 1_000_000;
  if (usd === 0) return "$0";
  if (usd >= 0.01) return `$${usd.toFixed(2)}`;
  if (usd >= 0.0001) return `$${usd.toFixed(4)}`;
  return `$${usd.toFixed(6)}`;
}


/** Translate known service errors; technical codes remain available in advanced details. */
export function humanError(raw) {
  let text;
  try { const value = typeof raw === "string" ? JSON.parse(raw) : raw;
    text = String(value?.message || value?.detail?.message || value?.code || ""); }
  catch { text = String(raw || ""); }
  const cases = [
    [/provider_rate_limited/i, "模型服务限流或额度不足，任务已暂停。请稍后恢复，继续使用原任务与执行记录。"],
    [/report contains dates or amounts absent/i, "报告出现无法从授权材料核对的日期或金额。请补充材料或修改要求后重新生成。"],
    [/report exceeds requested length/i, "报告超出字数限制，压缩后仍未通过检查。请调整篇幅要求后重试。"],
    [/rate.limit|returned 429/i, "模型请求受到限流，请稍后重试。"],
    [/citation locator check failed/i, "报告引用的行号无法回查，未交付文件。请重新生成或补充材料。"],
    [/wait for the current task/i, "当前任务还在执行，请等本轮结束后提交修改。"],
    [/context task not found|run not found/i, "任务不存在或当前账号没有读取权限。"],
    [/selected skill unavailable/i, "选定的技能已停用或不可用，请重新选择。"],
    [/model endpoint unreachable|model call failed/i, "模型调用未完成，请检查连接后重试。"],
    [/not your task|not your context|not authorized/i, "当前账号没有访问这项资源的权限。"],
  ];
  return cases.find(([pattern]) => pattern.test(text))?.[1] || text.slice(0,400) || "操作未完成，请稍后重试。";
}
