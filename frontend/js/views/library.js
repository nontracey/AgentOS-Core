/* U5-05 成果库：按来源任务保存的报告与媒体，预览、下载、版本与来源可回查。
 * 旧版 #/artifacts/:id 链接继续可用（服务端按主体权限裁定）。 */
"use strict";

import { openPreview } from "../preview.js";
import { esc } from "../safe-export.js";
import { outputAssets } from "../workspace-data.js";
import { assetTitle, emptyState, icon, timeShort, toast } from "../ui.js";

export async function renderLibrary(ctx) {
  const { el, api, params } = ctx;
  let stopped = false;
  let assets = [], query = "", kind = "all";

  el.innerHTML = `
    <section class="page">
      <div class="page-heading">
        <div><h2>成果库</h2><p>完成的工作，连同来源与版本一起保存。</p></div>
        <span class="tag" id="lib-count">读取中…</span>
      </div>
      <div class="library-toolbar"><input type="search" aria-label="搜索成果" placeholder="搜索成果名称…" id="lib-search"><select id="lib-kind" aria-label="成果类型"><option value="all">全部成果</option><option value="text">文档与数据</option><option value="image">图片</option><option value="video">视频</option></select></div>
      <div id="lib-grid" class="lib-grid"><div class="skeleton" style="height:180px"></div></div>
      <p class="side-caption">成果为不可变版本：修改会新建关联版本，旧版本与旧授权继续可查。</p>
    </section>`;

  async function load() {
    if (stopped) return false;
    try {
      assets = outputAssets(await api.listAssets());
    } catch (e) {
      el.querySelector("#lib-grid").innerHTML =
        `<div class="error-state"><div class="title">无法加载成果</div>
         <div class="desc">${esc(e.message)}</div></div>`;
      return false;
    }
    if (stopped) return false;
    draw();
    if (params[0]) { const a=assets.find(x=>x.id===params[0]);if(a) {try{await openPreview(api,a.id,{title:assetTitle(a),mime:a.mime});}catch(e){toast(e.message,"err");}} }
    return true;
  }

  function draw() {
    const shown=assets.filter(a=>(!query || assetTitle(a).toLowerCase().includes(query)) && (kind==="all" || (kind==="text" ? /text|json/.test(a.mime || "") : String(a.mime || "").startsWith(kind+"/"))));
    const isMd = (a) => /markdown|text|json/.test(String(a.mime || ""));
    el.querySelector("#lib-count").textContent = `${shown.length} / ${assets.length} 份成果`;
    el.querySelector("#lib-grid").innerHTML = shown.length ? shown.map((a) => `
      <article class="lib-card">
        <div class="lib-preview">${isMd(a)
          ? `<div class="lib-doc"><span class="doc-eyebrow">${esc(String(a.mime || "").includes("markdown")
              ? "MARKDOWN" : String(a.mime || "").includes("json") ? "JSON" : String(a.mime || "").includes("csv") ? "CSV" : "TEXT")}</span>
             <h3>${esc(assetTitle(a))}</h3>
             <p>${a.version ? `版本 ${a.version}` : "文档成果"}</p>
             <div class="doc-divider"></div>
             <span class="lib-paper-lines" aria-hidden="true"></span></div>`
          : `<div class="lib-media">${icon(/video/.test(a.mime || "") ? "play" : "image", 26)}
             <span>${esc(/video/.test(a.mime || "") ? "视频成果" : "媒体成果")} ·
               ${esc(a.mime || "—")}</span></div>`}</div>
        <h3 class="lib-title">${esc(assetTitle(a))}</h3>
        <p class="lib-meta">${a.version ? `v${a.version} · ` : ""}${a.parent_asset_id
          ? "关联旧版本 · " : ""}${timeShort(a.created_at)}</p>
        <details class="lib-integrity"><summary>来源与文件信息</summary>
          <p><a href="#/tasks/${esc(a.run_id || "")}">查看来源任务</a> · ${esc(a.mime || "未记录")} · ${Number(a.size || 0).toLocaleString()} 字节</p>
          <p>${a.sha256 ? `SHA-256 ${esc(String(a.sha256))}` : "校验值未记录"}</p>
        </details>
        <div class="lib-actions">
          <button class="btn small" data-lib-preview="${esc(a.id)}">${icon("eye", 14)} 预览</button>
          <button class="btn small" data-artifact-download="${esc(a.id)}"
            data-name="${esc(a.name || a.id)}">${icon("download", 14)} 下载</button>
          <a class="btn small" href="#/tasks/${esc(a.run_id || "")}">打开任务</a>
        </div>
      </article>`).join("")
      : emptyState(icon("files", 26), query || kind!=="all" ? "没有匹配的成果" : "还没有登记的成果",
        query || kind!=="all" ? "尝试其他名称或类型。" : "任务生成的文件会保存在这里；上传材料不混入成果。");

    el.querySelectorAll("[data-lib-preview]").forEach((btn) => btn.addEventListener("click", async () => {
      const a = assets.find((x) => x.id === btn.dataset.libPreview);
      try { await openPreview(api, btn.dataset.libPreview, { title: assetTitle(a || {}), mime: a?.mime }); }
      catch (e) { toast(e.isForbidden ? "权限不足，无法预览" : e.message, "err"); }
    }));
    ctx.bindCommon();
  }
  el.querySelector("#lib-search").addEventListener("input",e=>{query=e.target.value.trim().toLowerCase();draw();});
  el.querySelector("#lib-kind").addEventListener("change",e=>{kind=e.target.value;draw();});

  await load();
  return { destroy: () => { stopped = true; } };
}
