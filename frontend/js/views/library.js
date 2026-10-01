/* U5-05 成果库：按来源任务保存的报告与媒体，预览、下载、版本与来源可回查。
 * 旧版 #/artifacts/:id 链接继续可用（服务端按主体权限裁定）。 */
"use strict";

import { openPreview } from "../preview.js";
import { esc } from "../safe-export.js";
import { assetTitle, emptyState, icon, timeShort, toast } from "../ui.js";

export async function renderLibrary(ctx) {
  const { el, api, navigate, params } = ctx;
  let stopped = false;
  let assets = [];

  el.innerHTML = `
    <section class="page">
      <div class="page-heading">
        <div><h2>成果库</h2><p>完成的工作，连同来源与版本一起保存。</p></div>
        <span class="tag" id="lib-count">读取中…</span>
      </div>
      <div id="lib-grid" class="lib-grid"><div class="skeleton" style="height:180px"></div></div>
      <p class="side-caption">成果为不可变版本：修改会新建关联版本，旧版本与旧授权继续可查。</p>
    </section>`;

  async function load() {
    if (stopped) return false;
    try {
      assets = (await api.listAssets()).filter((a) => a.run_id || a.id);
    } catch (e) {
      el.querySelector("#lib-grid").innerHTML =
        `<div class="error-state"><div class="title">无法加载成果</div>
         <div class="desc">${esc(e.message)}</div></div>`;
      return false;
    }
    if (stopped) return false;
    const isMd = (a) => /markdown|text/.test(String(a.mime || ""));
    el.querySelector("#lib-count").textContent = `${assets.length} 份已登记`;
    el.querySelector("#lib-grid").innerHTML = assets.length ? assets.map((a) => `
      <article class="lib-card">
        <div class="lib-preview">${isMd(a)
          ? `<div class="lib-doc"><span class="doc-eyebrow">${esc(String(a.mime || "").includes("markdown")
              ? "MARKDOWN" : "TEXT")}</span>
             <h3>${esc(assetTitle(a))}</h3>
             <p>${a.version ? `v${a.version} · ` : ""}${esc(a.mime || "—")} ·
                ${Number(a.size || 0).toLocaleString()} 字节</p>
             <div class="doc-divider"></div>
             <p class="muted small">${a.sha256 ? `sha256 ${esc(String(a.sha256).slice(0, 16))}…`
                : "校验值未记录"}</p></div>`
          : `<div class="lib-media">${icon(/video/.test(a.mime || "") ? "play" : "image", 26)}
             <span>${esc(/video/.test(a.mime || "") ? "视频成果" : "媒体成果")} ·
               ${esc(a.mime || "—")}</span></div>`}</div>
        <h3 class="lib-title">${esc(assetTitle(a))}</h3>
        <p class="lib-meta">${a.version ? `v${a.version} · ` : ""}${a.parent_asset_id
          ? "关联旧版本 · " : ""}${timeShort(a.created_at)} · 来源任务
          <a href="#/tasks/${esc(a.run_id || "")}">${esc(String(a.run_id || "未记录"))}</a></p>
        <div class="lib-actions">
          <button class="btn small" data-lib-preview="${esc(a.id)}">${icon("eye", 14)} 预览</button>
          <button class="btn small" data-artifact-download="${esc(a.id)}"
            data-name="${esc(a.name || a.id)}">${icon("download", 14)} 下载</button>
          <a class="btn small" href="#/tasks/${esc(a.run_id || "")}">打开任务</a>
        </div>
      </article>`).join("")
      : emptyState(icon("files", 26), "还没有登记的成果",
        "任务产出通过服务端检查后登记在这里；未登记时不显示下载。");

    el.querySelectorAll("[data-lib-preview]").forEach((btn) => btn.addEventListener("click", async () => {
      const a = assets.find((x) => x.id === btn.dataset.libPreview);
      try { await openPreview(api, btn.dataset.libPreview, { title: assetTitle(a || {}), mime: a?.mime }); }
      catch (e) { toast(e.isForbidden ? "权限不足，无法预览" : e.message, "err"); }
    }));
    ctx.bindCommon();
    // 旧链接：#/artifacts/<id> 直接打开预览
    if (params[0]) {
      const a = assets.find((x) => x.id === params[0]);
      if (a) {
        try { await openPreview(api, a.id, { title: assetTitle(a), mime: a.mime }); }
        catch (e) { toast(e.message, "err"); }
      }
      navigate("#/library");
    }
    return true;
  }

  await load();
  return { destroy: () => { stopped = true; } };
}
