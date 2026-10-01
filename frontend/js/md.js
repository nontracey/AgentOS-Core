/* 安全 Markdown 渲染（计划 §3.4）：先整体 HTML 转义，再做受限语法变换。
 * - 原始 HTML 一律按文本显示（不执行）；
 * - 链接协议白名单 http/https/mailto，其余协议（javascript: 等）降级为纯文本；
 * - 不依赖外部库；图片不渲染为 <img>（首版文本报告）。
 */
"use strict";

const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
  ));

  const SAFE_PROTOCOL = /^(https?:|mailto:)/i;

  function inline(text) {
    // 输入为已转义文本；链接语法单独处理避免属性注入
    let out = "";
    const linkRe = /\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g;
    let last = 0, m;
    while ((m = linkRe.exec(text)) !== null) {
      out += inlinePlain(text.slice(last, m.index));
      const label = m[1];
      const href = m[2];
      if (SAFE_PROTOCOL.test(href)) {
        out += `<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>`;
      } else {
        out += `<span title="已拦截不安全链接">${label}</span>`;
      }
      last = m.index + m[0].length;
    }
    out += inlinePlain(text.slice(last));
    return out;
  }

  function inlinePlain(t) {
    return t
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|\W)\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
      .replace(/~~([^~]+)~~/g, "<del>$1</del>");
  }

  function render(src) {
    const lines = String(src ?? "").replace(/\r\n/g, "\n").split("\n");
    let html = "", i = 0;
    let para = [];
    const flush = () => {
      if (para.length) { html += `<p>${inline(esc(para.join(" ")))}</p>`; para = []; }
    };
    while (i < lines.length) {
      const line = lines[i];
      // fenced code
      const fence = line.match(/^```(\w*)\s*$/);
      if (fence) {
        flush();
        const buf = [];
        i++;
        while (i < lines.length && !/^```\s*$/.test(lines[i])) { buf.push(lines[i]); i++; }
        i++; // 跳过闭合 ```
        html += `<pre><code>${esc(buf.join("\n"))}</code></pre>`;
        continue;
      }
      // heading
      const h = line.match(/^(#{1,3})\s+(.*)$/);
      if (h) { flush(); html += `<h${h[1].length}>${inline(esc(h[2]))}</h${h[1].length}>`; i++; continue; }
      // hr
      if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) { flush(); html += "<hr>"; i++; continue; }
      // blockquote
      if (/^>\s?/.test(line)) {
        flush();
        const buf = [];
        while (i < lines.length && /^>\s?/.test(lines[i])) { buf.push(lines[i].replace(/^>\s?/, "")); i++; }
        html += `<blockquote>${inline(esc(buf.join(" ")))}</blockquote>`;
        continue;
      }
      // list（无序/有序）
      const li = line.match(/^\s*([-*+]|\d+\.)\s+(.*)$/);
      if (li) {
        flush();
        const ordered = /\d+\./.test(li[1]);
        const buf = [];
        while (i < lines.length) {
          const m2 = lines[i].match(/^\s*([-*+]|\d+\.)\s+(.*)$/);
          if (!m2) break;
          buf.push(`<li>${inline(esc(m2[2]))}</li>`);
          i++;
        }
        html += ordered ? `<ol>${buf.join("")}</ol>` : `<ul>${buf.join("")}</ul>`;
        continue;
      }
      // table（| a | b | 形式，第二行为分隔）
      if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
        flush();
        const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
        const head = cells(line);
        i += 2;
        const rows = [];
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { rows.push(cells(lines[i])); i++; }
        html += "<table><thead><tr>" + head.map((c) => `<th>${inline(esc(c))}</th>`).join("") +
          "</tr></thead><tbody>" +
          rows.map((r) => "<tr>" + r.map((c) => `<td>${inline(esc(c))}</td>`).join("") + "</tr>").join("") +
          "</tbody></table>";
        continue;
      }
      // 空行
      if (!line.trim()) { flush(); i++; continue; }
      para.push(line);
      i++;
    }
    flush();
    return html;
  }

/* ES 模块导出；Node 22（require(esm)）下 require() 返回 { render }，行为测试可用 */
export { render };
