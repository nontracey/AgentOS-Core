/* 前端安全转义原语（可被 Node 直接 eval 做行为测试，无 DOM 依赖）。 */
"use strict";

/* HTML 文本上下文转义：& < > " ' 全部实体化 */
const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
  ));

/* JS 字符串上下文转义（内联事件处理器的单引号串），先转危险引号再 HTML 转义 */
const jsq = (v) => esc(String(v ?? "").replace(/[\\'`]/g, "\\$&"));
