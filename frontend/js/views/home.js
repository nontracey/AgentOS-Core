/* U5-04 工作区首页（新任务）：一句话目标 + 可理解的示例 + 继续上次的工作。
 * 只展示服务端确实执行的能力：不伪装有"执行方式"开关，不显示未记录的指标。 */
"use strict";

import { mountComposer } from "../composer.js";
import { esc } from "../safe-export.js";
import { emptyState, icon, modelZh, stateBadge, timeShort, toast } from "../ui.js";

const EXAMPLES = [
  { goal: "根据附上的项目记录，整理本周进展、阻塞和下一步，生成一份有来源的进展报告",
    title: "整理一份项目周报", sub: "读取授权材料 → 生成报告 → 结构与引用检查",
    tag: "资料报告模板 · 交付 Markdown", toMaterials: true },
  { goal: "检查当前可用的工具与技能，并说明各自能完成什么任务",
    title: "看看系统能做什么", sub: "读取能力清单 → 汇总可执行范围" },
  { goal: "查看可用的图片生成能力，先告诉我需要你提供哪些输入",
    title: "准备一次视觉生成", sub: "确认参数前不会执行外部生成" },
];

export async function renderWorkspaceHome(ctx) {
  const { el, api, navigate } = ctx;
  let stopped = false;
  const revise = Boolean(ctx.revise);
  const source = revise ? sessionStorage.getItem("agentos_revise_source") || "" : "";
  const parent = revise ? sessionStorage.getItem("agentos_revise_asset") || "" : "";
  el.innerHTML = `
    <section class="home">
      <div class="home-greet">${icon("sparkles", 16)} 你的想法，从这里开始</div>
      <h1>今天，我们完成什么？</h1>
      <p class="home-sub">交给我一个目标。简单的直接做，需要确认的会在执行前问你。</p>
      ${revise ? `<div class="notice">继续修改上一版报告，授权材料会沿用；原成果保留。</div>` : ""}
      <div id="home-composer"></div>
      <div class="context-line">
        <span>${icon("shield", 13)} 重要操作由服务端裁定，执行前请你确认</span>
        <span id="home-cap">${icon("node", 13)} 执行环境读取中…</span>
      </div>
      <div class="example-grid">
        ${EXAMPLES.map((ex, i) => `<button class="example" data-example="${i}">
          ${icon(ex.toMaterials ? "doc" : i === 1 ? "blocks" : "image", 18)}
          <strong>${esc(ex.title)}</strong><span>${esc(ex.sub)}</span>
          <small>${esc(ex.tag || "目标驱动 · 直接执行")}</small></button>`).join("")}
      </div>
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
  el.querySelectorAll("[data-example]").forEach(btn => btn.addEventListener("click", () => {
    const ex = EXAMPLES[Number(btn.dataset.example)];
    composer.setGoal(ex.goal, Boolean(ex.toMaterials));
  }));

  async function loadRuns() {
    if (stopped) return false;
    try {
      const runs = await api.listRuns();
      if (stopped) return false;
      const box = el.querySelector("#home-runs");
      box.innerHTML = runs.length ? runs.slice(0, 4).map((r) => `
        <button class="recent-row" data-run="${esc(r.id)}">
          <span class="rtext"><b>${esc(r.goal || r.id)}</b>
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
