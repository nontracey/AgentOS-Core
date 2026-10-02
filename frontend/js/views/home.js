/* U5-04 工作区首页（新任务）：一句话目标 + 可理解的示例 + 继续上次的工作。
 * 只展示服务端确实执行的能力：不伪装有"执行方式"开关，不显示未记录的指标。 */
"use strict";

import { mountComposer } from "../composer.js";
import { esc } from "../safe-export.js";
import { emptyState, icon, modelZh, stateBadge, timeShort, toast } from "../ui.js";

import { ATTENTION_STATES, ACTIVE_STATES, taskTitle } from "../workspace-data.js";

const EXAMPLES = [
  { goal: "根据附上的项目记录，整理本周进展、阻塞和下一步，生成一份有来源的进展报告",
    title: "整理一份项目周报", sub: "读取授权材料 → 生成报告 → 结构与引用检查",
    tag: "资料报告模板 · 交付 Markdown", toMaterials: true, requires: "material.read" },
  { goal: "先列出本任务材料，检查附上的 CSV 的行列、空值和数值统计，解释数据问题，生成一份 Markdown 检查报告",
    title: "检查一份数据表", sub: "添加 CSV → 检查数据 → 保存成果", requires: "data.csv_profile", glyph: "blocks" },
  { goal: "检查当前可用的工具与技能，并说明各自能完成什么任务",
    title: "看看系统能做什么", sub: "读取能力清单 → 汇总可执行范围" },
  { goal: "查看可用的图片生成能力，先告诉我需要你提供哪些输入",
    title: "准备一次视觉生成", sub: "确认参数前不会执行外部生成", requires: "image.generate" },
];

export async function renderWorkspaceHome(ctx) {
  const { el, api, navigate } = ctx;
  let stopped = false;
  const revise = Boolean(ctx.revise);
  const source = revise ? sessionStorage.getItem("agentos_revise_source") || "" : "";
  const parent = revise ? sessionStorage.getItem("agentos_revise_asset") || "" : "";
  el.innerHTML = `
    <section class="home">
      <div class="home-greet">${icon("sparkles", 16)} AGENTOS WORKSPACE</div>
      <h1>今天，我们完成什么？</h1>
      <p class="home-sub">描述目标，按需添加材料。需要你确认时，会在任务里提醒。</p>
      ${revise ? `<div class="notice">继续修改上一版报告，授权材料会沿用；原成果保留。</div>` : ""}
      <div id="home-composer"></div>
      <div class="context-line">
        <span>${icon("shield", 13)} 重要操作由服务端裁定，执行前请你确认</span>
        <span id="home-cap">${icon("node", 13)} 执行环境读取中…</span>
      </div>
      <div class="home-status" id="home-status" aria-label="任务概况"></div>
      <details class="home-examples" open><summary>从一个实用场景开始</summary>
        <div class="example-grid" id="home-examples"></div>
      </details>
      <div class="home-recent">
        <div class="subhead"><h3>继续上次的工作</h3>
          <a href="#/tasks">全部任务 ${icon("arrow", 13)}</a></div>
        <div id="home-runs"><div class="skeleton" style="height:56px"></div></div>
      </div>
    </section>`;

  const draft = sessionStorage.getItem("agentos_goal_draft") || "";
  const composer = mountComposer(el.querySelector("#home-composer"), {
    api, scope: source ? `revise_${source}` : "new", goal: draft,
    template: revise ? "material-report" : "", contextRun: source, parentAsset: parent,
    onAccepted: r => {
      sessionStorage.removeItem("agentos_goal_draft");
      sessionStorage.removeItem("agentos_revise_source");
      sessionStorage.removeItem("agentos_revise_asset");
      navigate(`#/tasks/${r.run_id}`);
    },
  });
  if (draft) { composer.setGoal(draft, revise); sessionStorage.removeItem("agentos_goal_draft"); }
  function showExamples(tools) {
    const available = new Set(tools.filter(t => t.available !== false).map(t => t.name));
    const examples = EXAMPLES.filter(ex => !ex.requires || available.has(ex.requires));
    const box = el.querySelector("#home-examples");
    box.innerHTML = examples.map((ex, i) => `<button class="example" data-example="${i}">
      ${icon(ex.glyph || (ex.toMaterials ? "doc" : ex.requires ? "image" : "blocks"), 18)}
      <strong>${esc(ex.title)}</strong><span>${esc(ex.sub)}</span>
      <small>${esc(ex.tag || "目标驱动 · 按授权执行")}</small></button>`).join("");
    box.querySelectorAll("[data-example]").forEach(btn => btn.addEventListener("click", () => {
      const ex = examples[Number(btn.dataset.example)];
      composer.setGoal(ex.goal, Boolean(ex.toMaterials));
    }));
  }
  let capabilityCount = null;
  showExamples([]);
  api.capabilityCatalog().then(c => { if (!stopped) { capabilityCount=(c.tools || []).filter(t=>t.available!==false).length; showExamples(c.tools || []); const n=el.querySelector("#home-cap-count");if(n)n.textContent=capabilityCount; } }).catch(() => {});

  async function loadRuns() {
    if (stopped) return false;
    try {
      const runs = await api.listRuns();
      if (stopped) return false;
      const needs = runs.filter(r => ATTENTION_STATES.has(r.state)).length;
      const active = runs.filter(r => ACTIVE_STATES.has(r.state)).length;
      el.querySelector("#home-status").innerHTML = `<a href="#/inbox">${icon("shield", 18)}<span><b>${needs}</b><small>需要我处理</small></span>${icon("arrow", 14)}</a>
        <a href="#/tasks">${icon("activity", 18)}<span><b>${active}</b><small>正在进行</small></span>${icon("arrow", 14)}</a>
        <a href="#/hub">${icon("blocks", 18)}<span><b id="home-cap-count">${capabilityCount ?? "—"}</b><small>可用能力</small></span>${icon("arrow", 14)}</a>`;
      const box = el.querySelector("#home-runs");
      box.innerHTML = runs.length ? runs.slice(0, 4).map((r) => `
        <button class="recent-row" data-run="${esc(r.id)}">
          <span class="rtext"><b>${esc(taskTitle(r))}</b>
            <small>${esc(r.id)} · 最近活动 ${timeShort(r.updated_at || r.created_at)}</small></span>
          ${stateBadge(r.state)}</button>`).join("")
        : emptyState(icon("folder", 24), "还没有任务",
            "在上方写下目标，或用「添加材料 / 使用模板」从资料报告开始。");
      box.querySelectorAll("[data-run]").forEach((row) => {
        row.addEventListener("click", () => navigate(`#/tasks/${row.dataset.run}`));
      });
      return true;
    } catch (e2) {
      toast(e2.message, "err");
      return false;
    }
  }
  await loadRuns();

  try {
    const cap = await api.getCapabilities();
    const nodes = cap.nodes || [];
    const paired = nodes.filter((n) => n.status === "paired").length;
    el.querySelector("#home-cap").innerHTML = `${icon("node", 13)} `
      + `<b>${esc(modelZh(cap.model?.name))}</b> · ${paired} 个已配对执行设备 · `
      + (cap.model?.provider === "scripted"
        ? "本轮不产生真实云端调用" : "已配置，连通性以任务实测为准");
  } catch (_) {
    el.querySelector("#home-cap").innerHTML = `${icon("node", 13)} 执行环境未读取到配置`;
  }

  return { poll: loadRuns, destroy: () => { stopped = true; composer.destroy(); } };
}
