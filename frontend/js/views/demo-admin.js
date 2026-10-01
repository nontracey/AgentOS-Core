/* U5-06 演示访问：管理员维护"默认演示清单"（免登游客可见的那一份），
 * 逐任务/逐版本/逐动作勾选发布范围，发布不可变 revision，并可随时暂停对外演示。
 * 定向邀请码保留为可选能力（给指定人单独期限或单独清单），不再是游客入口的必经步骤。 */
"use strict";

import { esc } from "../safe-export.js";
import {
  accessBadge, confirmDialog, emptyState, icon, stateBadge, timeShort, toast,
} from "../ui.js";

const PUBLIC_GRANT = "grant_public";

export async function renderDemoAccess(ctx) {
  const { el, api } = ctx;
  let stopped = false;
  let runs = [];
  let assetsByRun = {};
  let selected = new Map();      // run_id -> {include:[...], live:false}
  let assetRules = new Map();    // asset_id|version -> {preview,download}
  let refCandidates = new Map(); // run_id -> [{source_id, chunk_id, quote}]
  let checked = new Set();       // 已勾选的引用片段 key
  let publishedIds = new Set();  // 当前 revision 已发布的任务

  el.innerHTML = `
    <section class="page">
      <div class="page-heading">
        <div><h2>演示访问</h2>
          <p>游客是默认只读角色：免登进入演示空间，只能看到你在这里明确发布的内容。</p></div>
        <div class="row" id="da-channel"><span class="tag">读取中…</span></div>
      </div>
      <div id="da-notice" class="notice" hidden></div>

      <div class="da-grid">
        <section class="da-main panel-x">
          <div class="subhead"><h3>1 · 选择要演示的任务</h3>
            <button class="btn small" id="da-refresh-runs">${icon("refresh", 13)} 重新读取</button></div>
      <div id="da-runs" class="rows"><div class="skeleton" style="height:60px"></div></div>

          <div class="subhead gap"><h3>2 · 逐资源确认开放范围</h3>
            <span class="muted small">默认只开放脱敏任务链路；成果默认仅预览，下载要单独开。</span></div>
          <div id="da-resources"><div class="side-empty">先在上方选择任务。</div></div>

          <div class="subhead gap"><h3>3 · 发布这一版演示清单</h3></div>
          <div class="publish-row">
            <input id="da-note" class="input-quiet" type="text" maxlength="200"
              placeholder="随清单说明（游客可见，例如：本次展示两轮协作与一次返修）">
            <button class="link-btn" id="da-preview">预览游客可见内容</button>
            <button class="btn primary" id="da-publish">发布新 revision</button>
          </div>
          <p class="side-caption">revision 不可变：修改范围要重新发布；重新发布会以当前勾选集为准（取消勾选即对游客不可见），旧成果版本继续保留。</p>
        </section>

        <aside class="da-side">
          <div class="panel-x">
            <div class="subhead"><h3>游客访问记录</h3></div>
            <div id="da-visitors"><div class="skeleton" style="height:60px"></div></div>
          </div>
          <div class="panel-x">
            <div class="subhead"><h3>定向邀请（可选）</h3>
              <span class="muted small">需要单独期限或单独清单时再用</span></div>
            <div class="invite-row">
              <input id="da-invite-label" class="input-quiet" type="text"
                placeholder="用途，例如：某场面试" maxlength="40">
              <select id="da-invite-ttl" class="input-quiet" aria-label="有效期">
                <option value="24">24 小时</option><option value="6">6 小时</option>
                <option value="1">1 小时</option>
              </select>
              <button class="btn small" id="da-invite">生成一次性邀请</button>
            </div>
            <div id="da-invite-out"></div>
            <div id="da-invites"></div>
          </div>
        </aside>
      </div>
    </section>`;

  function notice(text, tone = "") {
    const box = el.querySelector("#da-notice");
    box.hidden = !text;
    box.className = `notice ${tone}`;
    box.textContent = text || "";
  }

  async function loadChannel() {
    const channel = await api.publicChannel();
    // 已发布的任务预选：发布即"当前勾选集 = 游客可见集"，不静默取消已发内容
    for (const rid of channel.runs || []) {
      if (!selected.has(rid)) {
        selected.set(rid, { include: ["plan", "steps", "approvals", "artifacts"], live: false });
      }
    }
    publishedIds = new Set(channel.runs || []);
    el.querySelector("#da-channel").innerHTML = `
      <span class="badge ${channel.open ? "good" : ""}">${channel.open ? "免登演示已开放" : "免登演示已暂停"}</span>
      <span class="tag">revision ${esc(String(channel.manifest_revision || 0))}</span>
      <span class="tag">${(channel.runs || []).length} 个已发布任务</span>
      <span class="tag">在访 ${esc(String(channel.active_visitors ?? 0))} / ${esc(String(channel.max_sessions ?? "—"))}</span>
      <button class="btn small" id="da-toggle" data-open="${channel.open ? "0" : "1"}">
        ${channel.open ? "暂停对外演示" : "开放免登演示"}</button>`;
    el.querySelector("#da-toggle").addEventListener("click", async () => {
      const openNow = el.querySelector("#da-toggle").dataset.open === "1";
      if (!openNow) {
        if (!await confirmDialog({ title: "暂停对外演示", danger: true,
          body: "会立即作废全部免登访客会话；已在读的游客下一次请求即失效。已发布清单内容保留。",
          confirmText: "暂停" })) return;
      }
      try {
        await (openNow ? api.publicOpen() : api.publicClose());
        toast(openNow ? "已开放免登演示" : "已暂停并作废全部访客会话", "ok");
        await loadChannel();
        if (!openNow) await loadVisitors();
      } catch (e) { notice(e.message, "err"); }
    });
    if (!channel.published) {
      notice("管理员尚未发布演示内容：游客进入会看到「待发布」提示。", "warn");
    } else if (!channel.open) {
      notice("当前已暂停对外演示；游客入口返回「未开放」。", "");
    } else notice("");
    return channel;
  }

  async function loadRuns() {
    runs = await api.listRuns();
    const box = el.querySelector("#da-runs");
    box.innerHTML = runs.length ? runs.map((r) => `
      <label class="row-item pick ${selected.has(r.id) ? "on" : ""}">
        <input type="checkbox" data-run="${esc(r.id)}" ${selected.has(r.id) ? "checked" : ""}>
        <span class="row-main"><b>${esc(r.goal || r.id)}</b>
          <p>${esc(r.id)} · ${timeShort(r.updated_at || r.created_at)}
            ${publishedIds.has(r.id) ? "· 已在当前 revision" : ""}
            ${r.state === "succeeded" ? "· 已完成（可默认发布）"
              : `· 状态 ${esc(r.state)}；默认只发布已完成任务，进行中需勾选「实时观察」`}</p></span>
        ${stateBadge(r.state)}</label>`).join("")
      : emptyState(icon("folder", 24), "还没有可演示的任务",
          "先完成一个任务，再回来发布演示清单。");
    box.querySelectorAll("[data-run]").forEach((input) => input.addEventListener("change", () => {
      const id = input.dataset.run;
      if (input.checked) selected.set(id, { include: ["plan", "steps", "approvals", "artifacts"], live: false });
      else selected.delete(id);
      input.closest(".row-item").classList.toggle("on", input.checked);
      loadResources();
    }));
  }

  async function loadResources() {
    const ids = [...selected.keys()];
    const box = el.querySelector("#da-resources");
    if (!ids.length) {
      box.innerHTML = `<div class="side-empty">先在上方选择任务。</div>`;
      return;
    }
    assetsByRun = {};
    refCandidates = new Map();
    for (const id of ids) {
      const [detail, allAssets] = await Promise.all([
        api.getTask(id).catch(() => null), api.listAssets().catch(() => []),
      ]);
      assetsByRun[id] = (allAssets || []).filter((a) => a.run_id === id);
      const refs = (detail?.refs || []).map(ref => ({ source_id: ref.source_id,
        chunk_id: ref.locator || ref.chunk_id || "", quote: String(ref.quote || "").slice(0, 200) }));
      for (const step of detail?.steps || []) {
        try {
          const value = JSON.parse(step.result_json || "{}");
          for (const ref of value.refs || []) {
            if (ref?.source_id) {
              refs.push({ source_id: ref.source_id, chunk_id: ref.locator || "",
                quote: String(ref.quote || "").slice(0, 200) });
            }
          }
        } catch { /* 步骤没有结构化回执就没有引用候选 */ }
      }
      refCandidates.set(id, dedupe(refs));
    }
    box.innerHTML = ids.map((id) => `
      <div class="res-block">
        <div class="res-head"><b>${esc((runs.find((r) => r.id === id) || {}).goal || id)}</b>
          <label class="mini"><input type="checkbox" data-live="${esc(id)}"
            ${selected.get(id).live ? "checked" : ""}> 实时观察（开放该任务的正文增量，可传播范围更大）</label></div>
        <div class="mini-row">
          ${["plan", "steps", "approvals", "artifacts"].map((key) => `
            <label class="mini"><input type="checkbox" data-inc="${esc(id)}" data-key="${key}"
              ${selected.get(id).include.includes(key) ? "checked" : ""}> ${
                { plan: "已保存计划", steps: "步骤与交接", approvals: "审批裁定", artifacts: "成果卡片" }[key]
              }</label>`).join("")}
        </div>
        <table class="res-table"><thead><tr><th>成果版本</th><th>类型</th>
          <th>预览</th><th>下载</th></tr></thead><tbody>
          ${(assetsByRun[id] || []).length ? (assetsByRun[id] || []).map((a) => {
    const rule = assetRules.get(`${a.id}|${a.version || 1}`) || { preview: true, download: false };
    return `<tr><td>${esc(a.name || a.id)} · v${esc(String(a.version || 1))}</td>
              <td>${esc(a.mime || "—")}</td>
              <td><input type="checkbox" data-asset="${esc(a.id)}" data-ver="${esc(String(a.version || 1))}"
                aria-label="预览 ${esc(a.name || a.id)} v${esc(String(a.version || 1))}" data-what="preview" ${rule.preview ? "checked" : ""}></td>
              <td><input type="checkbox" data-asset="${esc(a.id)}" data-ver="${esc(String(a.version || 1))}"
                aria-label="下载 ${esc(a.name || a.id)} v${esc(String(a.version || 1))}" data-what="download" ${rule.download ? "checked" : ""}></td></tr>`;
  }).join("") : `<tr><td colspan="4" class="muted small">该任务没有登记成果</td></tr>`}
          </tbody></table>
        <div class="ref-block">
          <b class="small">引用片段（来自本任务记录）</b>
          ${(refCandidates.get(id) || []).length ? (refCandidates.get(id) || []).map((r) => `
            <label class="mini ref"><input type="checkbox" data-ref="${esc(id)}"
              data-source="${esc(r.source_id)}" data-chunk="${esc(r.chunk_id)}"
              data-quote="${esc(r.quote)}" ${checked.has(refKey(id, r)) ? "checked" : ""}>
              ${esc(r.source_id)}${r.chunk_id ? ` · ${esc(r.chunk_id)}` : ""}
              ${r.quote ? ` · 「${esc(r.quote.slice(0, 40))}…」` : ""}</label>`).join("")
            : `<span class="muted small">本任务没有可回查的引用记录；不发布凭空片段。</span>`}
        </div>
      </div>`).join("");

    box.querySelectorAll("[data-inc]").forEach((input) => input.addEventListener("change", () => {
      const entry = selected.get(input.dataset.inc);
      const set = new Set(entry.include);
      input.checked ? set.add(input.dataset.key) : set.delete(input.dataset.key);
      entry.include = [...set];
    }));
    box.querySelectorAll("[data-live]").forEach((input) => input.addEventListener("change", () => {
      selected.get(input.dataset.live).live = input.checked;
    }));
    box.querySelectorAll("[data-asset]").forEach((input) => input.addEventListener("change", () => {
      const key = `${input.dataset.asset}|${input.dataset.ver}`;
      const rule = assetRules.get(key) || { preview: true, download: false };
      rule[input.dataset.what] = input.checked;
      assetRules.set(key, rule);
    }));
    box.querySelectorAll("[data-ref]").forEach((input) => input.addEventListener("change", () => {
      const r = { source_id: input.dataset.source, chunk_id: input.dataset.chunk, quote: input.dataset.quote };
      input.checked ? checked.add(refKey(input.dataset.ref, r)) : checked.delete(refKey(input.dataset.ref, r));
    }));
  }

  function refKey(runId, r) { return `${runId}|${r.source_id}|${r.chunk_id}|${r.quote}`; }

  function dedupe(list) {
    const seen = new Set();
    return list.filter((r) => {
      const k = refKey("x", r);
      if (seen.has(k)) return false;
      seen.add(k);
      return true;
    });
  }

  function buildPayload() {
    const runsPayload = [...selected.entries()].map(([run_id, entry]) => ({
      run_id, include: entry.include, live: entry.live,
    }));
    const assetPayload = [];
    for (const [runId, list] of Object.entries(assetsByRun)) {
      if (!selected.has(runId)) continue;
      for (const a of list) {
        const key = `${a.id}|${a.version || 1}`;
        if (!assetRules.has(key) && !assetPayload.some((x) => x.asset_id === a.id)) continue;
        const rule = assetRules.get(key) || { preview: true, download: false };
        assetPayload.push({ asset_id: a.id, version: Number(a.version || 1),
          preview: rule.preview, download: rule.download });
      }
    }
    const refPayload = [];
    for (const [runId, list] of refCandidates.entries()) {
      for (const r of list) {
        if (!checked.has(refKey(runId, r))) continue;
        refPayload.push({ run_id: runId, source_id: r.source_id, chunk_id: r.chunk_id, quote: r.quote });
      }
    }
    return { runsPayload, assetPayload, refPayload };
  }

  async function preview() {
    const { runsPayload, assetPayload } = buildPayload();
    if (!runsPayload.length) { notice("先选择至少一个任务。", "warn"); return; }
    try {
      const out = await api.previewDemo(runsPayload, assetPayload);
      const mask = document.createElement("div");
      mask.className = "dialog-mask";
      mask.innerHTML = `<div class="dialog" role="dialog" aria-modal="true" aria-label="游客可见内容预览">
        <div class="side-head"><b>游客可见内容</b><button class="icon-btn" data-close
          aria-label="关闭">${icon("close", 15)}</button></div>
        <p class="muted small">${esc(out.note || "")}</p>
        <div class="rows">${(out.runs || []).map((r) => `<div class="row-item static">
          <span class="row-main"><b>${esc(r.goal || r.id)}</b>
            <p>${esc(r.state || "—")} · 已发布为白名单投影，不含提示词、端点与内部路径</p></span></div>`).join("")}
        </div></div>`;
      mask.addEventListener("click", (e) => {
        if (e.target === mask || e.target.closest("[data-close]")) mask.remove();
      });
      document.body.appendChild(mask);
    } catch (e) { notice(e.message, "err"); }
  }

  async function publish() {
    const { runsPayload, assetPayload, refPayload } = buildPayload();
    if (!runsPayload.length) { notice("先选择至少一个任务。", "warn"); return; }
    try {
      const out = await api.publishDefault(runsPayload, assetPayload, refPayload,
        el.querySelector("#da-note").value.trim());
      toast(`已发布 revision ${out.revision}`, "ok");
      await loadChannel();
      await loadVisitors();
    } catch (e) {
      notice(`发布失败：${e.message}`, "err");
    }
  }

  async function loadVisitors() {
    const rows = await api.listGrants("public");
    const visitors = rows.filter((r) => r.subject !== "guest:public");
    el.querySelector("#da-visitors").innerHTML = visitors.length ? visitors.map((v) => `
      <div class="visitor-row">
        <span class="row-main"><b>${esc(v.subject)}</b>
          <p>到期 ${timeShort(v.expires_at)} · revision ${esc(String(v.manifest_revision || 0))}</p></span>
        <span class="row-side">${accessBadge(v.status)}</span>
        ${v.status === "active" ? `<button class="link-btn" data-revoke="${esc(v.id)}">单独撤销</button>` : ""}
      </div>`).join("")
      : `<div class="side-empty">还没有免登访客进入；游客一进入就会出现在这里。</div>`;
    el.querySelectorAll("[data-revoke]").forEach((btn) => btn.addEventListener("click", async () => {
      if (!await confirmDialog({ title: "撤销这位游客", danger: true,
        body: "撤销事实会先写库外账本；恢复旧快照也不能让它复活。", confirmText: "撤销" })) return;
      try {
        await api.revokeGrant(btn.dataset.revoke);
        toast("已撤销", "ok");
        await loadVisitors();
      } catch (e) { notice(e.message, "err"); }
    }));
  }

  async function loadInvites() {
    const rows = await api.listGrants("invite");
    el.querySelector("#da-invites").innerHTML = rows.length ? rows.map((g) => `
      <div class="visitor-row">
        <span class="row-main"><b>${esc(g.label || g.id)}</b>
          <p>${esc(g.subject)} · 到期 ${timeShort(g.expires_at)} · revision ${
            esc(String(g.manifest_revision || 0))}</p></span>
        <span class="row-side">${accessBadge(g.status)}</span>
        ${g.status === "active" ? `<button class="link-btn" data-revoke-invite="${esc(g.id)}">撤销</button>` : ""}
      </div>`).join("") : `<div class="side-empty">没有定向邀请。</div>`;
    el.querySelectorAll("[data-revoke-invite]").forEach((btn) => btn.addEventListener("click", async () => {
      try { await api.revokeGrant(btn.dataset.revokeInvite); toast("已撤销", "ok"); await loadInvites(); }
      catch (e) { notice(e.message, "err"); }
    }));
  }

  el.querySelector("#da-refresh-runs").addEventListener("click", () => loadRuns().catch((e) => notice(e.message, "err")));
  el.querySelector("#da-preview").addEventListener("click", preview);
  el.querySelector("#da-publish").addEventListener("click", publish);
  el.querySelector("#da-invite").addEventListener("click", async () => {
    try {
      const out = await api.createGrant(el.querySelector("#da-invite-label").value.trim()
        || "定向邀请", Number(el.querySelector("#da-invite-ttl").value));
      el.querySelector("#da-invite-out").innerHTML = `
        <div class="notice warn">一次性访问码 <code>${esc(out.invite_code)}</code>
          ——只显示这一次，库内仅存哈希；到期 ${timeShort(out.expires_at)}。</div>`;
      el.querySelector("#da-invite-label").value = "";
      await loadInvites();
    } catch (e) { notice(e.message, "err"); }
  });

  try {
    await loadChannel();
    await loadRuns();
    await loadVisitors();
    await loadInvites();
  } catch (e) {
    el.innerHTML = `<div class="error-state"><div class="icon">${icon("warnsign", 28)}</div>
      <div class="title">无法读取演示访问配置</div><div class="desc">${esc(e.message)}</div></div>`;
  }
  return { destroy: () => { stopped = true; } };
}
