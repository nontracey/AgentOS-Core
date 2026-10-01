/* U5-05 成果预览：鉴权 fetch → Blob → 安全渲染；切页/关闭释放 URL。
 * 令牌不进 URL；预览与下载分别由服务端核权（能预览不等于能下载）。 */
"use strict";

import { esc } from "./safe-export.js";
import { icon } from "./ui.js";
import { render as renderMarkdown } from "./md.js";

const opened = [];

/** 关闭全部预览并释放 Blob（退出/撤权/切页时调用）。 */
export function closePreviews() {
  while (opened.length) {
    const { url, node } = opened.pop();
    if (url && url.startsWith("blob:")) URL.revokeObjectURL(url);
    node?.remove();
  }
}

/**
 * 打开成果预览对话框。
 * @param {object} api
 * @param {string} assetId
 * @param {{guest?:boolean, title?:string, mime?:string, meta?:string}} opts
 */
export async function openPreview(api, assetId, opts = {}) {
  const blob = opts.guest
    ? (await api.demoPreview(assetId)).blob      // 预览走 preview 判据，不等于下载授权
    : await api.downloadAsset(assetId);
  const mime = opts.mime || blob.type || "";
  const mask = document.createElement("div");
  mask.className = "dialog-mask preview-mask";
  mask.innerHTML = `<div class="dialog preview-dialog" role="dialog" aria-modal="true"
      aria-label="成果预览：${esc(opts.title || assetId)}">
    <div class="preview-head"><b>${esc(opts.title || "成果预览")}</b>
      <span class="muted small">${esc(opts.meta || mime || "—")} · ${blob.size.toLocaleString()} 字节</span>
      <button class="icon-btn" data-close aria-label="关闭预览">${icon("close", 16)}</button></div>
    <div class="preview-body"></div></div>`;
  const body = mask.querySelector(".preview-body");
  const url = URL.createObjectURL(blob);
  opened.push({ url, node: mask });

  if (/^image\//.test(mime)) {
    const img = document.createElement("img");
    img.src = url;
    img.alt = opts.title || "图片成果";
    body.appendChild(img);
  } else if (/^video\//.test(mime)) {
    const video = document.createElement("video");
    video.src = url;
    video.controls = true;
    body.appendChild(video);
  } else if (/text|json|markdown/.test(mime) || mime === "") {
    const text = await blob.text();
    const holder = document.createElement("div");
    holder.className = "md-view";
    // Markdown 渲染默认禁用原始 HTML（md.js 内已做协议白名单与转义）
    holder.innerHTML = mime.includes("json")
      ? `<pre>${esc(text.slice(0, 200_000))}</pre>` : renderMarkdown(text);
    body.appendChild(holder);
  } else {
    body.innerHTML = `<div class="empty-state"><div class="icon">${icon("files", 26)}</div>
      <div class="title">该类型不在此处预览</div>
      <div class="desc">${esc(mime || "未知类型")} 需要本地程序打开，请使用下载。</div></div>`;
  }

  mask.addEventListener("click", (e) => {
    if (e.target === mask || e.target.closest("[data-close]")) {
      URL.revokeObjectURL(url);
      const idx = opened.findIndex((it) => it.url === url);
      if (idx >= 0) opened.splice(idx, 1);
      mask.remove();
    }
  });
  document.body.appendChild(mask);
  mask.querySelector("[data-close]").focus();
}
