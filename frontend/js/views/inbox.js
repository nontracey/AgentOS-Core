/* U5-05 通知：把需要本人处理的事项汇总到一个入口（确认仍嵌在任务内）。
 * 来源为服务端事实：待裁审批 + 待核对/失败任务；不新建后端状态，也不预测进度。 */
"use strict";

import { esc } from "../safe-export.js";
import { emptyState, icon, stateBadge, timeShort } from "../ui.js";

export async function renderInbox(ctx) {
  const { el, api, navigate } = ctx;
  let stopped = false;
  el.innerHTML = `
    <section class="page">
      <div class="page-heading">
        <div><h2>需要你处理</h2><p>确认出现在对应任务里，这里只负责找到它。</p></div>
        <a class="btn" href="#/tasks">任务空间</a>
      </div>
      <div id="ib-body"><div class="skeleton" style="height:120px"></div></div>
    </section>`;

  async function load() {
    if (stopped) return false;
    const [approvals, runs] = await Promise.all([api.listApprovals(), api.listRuns()]);
    if (stopped) return false;
    const needRuns = runs.filter((r) => ["awaiting_approval", "paused", "failed"].includes(r.state));
    const goalOf = (runId) => runs.find((r) => r.id === runId)?.goal || runId;

    const blocks = [];
    if (approvals.length) {
      blocks.push(`<div class="inbox-group">
        <div class="subhead"><h3>等待确认的操作 <span class="tag">${approvals.length}</span></h3></div>
        ${approvals.map((a) => {
    const runId = String(a.call_id || "").split(":")[0];
    return `<button class="row-item" data-open="${esc(runId)}">
              <span class="row-main"><b>${esc(goalOf(runId))}</b>
                <p>${esc(a.summary || "服务端要求确认本次调用")} · 到期 ${timeShort(a.expires_at)}</p></span>
              <span class="row-side">${stateBadge("awaiting_approval")}</span></button>`;
  }).join("")}</div>`);
    }
    if (needRuns.length) {
      blocks.push(`<div class="inbox-group">
        <div class="subhead"><h3>需要核对或重跑的任务</h3></div>
        ${needRuns.map((r) => `<button class="row-item" data-open="${esc(r.id)}">
          <span class="row-main"><b>${esc(r.goal || r.id)}</b>
            <p>${esc(r.state === "paused" ? "外部执行结果未知，先核对再决定下一步"
              : r.state === "failed" ? "任务未完成，查看失败步骤与服务端原因"
                : "等待你的批准")} · 最近活动 ${timeShort(r.updated_at)}</p></span>
          <span class="row-side">${stateBadge(r.state)}</span></button>`).join("")}</div>`);
    }
    el.querySelector("#ib-body").innerHTML = blocks.join("")
      || emptyState(icon("check", 26), "现在没有需要你处理的事项",
        "需要确认、结果待核对或失败的任务会汇总到这里。");
    el.querySelectorAll("[data-open]").forEach((row) => row.addEventListener("click",
      () => navigate(`#/tasks/${row.dataset.open}`)));
    return true;
  }

  await load();
  return { poll: () => load(), destroy: () => { stopped = true; } };
}
