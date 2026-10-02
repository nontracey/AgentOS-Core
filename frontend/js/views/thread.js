/* U5-04 任务工作区（对话优先）：对话 / 执行图 / 成果 = 同一任务的三个视图。
 * 右侧并排成果预览与步骤检查器；手机上按需展开。确认嵌在对话里，不是独立菜单。
 * 原始 JSON 只进「高级：已脱敏事件」，仅管理员外壳可见。 */
"use strict";

import { mountComposer } from "../composer.js";
import { answerMessages, mergeEvents } from "../live.js";
import { render as renderMarkdown } from "../md.js";
import { projectExecution, executionMetrics, duration, inspector } from "../execution.js";
import { projectConversation, renderConversation } from "../conversation.js";
import { closePreviews, openPreview } from "../preview.js";
import { esc } from "../safe-export.js";
import { confirmDialog, humanError, icon, modelZh, stateBadge, timeShort, toast } from "../ui.js";

import { shortGoal } from "../workspace-data.js";

const ACTIVE = new Set(["created", "planning", "running", "awaiting_approval", "cancel_requested"]);

export async function renderThread(ctx) {
  const { el, api, navigate, params } = ctx;
  const id = params[0];
  let stopped = false, tab = "chat", data = null, events = [], cursor = 0, selected = null,
    selectedByUser = false, timer = null, loading = false;
  let composer = null, composerNode = null, previewUrl = null;
  const previewCache = new Map();
  const openedDetails = new Map();

  el.innerHTML = `<div class="panel"><div class="skeleton" style="height:220px"></div></div>`;

  async function load(silent = false) {
    if (loading || stopped) return true;
    loading = true;
    try {
      const d = await api.getTask(id);
      // 游标跨轮询保留：全量分页消费，不做首页静默截断
      let more = true;
      while (more && !stopped) {
        const page = await api.getTrace(id, cursor);
        const batch = page.events || [];
        events = mergeEvents(events, batch);
        cursor = page.cursor || batch.at(-1)?.seq || cursor;
        more = batch.length >= 200;
      }
      if (stopped || !el.isConnected) return false;
      data = d;
      draw();
      return ACTIVE.has(d.run?.state);
    } catch (e) {
      if (!silent) {
        el.innerHTML = `<div class="error-state"><div class="icon">${icon("warnsign", 28)}</div>
          <div class="title">任务读取失败</div><div class="desc">${esc(e.message)}</div>
          <div class="row" style="justify-content:center"><a class="btn" href="#/tasks">返回任务空间</a>
          <button class="btn primary" id="th-retry">重试</button></div></div>`;
        el.querySelector("#th-retry").addEventListener("click", () => load());
      }
      return false;
    } finally {
      loading = false;
    }
  }

  function nodes() { return projectExecution(data, events); }

  function draw() {
    const focus = document.activeElement;
    const selection = focus?.tagName === "TEXTAREA" ? [focus.selectionStart, focus.selectionEnd] : null;
    const pageY = window.scrollY;
    const sideVisible = el.querySelector("#th-side-panel")?.classList.contains("mobile-visible");
    el.querySelectorAll("details[open][data-open-key]")
      .forEach((d) => openedDetails.set(d.dataset.openKey, true));
    el.querySelectorAll("details:not([open])[data-open-key]")
      .forEach((d) => openedDetails.set(d.dataset.openKey, false));

    const run = data.run || {};
    const steps = data.steps || [];
    const artifacts = data.artifacts || [];
    const approvals = data.approvals || [];
    const graph = nodes();
    if (!selectedByUser || !graph.some((n) => n.id === selected)) {
      selected = (graph.find((n) => ["awaiting_approval", "dispatched", "uncertain"].includes(n.state))
        || graph.at(-1))?.id;
    }
    const m = executionMetrics(run, steps, events, data.budget);
    let resumeCode = "";
    try { resumeCode = JSON.parse(run.error_json || "{}").code; } catch { /* advanced error only */ }
    const resumable = run.state === "paused" && ["provider_rate_limited", "runtime_unavailable"].includes(resumeCode)
      && !steps.some((s) => ["uncertain", "dispatched", "pending_external", "awaiting_approval"].includes(s.state));
    const uncertain = steps.find((s) => s.state === "uncertain");
    const pendingDelivery = steps.filter((s) => {
      try { return JSON.parse(s.result_json || "{}").value?.delivery === "pending"; }
      catch { return false; }
    });
    const items = projectConversation(data, events, { guest: false });


    el.innerHTML = `
      <div class="work-header">
        <div class="work-title"><h2>${esc(shortGoal(run.goal_text || run.goal || id))}</h2>
          ${stateBadge(run.state)}</div>
        <details class="task-goal"><summary>查看完整目标</summary><p>${esc(run.goal_text || run.goal || id)}</p></details>
        <div class="work-meta">
          <span>${steps.length ? `${steps.length} 个执行步骤` : "尚无执行步骤"}</span><span>·</span>
          <span>${events.length} 条已记录事件</span><span>·</span>
          <span>创建 ${timeShort(run.created_at)}</span><span>·</span>
          <span>${data.plan ? (data.plan.source === "template" ? "固定模板 v1" : "已保存计划")
            : "未保存结构化计划"}</span>
        </div>
        <div class="tabs work-tabs" role="tablist" aria-label="任务视图">
          ${[["chat", "对话"], ["flow", "执行图"], ["files", `成果${artifacts.length ? ` · ${artifacts.length}` : ""}`]]
            .map(([key, name]) => `<button data-tab="${key}" role="tab"
              aria-selected="${tab === key}" class="${tab === key ? "on" : ""}">${name}</button>`).join("")}
          <span class="tab-spacer"></span>
          <button class="btn small mobile-open" id="th-side-toggle" type="button">
            ${icon("eye", 13)} 成果与步骤详情</button>
        </div>
      </div>

      ${approvals.length && tab !== "chat"
        ? `<div class="notice warn inline-ask">${icon("shield", 14)}
            这一步需要你确认（${approvals.length} 项）——切到「对话」查看具体操作与参数。</div>` : ""}
      ${run.state === "awaiting_input" ? `<div class="notice warn">任务需要补充信息。请查看评审意见，在下方补充要求或材料后继续；已确认的成果会保留。</div>` : ""}
      ${run.error_json || data.error ? `<div class="notice err">${esc(readableError(run.error_json || data.error))}</div>` : ""}
      ${resumable || uncertain || pendingDelivery.length || ACTIVE.has(run.state) || run.state === "succeeded" && artifacts.length ? `
        <div class="work-actions">
          ${resumable ? `<button class="btn small" id="th-resume">${icon("refresh", 14)} 恢复原任务</button>` : ""}
          ${uncertain ? `<button class="btn small" id="th-reconcile">${icon("refresh", 14)} 核对结果</button>` : ""}
          ${pendingDelivery.length ? `<button class="btn small" id="th-deliver">${icon("upload", 14)} 补传已有成果</button>` : ""}
          ${run.state === "succeeded" && artifacts.length ? `<button class="btn small" id="th-revise">${icon("branch", 14)} 基于成果提出修改</button>` : ""}
          ${ACTIVE.has(run.state) ? `<button class="btn small danger" id="th-cancel">${icon("close", 14)} 请求取消</button>` : ""}
          <span class="grow"></span>
          <span class="muted small">任务历时 ${duration(m.duration)} · 已记录 token ${m.tokens == null ? "未记录" : m.tokens.toLocaleString()} · 账本成本 ${esc(m.cost)}</span>
        </div>` : ""}

      <div class="workspace">
        <section class="work-main">
          ${tab === "chat" ? `<div class="conversation" id="th-conv">${data.context ? `<div class="thread-context"><a href="#/tasks/${esc(data.context.run_id)}">${icon("branch", 13)} 查看上一轮</a>
                <p>${esc(data.context.goal)}</p>${data.context.answer ? `<details><summary>上一轮答复</summary><div class="context-answer">${esc(data.context.answer)}</div></details>` : ""}</div>` : ""}${renderConversation(items)}
              <div class="live-answer" id="th-live" hidden><b>实时输出</b><p class="live-text"></p></div>
            </div>
            <div id="thread-composer"></div>`
            : tab === "flow" ? flowView(graph) : filesView(artifacts, events)}
        </section>
        <aside class="work-side" id="th-side-panel">${tab === "flow" ? nodePanel(graph) : docPanel(artifacts, events, m)}</aside>
      </div>

      <details class="tech-details adv" data-open-key="advanced">
        <summary>高级：已脱敏事件与用量明细（管理员）</summary>
        <div class="adv-metrics">${[
          ["有用量记录的模型调用", `${m.calls} 次`], ["步骤重试", `${m.retries} 次`],
          ["已完成工具步骤", `${m.completed} / ${m.steps}`], ["账本成本", m.cost],
        ].map(([k, v]) => `<div><span>${esc(k)}</span><b>${esc(String(v))}</b></div>`).join("")}</div>
        <pre class="step-io">${esc(JSON.stringify(events, null, 2))}</pre>
      </details>`;

    el.querySelectorAll("[data-tab]").forEach((btn) => {
      btn.addEventListener("click", () => { tab = btn.dataset.tab; draw(); });
      btn.addEventListener("keydown", (e) => {
        const keys = ["chat", "flow", "files"];
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
        e.preventDefault();
        const idx = keys.indexOf(tab);
        const next = e.key === "Home" ? keys[0] : e.key === "End" ? keys[2]
          : keys[(idx + (e.key === "ArrowRight" ? 1 : 2)) % 3];
        tab = next; draw(); el.querySelector(`[data-tab="${next}"]`)?.focus();
      });
    });
    el.querySelectorAll("[data-open-key]").forEach((d) => {
      if (openedDetails.has(d.dataset.openKey)) d.open = openedDetails.get(d.dataset.openKey);
    });
    el.querySelectorAll("[data-node]").forEach((btn) => btn.addEventListener("click", () => {
      selected = btn.dataset.node; selectedByUser = true; draw();
      if (window.innerWidth < 950) {
        const side = el.querySelector("#th-side-panel");
        side?.classList.add("mobile-visible");
        side?.scrollIntoView({ block: "start", behavior: "smooth" });
      }
    }));
    el.querySelectorAll("[data-tool]").forEach((btn) => btn.addEventListener("click", () => {
      selected = btn.dataset.tool; selectedByUser = true; tab = "flow"; draw();
    }));
    el.querySelectorAll("[data-source]").forEach((btn) => btn.addEventListener("click", async () => {
      try {
        const ref = await api.resolveTaskReference(id, btn.dataset.source, btn.dataset.locator);
        if (!stopped) openSourceDialog(ref.name || ref.source_id, ref.locator, ref.quote || ref.text);
      } catch (err) { toast(err.message, "err"); }
    }));
    el.querySelectorAll("[data-preview-artifact]").forEach((btn) => btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const a = artifacts.find((x) => x.id === btn.dataset.previewArtifact);
      try { await openPreview(api, btn.dataset.previewArtifact, { title: a?.name || a?.id, mime: a?.mime }); }
      catch (err) { toast(err.isForbidden ? "权限不足，无法预览" : err.message, "err"); }
    }));
    el.querySelectorAll("[data-open-artifact]").forEach((btn) => btn.addEventListener("click", async () => {
      const a = artifacts.find((x) => x.id === btn.dataset.openArtifact);
      try { await openPreview(api, btn.dataset.openArtifact, { title: a?.name || a?.id, mime: a?.mime }); }
      catch (err) { toast(err.isForbidden ? "权限不足，无法预览" : err.message, "err"); }
    }));
    el.querySelector("#side-close")?.addEventListener("click", () => {
      el.querySelector("#th-side-panel")?.classList.remove("mobile-visible");
    });
    el.querySelector("#th-side-toggle")?.addEventListener("click", () => {
      const side = el.querySelector("#th-side-panel");
      side?.classList.toggle("mobile-visible");
      side?.scrollIntoView({ block: "start", behavior: "smooth" });
    });
    if (tab === "chat") {
      if (!composerNode) {
        composerNode = document.createElement("div"); composerNode.id = "thread-composer";
        composer = mountComposer(composerNode, {
          api, scope: `thread_${id}`, contextRun: id, parentAsset: "",
          supplement: () => data?.run?.state === "awaiting_input",
          isBlocked: () => ACTIVE.has(data?.run?.state),
          onAccepted: r => navigate(`#/tasks/${r.run_id}`),
        });
      }
      el.querySelector("#thread-composer").replaceWith(composerNode);
      const submit = composerNode.querySelector("[type=submit]");
      submit.disabled = ACTIVE.has(run.state);
      submit.title = ACTIVE.has(run.state) ? "等当前执行结束后提交；可以先写好修改要求" : run.state === "awaiting_input" ? "提交资料并继续原任务" : "提交修改";
    }
    if (sideVisible) el.querySelector("#th-side-panel")?.classList.add("mobile-visible");
    if (focus?.isConnected) { focus.focus({preventScroll:true}); if (selection) focus.setSelectionRange(...selection); }
    window.scrollTo({ top: pageY, behavior: "instant" });
    paintLive();
    paintPreview(artifacts[0]);
    bindActions(run, uncertain, pendingDelivery, artifacts);
    ctx.bindCommon();
  }

  function bindActions(run, uncertain, pendingDelivery, artifacts) {
    el.querySelector("#th-resume")?.addEventListener("click", async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try { await api.resumeTask(id); toast("已请求恢复原任务"); await load(); }
      catch (e) { toast(e.message, "err"); button.disabled = false; }
    });
    el.querySelector("#th-cancel")?.addEventListener("click", async () => {
      if (!await confirmDialog({ title: "请求取消任务", danger: true,
        body: "服务端确认停止前，已派发的步骤可能仍在执行。", confirmText: "请求取消" })) return;
      try { await api.cancelTask(id); toast("已请求取消"); await load(); }
      catch (e) { toast(e.message, "err"); }
    });
    el.querySelector("#th-revise")?.addEventListener("click", () => {
      sessionStorage.setItem("agentos_revise_source", id);
      sessionStorage.setItem("agentos_revise_asset", artifacts[0]?.id || "");
      location.hash = "#/tasks/new?revise=1";
    });
    el.querySelector("#th-reconcile")?.addEventListener("click", async () => {
      try { await api.reconcileTask(id, uncertain.id); toast("已发起核对"); await load(); }
      catch (e) { toast(e.message, "err"); }
    });
    el.querySelector("#th-deliver")?.addEventListener("click", async () => {
      try {
        for (const s of pendingDelivery) {
          let value;
          try { value = JSON.parse(s.result_json || "{}").value; } catch { continue; }
          const route = events.findLast((e) => e.kind === "node.dispatched" && e.payload?.step_id === s.id);
          if (!value?.asset_ref || !route) continue;
          const mime = /\.mp4$/i.test(value.asset_ref) ? "video/mp4"
            : /\.png$/i.test(value.asset_ref) ? "image/png" : "application/octet-stream";
          await api.fetchNodeAsset(route.payload.node_id, {
            remote_ref: value.asset_ref, sha256: value.sha256, mime, run_id: id });
        }
        toast("成果已校验并登记");
        await load();
      } catch (e) { toast(e.message, "err"); }
    });
  }

  function paintLive() {
    const box = el.querySelector("#th-live");
    if (!box) return;
    const messages = answerMessages(events);
    const latest = messages.at(-1);
    const hasFinal = data?.messages?.length || data?.artifacts?.length;
    box.hidden = !latest || Boolean(hasFinal && data?.run?.state === "succeeded");
    if (latest) { box.querySelector("b").textContent = latest.ended ? "已生成正文" : "实时输出";
      box.querySelector(".live-text").textContent = latest.text; }
  }

  async function paintPreview(asset) {
    const holder = el.querySelector("#inline-preview");
    if (!holder || !asset) return;
    try {
      if (!previewCache.has(asset.id)) {
        const blob = await api.downloadAsset(asset.id);
        if (stopped) return;
        if (/text|markdown/.test(asset.mime || "")) previewCache.set(asset.id, renderMarkdown(await blob.text()));
        else if (/^(image|video)\//.test(asset.mime || "")) {
          if (previewUrl) URL.revokeObjectURL(previewUrl);
          previewUrl = URL.createObjectURL(blob);
          previewCache.set(asset.id, asset.mime.startsWith("image/") ? `<img src="${previewUrl}" alt="${esc(asset.name)}">` : `<video src="${previewUrl}" controls></video>`);
        } else previewCache.set(asset.id, "该文件请下载查看。");
      }
      const current = el.querySelector("#inline-preview");
      if (!stopped && current?.dataset.asset === asset.id) current.innerHTML = previewCache.get(asset.id);
    } catch (err) { if (!stopped && holder.isConnected) holder.textContent = err.message; }
  }

  function flowView(graph) {
    const simple = graph.length <= 4;
    const taskTitle = id => data.plan?.tasks?.find(t => t.task_id === id)?.title || id;
    const routed = graph.some((n) => {
      const field = (n.fields || []).find(([k]) => k === "实际路由");
      return field && !String(field[1]).startsWith("未记录");
    });
    return `<div class="flow-page">
      <div class="flow-toolbar">
        <span>${data.plan ? (data.plan.source === "template" ? "固定模板 · 步骤顺序执行"
          : `已保存计划 · ${graph.length} 个节点`) : "按真实事件展示执行轨迹（未保存结构化计划）"}</span>
        <button class="btn small mobile-open" data-show-side="1">步骤详情 ${icon("arrow", 13)}</button>
      </div>
      ${data.plan?.source !== "template" && data.plan?.tasks?.length ? `<div class="plan-dag" aria-label="计划依赖">
        <b>计划 v${esc(data.plan.version || "1")} · 依赖关系</b>
        ${data.plan.tasks.map(t => `<p><span>${esc((t.depends_on || []).map(taskTitle).join(" + ") || "目标 / 授权输入")}</span><span aria-hidden="true"> → </span><button class="link-btn" data-node="task:${esc(t.task_id)}">${esc(t.title || t.task_id)}</button></p>`).join("")}
        ${events.filter(e => e.kind === "plan.rework_accepted").map(e => `<p class="rework-edge">返修 ${esc(String(e.payload.round))}：${esc(e.payload.reason)} ⇢ ${esc(e.payload.target)} · 复用 ${(e.payload.reused || []).map(esc).join("、")}</p>`).join("")}
      </div>` : ""}
      <div class="graph">
        <div class="graph-grid">
          ${graph.map((n) => `<button class="graph-node ${n.id === selected ? "selected" : ""
            } ${simple ? "center" : ""}" data-node="${esc(n.id)}">
            <span class="gn-glyph">${icon(n.type === "node" ? "cpu" : n.type === "asset" ? "files"
              : n.type === "check" ? "check" : "flow", 15)}</span>
            <span class="gn-body"><b>${esc(n.title)}</b>
              <small>${esc(nodeSubtitle(n))}</small>
              ${stateBadge(n.state)}</span></button>`).join("")}
        </div>
      </div>
      <div class="flow-legend"><span>节点顺序来自已保存计划或实际事件</span>
        <span>角色与设备分别标注</span><span>重试次数在步骤检查器中显示</span></div>
      <p class="side-caption">执行图只读；${routed
        ? "有事件证明的节点派发已标注" : "本任务未记录跨节点派发"}。不代表可编辑工作流。</p>
    </div>`;
  }

  function filesView(artifacts, events) {
    const finalText = events.findLast((e) => e.payload?.final)?.payload?.final || "";
    return `<div class="files-page">
      <div class="subhead"><h3>本次成果</h3><span class="muted small">不可变版本 · 旧版继续保留</span></div>
      ${artifacts.length ? artifacts.map((a) => `
        <button class="inline-artifact" data-open-artifact="${esc(a.id)}">
          <span class="file-symbol">${icon(/image|video/.test(a.mime || "") ? "image" : "doc", 18)}</span>
          <span class="artifact-body"><b>${esc(a.name || a.id)}${a.version ? ` · v${a.version}` : ""}</b>
            <small>${esc(a.mime || "—")} · ${Number(a.size || 0).toLocaleString()} 字节${
              a.sha256 ? ` · sha256 ${esc(String(a.sha256).slice(0, 12))}…` : ""}</small></span>
          ${icon("arrow", 16)}</button>
        <div class="artifact-actions">
          <button class="btn small" data-preview-artifact="${esc(a.id)}">${icon("eye", 14)} 预览</button>
          <button class="btn small" data-artifact-download="${esc(a.id)}" data-name="${esc(a.name || a.id)}">
            ${icon("download", 14)} 下载</button>
          ${a.parent_asset_id ? `<span class="muted small">新版本，源自 ${esc(String(a.parent_asset_id))}</span>` : ""}
        </div>`).join("")
        : `<div class="empty-state"><div class="icon">${icon("files", 26)}</div>
            <div class="title">尚未登记成果</div>
            <div class="desc">文件成果通过服务端检查后显示在这里；未登记时不显示下载。</div></div>`}
      ${finalText ? `<div class="msg-body final-answer-text"><b>任务答复</b><span>${esc(finalText)}</span></div>` : ""}
    </div>`;
  }

  function nodePanel(graph) {
    const node = graph.find((n) => n.id === selected) || graph.at(-1);
    if (!node) return `<div class="side-empty">没有可展示的步骤记录。</div>`;
    return `<div class="side-head"><b>步骤详情</b>${stateBadge(node.state)}
        <button class="icon-btn side-close" id="side-close" aria-label="关闭详情">${icon("close", 15)}</button></div>
      <div class="node-details">${inspector(node)}</div>`;
  }

  function docPanel(artifacts, events, m) {
    const first = artifacts.find(a => a.mime === "text/markdown") || artifacts[0];
    const finalText = events.findLast((e) => e.payload?.final)?.payload?.final || "";
    const usageEvents = events.filter((e) => e.kind === "model.usage");
    return `<div class="side-head"><b>${first ? "成果预览" : "任务上下文"}</b>
      ${first ? `<button class="icon-btn" data-open-artifact="${esc(first.id)}"
        aria-label="放大预览">${icon("arrow", 15)}</button>` : ""}
      <button class="icon-btn side-close" id="side-close" aria-label="关闭面板">${icon("close", 15)}</button></div>
      ${first ? `<div class="doc-card">
        <div class="doc-eyebrow">AGENTOS / 交付成果</div>
        <h3>${esc(first.name || first.id)}</h3>
        <p class="muted small">${first.version ? `版本 v${first.version} · ` : ""}${esc(first.mime || "—")}</p>
        <div class="doc-divider"></div>
        <div class="inline-preview md-view" id="inline-preview" data-asset="${esc(first.id)}">读取成果正文…</div>
        ${[...new Map(events.filter((e) => e.kind.startsWith("report.") && e.kind.endsWith("_check")).map(e => [e.kind,e])).values()]
          .map((e) => `<div class="note-row ${e.payload?.passed ? "ok" : "err"}">
            ${icon(e.payload?.passed ? "check" : "warnsign", 13)}
            <span>${({"report.structure_check":"结构检查", "report.citation_check":"引用检查", "report.fact_check":"日期与金额检查", "report.length_check":"字数检查"})[e.kind] || "报告检查"}${
              e.payload?.passed ? "通过" : "未通过"}</span></div>`).join("") || ""}
        <div class="doc-actions">
          <button class="btn small" data-preview-artifact="${esc(first.id)}">${icon("eye", 14)} 预览</button>
          <button class="btn small" data-artifact-download="${esc(first.id)}"
            data-name="${esc(first.name || first.id)}">${icon("download", 14)} 下载</button>
        </div></div>`
        : `<div class="side-empty">${ACTIVE.has(data.run?.state)
          ? "任务尚未产出可预览的成果；进展在对话与执行图中持续记录。"
          : "本次运行没有登记文件成果（结果以事件与答复为准）。"}</div>`}
      <div class="side-bound">
        <h4>本次绑定</h4>
        ${[
          ["知识后端", knowledgeLabel(events)],
          ["执行设备", deviceLabel(events)],
          ["模型 / 用量", usageEvents.length
            ? `${esc(modelZh(usageEvents.at(-1).payload?.model))} · ${m.tokens ?? "未记录"} tokens`
            : "未记录"],
          ["账本成本", m.cost],
        ].map(([k, v]) => `<div class="bound-row"><span>${esc(k)}</span><b>${v}</b></div>`).join("")}
        <p class="side-caption">未记录项保持「未记录」，不根据目标补造。</p>
      </div>`;
  }

  function knowledgeLabel(events) {
    const used = events.filter((e) => e.kind === "tool.prepared"
      && /knowledge|search/i.test(String(e.payload?.tool || "")));
    return used.length ? "已使用授权知识检索" : "本任务未调用知识检索";
  }

  /** 节点副标题：只说类型能证明的事，不给模型/检查节点编造“设备”。 */
  function nodeSubtitle(node) {
    if (node.type === "model") return modelZh(node.subtitle);
    if (node.type === "check") return node.subtitle || "服务端确定性检查";
    if (node.type === "asset") return node.subtitle || "不可变成果";
    if (node.type === "plan") return node.subtitle || "工作流节点";
    if (node.type === "input") return node.subtitle || "自然语言目标";
    return `${node.subtitle || "角色未记录"} · ${node.type === "node" ? "已配对执行设备" : "本地受控工具"}`;
  }

  function deviceLabel(events) {
    const routes = events.filter((e) => e.kind === "node.dispatched");
    if (!routes.length) return "本地执行";
    const names = [...new Set(routes.map((e) => e.payload?.os || "远程"))];
    return esc(names.join(" + "));
  }

  function readableError(raw) { return humanError(raw); }

  function openSourceDialog(source, locator, quote) {
    const mask = document.createElement("div");
    mask.className = "dialog-mask";
    mask.innerHTML = `<div class="dialog" role="dialog" aria-modal="true" aria-label="来源片段">
      <div class="side-head"><b>来源片段</b><button class="icon-btn" data-close
        aria-label="关闭">${icon("close", 15)}</button></div>
      <p class="muted small">${esc(source)}${locator ? ` · ${esc(locator)}` : ""}</p>
      ${quote ? `<div class="source-quote">${esc(quote)}</div>`
        : `<p class="muted small">该引用只记录了来源与定位符，片段正文未持久化；
           可回查范围由服务端按来源授权裁定。</p>`}
    </div>`;
    mask.addEventListener("click", (e) => {
      if (e.target === mask || e.target.closest("[data-close]")) mask.remove();
    });
    document.body.appendChild(mask);
  }

  /* ---------- U5-04 流式：正文增量到达即显示，结束后由持久事件补齐 ---------- */
  const stopStream = api.streamTask(id, {
    onEvent: event => {
      events = mergeEvents(events, [event]);
      paintLive();
      if (["message.completed", "run.state_changed", "approval.requested", "asset.registered"].includes(event.kind)) load(true);
    },
    onError: () => { /* trace polling recovers persisted events */ },
  });

  const needPoll = await load();
  return {
    poll: needPoll ? load : null,
    destroy: () => {
      stopped = true;
      stopStream?.();
      closePreviews();
      composer?.destroy();
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      previewCache.clear();
      events = [];
    },
  };
}
