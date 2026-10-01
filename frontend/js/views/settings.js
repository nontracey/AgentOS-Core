/* U5-03 偏好设置：当前会话与凭据、界面与数据边界说明、只读能力摘要。
 * 服务端未实现的可编辑项不伪装成开关；能力明细在「能力中心」查看。 */
"use strict";

import { esc } from "../safe-export.js";
import { icon, modelZh, toast } from "../ui.js";
import { applyTheme } from "../theme.js";

import { roleLabel } from "../workspace-data.js";

function row(label, value, note = "") {
  return `<div class="settings-row"><span>${esc(label)}${
    note ? `<small>${esc(note)}</small>` : ""}</span><b>${value}</b></div>`;
}

export async function renderSettings(ctx) {
  const { el, api, session } = ctx;
  const [caps, me] = await Promise.all([
    api.getCapabilities().catch(() => null),
    api.authMe().catch(() => null),
  ]);
  const nodes = caps?.nodes || [];
  const scriptModel = caps?.model?.provider === "scripted";

  el.innerHTML = `
    <section class="page settings-page">
      <div class="page-heading"><div><h2>偏好设置</h2>
        <p>先按你的工作方式组织界面；权限、预算与审批由服务端裁定。</p></div></div>

      <div class="settings-group">
        <div class="subhead"><h3>当前会话</h3></div>
        ${row("访问角色", `<span class="badge good">${esc(roleLabel(me?.role))}</span>`,
          "权限以当前账号的服务端授权为准")}
        ${row("主体", `<code>${esc(me?.subject || "未读取")}</code>`)}
        ${row("凭据存放", "<b>仅本浏览器</b>",
          "access 令牌在 sessionStorage；勾选保持登录时长驻 refresh（30 天），退出即清")}
        <div class="row gap">
          ${me?.role === "admin" ? `<a class="btn small" href="#/demo">${icon("shield", 14)} 演示访问（游客发布与撤销）</a>` : ""}
          <button class="btn small danger" id="st-logout">退出登录</button>
        </div>
      </div>

      <div class="settings-group">
        <div class="subhead"><h3>执行环境摘要</h3>
          <a class="link-btn" href="#/hub">到能力中心查看明细 ${icon("arrow", 12)}</a></div>
        ${row("模型", `<b>${esc(modelZh(caps?.model?.name))}</b> ·
          ${scriptModel ? "本轮不产生真实云端调用" : "已配置，连通性以任务实测为准"}`)}
        ${row("兜底端点", caps?.fallback?.configured
          ? `<b>${esc(modelZh(caps.fallback.name))}</b>` : "未配置")}
        ${row("执行设备", `<b>${nodes.filter((n) => n.status === "paired").length}</b> 个已配对`,
          "配对不等于在线；实际派发在任务详情中核对")}
        ${row("默认模板", `<b>${esc(caps?.template?.id || "未记录")}</b> v${esc(caps?.template?.version || "—")}`)}
      </div>

      <div class="settings-group">
        <div class="subhead"><h3>界面与数据</h3></div>
        <div class="settings-row"><label for="appearance">外观</label>
          <select id="appearance"><option value="dark">深色</option><option value="light">浅色</option><option value="system">跟随系统</option></select></div>
        ${row("工作区布局", "对话优先（电脑并排成果与步骤检查器，手机按需展开）")}
        ${row("任务历时与用量", "按服务端已记录值显示；未记录项保持「未记录」")}
        ${row("离线与缓存", "静态外壳可离线打开（Service Worker），API 与私有素材不缓存")}
        ${row("旧版控制台", `<a href="/ui/legacy.html">/ui/legacy.html</a>`,
          "保留过渡兼容，不等同当前工作区")}
      </div>

      ${me?.role === "admin" ? `<div class="settings-group">
        <div class="subhead"><h3>修改管理员密码</h3></div>
        <form id="st-pw" class="pw-form">
          <div class="field"><label for="pw-current">当前密码</label>
            <input id="pw-current" type="password" autocomplete="current-password" required></div>
          <div class="field"><label for="pw-new">新密码（至少 8 位）</label>
            <input id="pw-new" type="password" autocomplete="new-password" required minlength="8"></div>
          <div class="field"><label for="pw-confirm">确认新密码</label>
            <input id="pw-confirm" type="password" autocomplete="new-password" required minlength="8"></div>
          <div id="pw-msg" class="field" hidden><span class="error-text"></span></div>
          <button class="btn primary small" type="submit">更新密码</button>
          <span class="muted small">修改后所有已登录会话失效，需要重新登录。</span>
        </form>
      </div>` : ""}
    </section>`;

  const appearance = el.querySelector("#appearance");
  appearance.value = localStorage.getItem("agentos_theme") || "dark";
  appearance.addEventListener("change", () => applyTheme(appearance.value));

  el.querySelector("#st-logout").addEventListener("click", () => session.logout());
  el.querySelector("#st-pw")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = el.querySelector("#pw-msg");
    msg.hidden = true;
    const current = el.querySelector("#pw-current").value;
    const next = el.querySelector("#pw-new").value;
    const again = el.querySelector("#pw-confirm").value;
    if (next !== again) {
      msg.hidden = false;
      msg.querySelector(".error-text").textContent = "两次输入的新密码不一致";
      return;
    }
    try {
      await api.changePassword(current, next);
      toast("密码已更新，请重新登录", "ok");
      setTimeout(() => session.logout(), 800);
    } catch (e2) {
      msg.hidden = false;
      msg.querySelector(".error-text").textContent = e2.message;
    }
  });
  return { poll: null };
}
