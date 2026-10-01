/* U5-06 游客只读投影：与管理员同一工作区视觉，数据只走 /v1/demo/*。
 * 没有目标输入、没有写控件；事件按可读标签渲染，不显示原始 JSON。
 * 隐藏入口不等于授权——服务端仍逐资源、逐动作、逐版本核权。 */
"use strict";

import { projectConversation, renderConversation, toolLabel } from "../conversation.js";
import { closePreviews, openPreview } from "../preview.js";
import { esc } from "../safe-export.js";
import { emptyState, icon, stateBadge, timeShort, toast } from "../ui.js";

function endsAt(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString("zh-CN", { hour12: false });
}

export function renderGuestExpired(ctx, reason) {
  closePreviews();
  sessionStorage.setItem("agentos_guest_expired", reason || "演示访问已结束。");
  ctx.el.innerHTML = `
    <div class="error-state">
      <div class="icon">${icon("shield", 30)}</div>
      <div class="title">访问已结束</div>
      <div class="desc">${esc(reason || "演示访问已过期或被管理员撤销。")}已展示的内容已从本页移除。</div>
      <div class="row" style="justify-content:center">
        <a class="btn primary" href="#/enter">返回进入页</a>
        <button class="btn" id="ge-retry">重试</button></div>
    </div>`;
  document.getElementById("ge-retry")?.addEventListener("click", () => {
    sessionStorage.removeItem("agentos_guest_expired");
    location.hash = "#/guest";
  });
  return null;
}

export async function renderGuestHome(ctx) {
  const { el, api, navigate } = ctx;
  let home;
  try {
    home = await api.demoHome();
  } catch (e) {
    if (e.status === 401 || e.status === 403 || e.status === 404) {
      return renderGuestExpired(ctx, "演示访问已结束或被管理员撤销。");
    }
    throw e;
  }
  el.innerHTML = `
    <section class="home guest-home">
      <div class="home-greet">${icon("eye", 16)} 演示空间 · 只读访问</div>
      <h1>管理员发布的内容，都在这里。</h1>
      <p class="home-sub">${esc(home.note || "任务链路经过脱敏投影；不能创建、修改、审批任务，也无法读取未发布资源。")}</p>
      <div class="guest-bounds">
        <div><span class="muted small">可查看</span><b>已发布任务的计划、步骤与交接、审批裁定、成果预览与已开放下载的版本</b></div>
        <div><span class="muted small">不可做</span><b>创建 / 修改 / 取消任务、审批、上传材料、检索整库、管理工具与设备</b></div>
        <div><span class="muted small">本次访问</span><b>revision ${esc(String(home.manifest_revision ?? 0))}
          ${home.expires_at ? ` · 有效至 ${esc(endsAt(home.expires_at))}` : " · 未设置到期"}</b></div>
      </div>
      <div class="home-recent">
        <div class="subhead"><h3>已发布任务</h3>
          <a href="#/guest/artifacts">演示成果 ${icon("arrow", 13)}</a></div>
        <div id="gg-runs">${home.runs.length ? home.runs.map((r) => `
          <button class="recent-row" data-goto="${esc(r.id)}">
            <span class="rtext"><b>${esc(r.goal || r.id)}</b>
              <small>${esc(r.channel || "提交入口未记录")} · ${timeShort(r.created_at)}</small></span>
            ${stateBadge(r.state)}</button>`).join("")
          : emptyState(icon("folder", 24), "管理员尚未发布演示任务",
              "发布后这里会出现任务卡片；当前清单 revision 为空。")}</div>
      </div>
    </section>`;
  el.querySelectorAll("[data-goto]").forEach((btn) => btn.addEventListener("click",
    () => navigate(`#/guest/tasks/${btn.dataset.goto}`)));
  return null;
}

export async function renderGuestArtifacts(ctx) {
  const { el, api, navigate } = ctx;
  let stopped = false;

  async function load() {
    if (stopped) return false;
    let home;
    try {
      home = await api.demoHome();
    } catch (e) {
      if (e.status === 401 || e.status === 403 || e.status === 404) {
        renderGuestExpired(ctx, "演示访问已结束或被管理员撤销。");
        return false;
      }
      throw e;
    }
    const details = await Promise.all((home.runs || []).map((r) =>
      api.demoRun(r.id).catch(() => null)));
    if (stopped) return false;
    const items = [];
    details.filter(Boolean).forEach((d) => {
      for (const a of d.artifacts || []) {
        items.push({ ...a, runId: d.run.id, goal: d.run.goal || d.run.id,
          download: Boolean(a.download), previewAllowed: Boolean(a.preview) });
      }
    });
    el.innerHTML = `
      <section class="page">
        <div class="page-heading"><div><h2>演示成果</h2>
          <p>只列出管理员为本次演示明确发布的版本。</p></div>
          <a class="btn" href="#/guest">返回演示任务</a></div>
        <div class="lib-grid">${items.length ? items.map((a) => `
          <article class="lib-card">
            <div class="lib-preview"><div class="lib-doc">
              <span class="doc-eyebrow">${esc(String(a.mime || "").includes("image") ? "IMAGE"
                : String(a.mime || "").includes("video") ? "VIDEO" : "DELIVERABLE")}</span>
              <h3>${esc(a.name || a.id)}</h3>
              <p>${a.version ? `v${esc(String(a.version))} · ` : ""}${esc(a.mime || "—")} ·
                ${Number(a.size || 0).toLocaleString()} 字节</p>
              <div class="doc-divider"></div>
              <p class="muted small">来源任务：${esc(a.goal)}</p>
            </div></div>
            <h3 class="lib-title">${esc(a.name || a.id)}</h3>
            <p class="lib-meta">${a.sha256 ? `sha256 ${esc(String(a.sha256).slice(0, 12))}…`
              : "校验值未记录"}</p>
            <div class="lib-actions">
              ${a.previewAllowed ? `<button class="btn small" data-gp="${esc(a.id)}"
                data-name="${esc(a.name || a.id)}" data-mime="${esc(a.mime || "")}">
                ${icon("eye", 14)} 预览</button>` : `<span class="muted small">预览未开放</span>`}
              ${a.download ? `<button class="btn small" data-artifact-download="${esc(a.id)}"
                data-name="${esc(a.name || a.id)}">${icon("download", 14)} 下载</button>`
                : `<span class="muted small">下载未开放</span>`}
              <a class="btn small" href="#/guest/tasks/${esc(a.runId)}">打开任务</a>
            </div>
          </article>`).join("")
          : emptyState(icon("files", 26), "本次演示没有发布的成果",
              "管理员可以逐版本开放预览与下载；未发布版本不在此列出。")}</div>
        <p class="side-caption">能预览不等于能下载：两者是独立授权，由服务端逐项核对。</p>
      </section>`;
    el.querySelectorAll("[data-gp]").forEach((btn) => btn.addEventListener("click", async () => {
      try {
        await openPreview(api, btn.dataset.gp, {
          guest: true, title: btn.dataset.name, mime: btn.dataset.mime });
      } catch (e) { toast(e.isForbidden ? "该资源未开放预览" : e.message, "err"); }
    }));
    ctx.bindCommon();
    return true;
  }

  await load();
  return { poll: load, destroy: () => { stopped = true; closePreviews(); } };
}

export async function renderGuestTask(ctx) {
  const { el, api, navigate, params } = ctx;
  const runId = params[0];
  let stopped = false, detail = null, events = [], cursor = 0, tab = "chat", loading = false;
  const seenSeq = new Set();

  el.innerHTML = `<div class="panel"><div class="skeleton" style="height:200px"></div></div>`;

  async function load(silent = false) {
    if (loading || stopped) return true;
    loading = true;
    try {
      detail = await api.demoRun(runId);
      let more = true;
      while (more && !stopped) {
        const page = await api.demoEvents(runId, cursor, 500);
        const batch = page.events || [];
        batch.forEach((e) => seenSeq.add(e.seq));
        events.push(...batch);
        cursor = page.cursor || cursor;
        more = batch.length > 0 && Number(page.cursor) > Number(page.events?.at(-1)?.seq ?? page.cursor);
      }
      if (stopped || !el.isConnected) return false;
      draw();
      return true;
    } catch (e) {
      if (e.status === 404 || e.status === 401 || e.status === 403) {
        renderGuestExpired(ctx, e.status === 404
          ? "该任务不在本次演示范围内，或访问已结束。" : "访问已结束，无法继续读取。");
        return false;
      }
      if (!silent) throw e;
      return true;
    } finally {
      loading = false;
    }
  }

  function draw() {
    const run = detail.run || {};
    const artifacts = detail.artifacts || [];
    const items = projectConversation(detail, events, { guest: true });
    el.innerHTML = `
      <div class="work-header">
        <div class="work-title"><h2>${esc(run.goal || runId)}</h2>${stateBadge(run.state)}</div>
        <div class="work-meta"><span>只读投影</span><span>·</span>
          <span>revision ${esc(String(detail.manifest_revision ?? 0))}</span><span>·</span>
          <span>${(detail.steps || []).length} 个已发布步骤</span><span>·</span>
          <a href="#/guest">返回演示空间</a></div>
        <button class="btn small mobile-open" id="gg-side" type="button"
          >${icon("eye", 13)} 成果与发布范围</button>
        <div class="tabs work-tabs" role="tablist" aria-label="任务视图">
          ${[["chat", "对话"], ["flow", "执行图"], ["files", `成果${artifacts.length ? ` · ${artifacts.length}` : ""}`]]
            .map(([key, name]) => `<button data-tab="${key}" role="tab"
              aria-selected="${tab === key}" class="${tab === key ? "on" : ""}">${name}</button>`).join("")}
        </div>
      </div>
      <div class="workspace">
        <section class="work-main">
          ${tab === "chat" ? `<div class="conversation">${renderConversation(items)}
              <div class="live-answer" id="gg-live" hidden><b>实时输出</b><p class="live-text"></p></div>
            </div>
            <div class="notice readonly">${icon("shield", 14)}
              演示空间为只读：可以查看链路、引用与已发布成果，不能补充要求或触发执行。</div>`
            : tab === "flow" ? flowView(detail) : filesView(artifacts)}
        </section>
        <aside class="work-side" id="gg-side-panel">${sidePanel(detail, artifacts)}</aside>
      </div>`;

    el.querySelectorAll("[data-tab]").forEach((btn) => btn.addEventListener("click", () => {
      tab = btn.dataset.tab; draw();
    }));
    el.querySelector("#gg-side")?.addEventListener("click", () => {
      const side = el.querySelector("#gg-side-panel");
      side?.classList.toggle("mobile-visible");
      side?.scrollIntoView({ block: "start", behavior: "smooth" });
    });
    el.querySelectorAll("[data-open-artifact],[data-preview-artifact]").forEach((btn) => {
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        const id = btn.dataset.openArtifact || btn.dataset.previewArtifact;
        const a = artifacts.find((x) => x.id === id);
        if (btn.dataset.previewArtifact === id && !a?.preview) {
          toast("该版本未开放预览", "err");
          return;
        }
        try {
          await openPreview(api, id, { guest: true, title: a?.name || id, mime: a?.mime });
        } catch (e2) {
          toast(e2.isForbidden ? "该资源未开放预览或下载" : e2.message, "err");
        }
      });
    });
    el.querySelectorAll("[data-source]").forEach((btn) => btn.addEventListener("click", () => {
      const mask = document.createElement("div");
      mask.className = "dialog-mask";
      mask.innerHTML = `<div class="dialog" role="dialog" aria-modal="true" aria-label="来源片段">
        <div class="side-head"><b>已发布来源片段</b><button class="icon-btn" data-close
          aria-label="关闭">${icon("close", 15)}</button></div>
        <p class="muted small">${esc(btn.dataset.source)}${
          btn.dataset.locator ? ` · ${esc(btn.dataset.locator)}` : ""}</p>
        <div class="source-quote">${esc(btn.dataset.quote || "该片段未提供展示文本。")}</div>
        <p class="muted small">引用只跳转到已发布片段，不能跳到整库或原文。</p></div>`;
      mask.addEventListener("click", (ev) => {
        if (ev.target === mask || ev.target.closest("[data-close]")) mask.remove();
      });
      document.body.appendChild(mask);
    }));
    ctx.bindCommon();
  }

  function flowView(d) {
    const steps = d.steps || [];
    const plan = d.plan;
    return `<div class="flow-page">
      <div class="flow-toolbar"><span>${plan ? `已发布计划 · ${(plan.tasks || []).length} 个步骤`
        : "本任务未发布结构化计划；以下为已发布的步骤记录"}</span></div>
      ${plan && (plan.tasks || []).length ? `<div class="plan-summary"><b>已发布计划</b>
        ${plan.tasks.map((t) => `<span>${esc(t.title || t.task_id)}</span>`).join("")}</div>` : ""}
      <div class="graph"><div class="graph-grid">
        ${steps.length ? steps.map((s) => `<div class="graph-node static center">
          <span class="gn-glyph">${icon("cpu", 15)}</span>
          <span class="gn-body"><b>${esc(toolLabel(s.tool_name || s.id))}</b>
            <small>执行设备：${esc(s.node_id || "本地/受控工具")} · ${
              Number(s.attempt || 1) > 1 ? `第 ${esc(String(s.attempt))} 次尝试` : "首次执行"}</small>
            ${stateBadge(s.state)}</span></div>`).join("")
          : `<div class="side-empty">本次演示未发布步骤记录。</div>`}
      </div></div>
      <div class="flow-legend"><span>步骤与状态来自管理员发布的白名单投影</span>
        <span>提示词、隐私参数与内部路径不在范围内</span></div>
    </div>`;
  }

  function filesView(artifacts) {
    return `<div class="files-page">
      <div class="subhead"><h3>本次成果</h3><span class="muted small">仅已发布版本</span></div>
      ${artifacts.length ? artifacts.map((a) => `
        <button class="inline-artifact" data-open-artifact="${esc(a.id)}">
          <span class="file-symbol">${icon(/image|video/.test(a.mime || "") ? "image" : "doc", 18)}</span>
          <span class="artifact-body"><b>${esc(a.name || a.id)}${
            a.version ? ` · v${esc(String(a.version))}` : ""}</b>
            <small>${esc(a.mime || "—")} · ${Number(a.size || 0).toLocaleString()} 字节${
              a.sha256 ? ` · sha256 ${esc(String(a.sha256).slice(0, 12))}…` : ""}</small></span>
          ${icon("arrow", 16)}</button>
        <div class="artifact-actions">
          <button class="btn small" data-preview-artifact="${esc(a.id)}">${icon("eye", 14)} 预览</button>
          ${a.download ? `<button class="btn small" data-artifact-download="${esc(a.id)}"
            data-name="${esc(a.name || a.id)}">${icon("download", 14)} 下载</button>`
            : `<span class="muted small">下载未开放</span>`}
        </div>`).join("")
        : `<div class="side-empty">本次演示未发布成果。</div>`}
    </div>`;
  }

  function sidePanel(d, artifacts) {
    const first = artifacts.find((a) => a.preview) || artifacts[0];
    return `<div class="side-head"><b>${first ? "成果预览" : "任务上下文"}</b></div>
      ${first ? `<div class="doc-card"><div class="doc-eyebrow">AGENTOS / 演示成果</div>
        <h3>${esc(first.name || first.id)}</h3>
        <p class="muted small">${first.version ? `v${esc(String(first.version))} · ` : ""}${
          esc(first.mime || "—")}</p>
        <div class="doc-divider"></div>
        <p class="muted small">正文按发布授权显示；点击预览读取的是已发布版本字节。</p>
        <div class="doc-actions"><button class="btn small" data-preview-artifact="${esc(first.id)}">
          ${icon("eye", 14)} 预览</button>
          ${first.download ? `<button class="btn small" data-artifact-download="${esc(first.id)}"
            data-name="${esc(first.name || first.id)}">${icon("download", 14)} 下载</button>` : ""}
        </div></div>` : ""}
      <div class="side-bound"><h4>本次发布范围</h4>
        ${[["计划", d.plan ? "已发布" : "未发布"],
          ["步骤与交接", (d.steps || []).length ? `${d.steps.length} 条` : "未发布"],
          ["审批裁定", (d.approvals || []).length ? `${d.approvals.length} 条` : "无记录"],
          ["引用片段", (d.refs || []).length ? `${d.refs.length} 段` : "未发布"],
          ["成果版本", artifacts.length ? `${artifacts.length} 个` : "未发布"]]
          .map(([k, v]) => `<div class="bound-row"><span>${esc(k)}</span><b>${esc(String(v))}</b></div>`).join("")}
        <p class="side-caption">未列出的内容不在本次授权范围内。</p>
      </div>`;
  }

  /* 已发布任务的正文增量只在管理员显式开启直播时到达（服务端裁定） */
  let streamCursor = cursor;
  const stopStream = api.streamDemoTask(runId, {
    cursor: streamCursor,
    onEvent: (event) => {
      if (typeof event.seq === "number") streamCursor = Math.max(streamCursor, event.seq);
      if (event.kind === "access.revoked") {
        renderGuestExpired(ctx, "访问已结束，实时输出已停止。");
        return;
      }
      const box = el.querySelector("#gg-live");
      if (event.kind === "answer.delta" && event.payload?.delta && box) {
        box.hidden = false;
        box.querySelector(".live-text").textContent += event.payload.delta;
      }
    },
    onError: () => { /* 轮询兜底 */ },
  });

  await load();
  if (stopped) return { destroy: () => { stopStream?.(); closePreviews(); } };
  return {
    poll: () => load(true),
    destroy: () => { stopped = true; stopStream?.(); closePreviews(); },
  };
}

export function renderGuestAbout(ctx) {
  ctx.el.innerHTML = `
    <section class="page">
      <div class="page-heading"><div><h2>访问说明</h2>
        <p>本空间为只读演示，范围与下载权限由管理员逐资源发布。</p></div></div>
      <div class="panel-x">
        <div class="bound-list">
          <div><span class="muted small">可查看</span>
            <b>已发布任务的计划、步骤与交接、审批裁定历史、来源片段、成果预览与已开放下载的版本</b></div>
          <div><span class="muted small">不可做</span>
            <b>创建 / 修改 / 取消 / 重试 / 对账任务、审批、上传材料、管理工具与设备、导出私有备份</b></div>
          <div><span class="muted small">引用</span>
            <b>只能读取显式发布的片段快照，不能由引用跳到整库或原文</b></div>
          <div><span class="muted small">失效</span>
            <b>到期、被撤销或管理员暂停演示后，后续读取、刷新、预览与下载立即失败，本页内容随之移除</b></div>
          <div><span class="muted small">证据边界</span>
            <b>展示的执行链路来自服务端真实记录；未记录项标注「未记录」，不做补造</b></div>
        </div>
      </div>
    </section>`;
  return null;
}
