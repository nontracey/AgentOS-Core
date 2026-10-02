/* U5-04 任务空间：全部任务 / 需要我处理；数据来自 /v1/runs（服务端按角色过滤）。 */
"use strict";

import { esc } from "../safe-export.js";
import { emptyState, icon, stateBadge, timeShort, toast } from "../ui.js";

import { ATTENTION_STATES as NEEDS, filterRuns, taskTitle } from "../workspace-data.js";

export async function renderTasks(ctx, hooks = {}) {
  const { el, api, navigate } = ctx;
  let stopped = false, filter = "all", query = "", runs = [];

  el.innerHTML = `
    <section class="page">
      <div class="page-heading">
        <div><h2>任务空间</h2><p>任务会持续保存，回来就能继续。</p></div>
        <a class="btn primary" href="#/home">${icon("plus", 15)} 新任务</a>
      </div>
      <div class="tabs" role="tablist" aria-label="任务筛选">
        <button data-filter="all" class="on" role="tab">全部任务</button>
        <button data-filter="attention" role="tab">需要我处理</button>
        <span class="tab-spacer"></span>
        <input id="tk-q" class="input-quiet" type="search" placeholder="搜索目标…"
          aria-label="按目标搜索">
      </div>
      <div id="tk-rows" class="rows"><div class="skeleton" style="height:64px"></div></div>
    </section>`;

  function visible() {
    return filterRuns(runs, filter, query);
  }

  async function load(silent = false) {
    if (stopped) return false;
    try {
      runs = await api.listRuns();
      if (stopped) return false;
      const attention = runs.filter((r) => NEEDS.has(r.state)).length;
      el.querySelector('[data-filter="attention"]').textContent
        = attention ? `需要我处理 · ${attention}` : "需要我处理";
      const rows = visible();
      const box = el.querySelector("#tk-rows");
      box.innerHTML = rows.length ? rows.map((r) => `
        <button class="row-item" data-run="${esc(r.id)}">
          <span class="row-main"><b>${esc(taskTitle(r))}</b>
            <p>最近活动 ${timeShort(r.updated_at || r.created_at)}</p></span>
          <span class="row-side">${stateBadge(r.state)}</span>
        </button>`).join("")
        : emptyState(icon("folder", 26),
          filter === "attention" ? "现在没有需要你处理的任务" : "还没有任务",
          filter === "attention"
            ? "待确认、待核对或失败的任务会出现在这里。"
            : "从一句话目标开始，系统会记录全过程与成果。");
      box.querySelectorAll("[data-run]").forEach((row) => row.addEventListener("click",
        () => navigate(`#/tasks/${row.dataset.run}`)));
      hooks.refreshRecents?.();
      return true;
    } catch (e) {
      if (!silent) toast(e.message, "err");
      return false;
    }
  }

  el.querySelectorAll("[data-filter]").forEach((btn) => btn.addEventListener("click", () => {
    filter = btn.dataset.filter;
    el.querySelectorAll("[data-filter]").forEach((b) => b.classList.toggle("on", b === btn));
    load(true);
  }));
  let timer = null;
  el.querySelector("#tk-q").addEventListener("input", (e) => {
    query = e.target.value;
    clearTimeout(timer);
    timer = setTimeout(() => load(true), 250);
  });

  await load();
  return { poll: () => load(true), destroy: () => { stopped = true; clearTimeout(timer); } };
}
