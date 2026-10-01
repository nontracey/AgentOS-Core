/* ES 模块版转义原语：与 safe.js（经典脚本，Node 行为测试用）保持同一实现。
 * 修改任一实现必须同步另一份——test_frontend_security 是行为基线。 */
"use strict";

export const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
  ));

export const jsq = (v) => esc(String(v ?? "").replace(/[\\'`]/g, "\\$&"));
