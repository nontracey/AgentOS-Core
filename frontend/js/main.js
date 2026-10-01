/* AgentOS 工作区主入口（U5：对话式工作区 + 管理员/游客两种访问角色）
 *
 * - 外壳与导航按已接受的 U3 参照：新任务 / 任务空间 / 成果库 / 能力中心 / 偏好设置
 *   （管理员另有「演示访问」）；确认嵌在任务内，需要处理的事项汇总到顶栏通知。
 * - 访问角色只由服务端 /v1/auth/me 裁定；游客是默认角色，免登进入只读演示空间。
 * - hash 路由可直开与刷新；轮询只刷新当前页，切页/退出清理，失败退避不重叠（§3.4）。
 */
"use strict";

import { ApiError, clearGuestResources, createApi, getStaticToken, setStaticToken } from "./api.js";
import { renderDemoAccess } from "./views/demo-admin.js";
import { renderGuestAbout, renderGuestArtifacts, renderGuestExpired, renderGuestHome,
  renderGuestTask } from "./views/guest.js";
import { renderHub } from "./views/hub.js";
import { renderInbox } from "./views/inbox.js";
import { renderLibrary } from "./views/library.js";
import { renderSettings } from "./views/settings.js";
import { renderTaskNew } from "./views/task-new.js";
import { renderTasks } from "./views/tasks.js";
import { renderThread } from "./views/thread.js";
import { renderWorkspaceHome } from "./views/home.js";
import { closePreviews } from "./preview.js";
import { esc } from "./safe-export.js";
import { confirmDialog, icon, toast } from "./ui.js";
import { applyTheme } from "./theme.js";

import { ATTENTION_STATES, taskTitle } from "./workspace-data.js";

applyTheme();

/* ---------- session / api ---------- */
const api = createApi();

const session = {
  get token() { return getStaticToken(); },
  async logout() {
    await api.logout();          // 撤销 refresh + 清空本地凭据
    clearGuestResources();
    roleInfo = null;
    shellRole = null;
    poller.stop();
    go("#/enter");
  },
};

/* ---------- poller：当前页轮询 / 退避 / 清理 ---------- */
const poller = {
  timer: null, fnAsync: null, fails: 0,
  start(fn, interval = 4000) {
    this.stop();
    this.fnAsync = fn;
    const tick = async () => {
      if (!this.fnAsync) return;
      if (document.hidden) { this.timer = setTimeout(tick, interval); return; }
      let keep = true;
      try {
        keep = await this.fnAsync() !== false;
        this.fails = 0;
      } catch (e) {
        this.fails += 1;
        keep = this.fails < 5;      // 连续失败退避后停止
      }
      if (!keep || !this.fnAsync) return;
      const backoff = Math.min(interval * Math.pow(2, this.fails), 30000);
      this.timer = setTimeout(tick, backoff);
    };
    this.timer = setTimeout(tick, interval);
  },
  stop() {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null; this.fnAsync = null; this.fails = 0;
  },
};

/* ---------- 外壳（按访问角色重建）---------- */
const ADMIN_NAV = [
  { hash: "#/tasks", icon: "layers", label: "任务空间" },
  { hash: "#/library", icon: "files", label: "成果库" },
  { hash: "#/hub", icon: "blocks", label: "能力中心" },
  { hash: "#/workflows", icon: "flow", label: "工作流" },
];
const ADMIN_BOTTOM = [
  { hash: "#/home", icon: "plus", label: "新任务" },
  { hash: "#/tasks", icon: "layers", label: "任务" },
  { hash: "#/library", icon: "files", label: "成果" },
  { hash: "#/hub", icon: "blocks", label: "能力" },
  { hash: "#/settings", icon: "gear", label: "设置" },
];
const GUEST_NAV = [
  { hash: "#/guest", icon: "folder", label: "演示任务" },
  { hash: "#/guest/artifacts", icon: "files", label: "演示成果" },
  { hash: "#/guest-about", icon: "info", label: "访问说明" },
];
const GUEST_BOTTOM = [
  { hash: "#/guest", icon: "folder", label: "演示任务" },
  { hash: "#/guest/artifacts", icon: "files", label: "演示成果" },
  { hash: "#/guest-about", icon: "info", label: "访问说明" },
];

let shellRole = null;      // 当前外壳按哪个访问角色渲染
let roleInfo = null;       // /v1/auth/me 结果（subject/role/grant/到期）
let roleError = "";        // 角色查询失败原因：显示错误态，不猜 admin
let recents = [];          // 侧栏「最近任务」
let activeViewCleanup = null;

/**
 * 访问角色由服务端裁定。
 * @returns {Promise<"admin"|"guest"|null>} null = 需要显示进入页/错误态
 */
async function ensureRole() {
  roleError = "";
  try {
    roleInfo = await api.authMe();
  } catch (e) {
    if (e && e.status === 401) { roleInfo = null; return null; }
    // 非鉴权失败（服务不可达/5xx）：不假定任何角色，交回错误态处理
    roleInfo = null;
    roleError = e.message || "无法确认访问角色";
    return null;
  }
  return roleInfo.role || "guest";
}

function isGuest() { return (roleInfo?.role || "") === "guest"; }

function buildShell() {
  const guest = isGuest();
  const nav = guest ? GUEST_NAV : ADMIN_NAV;
  const bottom = guest ? GUEST_BOTTOM : ADMIN_BOTTOM;
  const member = roleInfo?.role === "member";
  const sessionName = guest ? "演示空间 · 只读访问" : member ? "企业任务空间" : "我的工作空间";
  document.body.innerHTML = `
    <div class="shell">
      <aside class="sidebar" id="sidebar">
        <a class="brand" href="${guest ? "#/guest" : "#/home"}">
          <span class="logo">${icon("flow", 20)}</span>
          <span>AgentOS<small>${guest ? "只读演示" : "AI 集成工作台"}</small></span>
        </a>
        ${guest ? "" : `<a class="nav-new" href="#/home">${icon("plus", 16)} 新任务</a>`}
        <nav class="nav-group">
          ${nav.map((n) => `<a class="nav-item" data-nav="${n.hash}" href="${n.hash}">
            <span class="icon">${icon(n.icon)}</span><span class="label">${n.label}</span>
          </a>`).join("")}
        </nav>
        ${guest ? "" : `<div class="nav-caption">最近任务</div><div class="nav-recent" id="nav-recents"></div>`}
        <div class="spacer"></div>
        ${guest || member ? "" : `<a class="nav-item" data-nav="#/demo" href="#/demo">
          <span class="icon">${icon("shield")}</span><span class="label">演示访问</span></a>`}
        ${guest ? "" : `<a class="nav-item" data-nav="#/settings" href="#/settings">
          <span class="icon">${icon("gear")}</span><span class="label">偏好设置</span></a>`}
        <div class="session" id="session-info">
          <span class="workspace-avatar">${guest ? "G" : "A"}</span>
          <div>${sessionName}<small>${guest ? "GUEST READ-ONLY" : member ? "MEMBER" : "ADMIN"}</small></div>
        </div>
      </aside>
      <div class="main">
        <header class="topbar">
          <div class="top-left">
            <button class="icon-btn menu-btn" id="menu-toggle" type="button"
              aria-label="打开导航">${icon("menu", 18)}</button>
            <span id="page-context">${guest ? "演示空间" : "我的工作空间"}</span>
          </div>
          <div class="top-right">
            <span class="tag role-tag">${guest ? "只读" : member ? "成员" : "管理员"}</span>
            ${guest ? `<button class="btn small" id="guest-exit" type="button">退出演示</button>`
              : `<button class="icon-btn" id="inbox-btn" type="button"
                   aria-label="通知与需要确认的操作">${icon("bell", 18)}<span class="nb" id="ap-badge"></span></button>
                 <a class="icon-btn" href="#/settings" aria-label="系统说明">${icon("info", 18)}</a>`}
          </div>
        </header>
        <div class="content" id="view"></div>
      </div>
    </div>
    <div class="side-scrim" id="side-scrim"></div>
    <nav class="bottom-nav">
      ${bottom.map((n) => `<a data-nav="${n.hash}" href="${n.hash}">
        <span class="icon">${icon(n.icon, 20)}</span><span>${n.label}</span></a>`).join("")}
    </nav>`;

  document.getElementById("guest-exit")?.addEventListener("click", () => session.logout());
  document.getElementById("inbox-btn")?.addEventListener("click", () => { location.hash = "#/inbox"; });
  document.getElementById("menu-toggle")?.addEventListener("click", () => {
    document.getElementById("sidebar").classList.toggle("is-open");
    document.getElementById("side-scrim").classList.toggle("on");
  });
  document.getElementById("side-scrim")?.addEventListener("click", () => {
    document.getElementById("sidebar").classList.remove("is-open");
    document.getElementById("side-scrim").classList.remove("on");
  });
  drawRecents();
}

function drawRecents() {
  const box = document.getElementById("nav-recents");
  if (!box) return;
  box.innerHTML = recents.slice(0, 5).map((r) => `
    <a href="#/tasks/${esc(r.id)}"><span class="rdot ${esc(r.state)}"></span>
      <span class="rlabel">${esc(taskTitle(r))}</span></a>`).join("")
    || `<div class="muted small r-empty">还没有任务</div>`;
}

/* ---------- 共享事件（审批决定 / 下载 / 预览）：document 级委托 ----------
 * 绑在常驻节点上：轮询重渲染替换按钮节点后点击依然命中（N5 竞态修复）。 */
let commonBound = false;
function bindCommon() {
  if (commonBound) return;
  commonBound = true;
  document.addEventListener("click", async (e) => {
    const decide = e.target.closest("[data-decide]");
    if (decide) {
      const card = decide.closest(".approval-card");
      card.querySelectorAll("button").forEach((b) => (b.disabled = true));
      const reenable = setTimeout(() => {
        card.querySelectorAll("button").forEach((b) => (b.disabled = false));
      }, 15_000);
      try {
        await api.decideApproval(decide.dataset.id, decide.dataset.decide);
        clearTimeout(reenable);
        toast(decide.dataset.decide === "approve" ? "已批准，任务继续执行" : "已拒绝",
          decide.dataset.decide === "approve" ? "ok" : "");
        route();
      } catch (err) {
        clearTimeout(reenable);
        card.querySelectorAll("button").forEach((b) => (b.disabled = false));
        toast(err.isForbidden ? "权限不足" : err.message, "err");
      }
      return;
    }
    const dl = e.target.closest("[data-artifact-download]");
    if (dl) {
      e.stopPropagation();
      try {
        const blob = isGuest()
          ? (await api.demoDownload(dl.dataset.artifactDownload)).blob
          : await api.downloadAsset(dl.dataset.artifactDownload);
        saveBlob(blob, dl.dataset.name || "artifact");
      } catch (err) {
        toast(err.isForbidden ? "该资源未开放下载" : err.message, "err");
      }
    }
  });
}

export function saveBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.dataset.blobUrl = url;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

/* ---------- 进入页：游客为默认角色，管理员需要密码 ---------- */
async function renderEnter(el, reason) {
  poller.stop();
  document.body.classList.add("login-mode");
  document.body.innerHTML = `
    <div class="enter-wrap">
      <div class="enter-card">
        <div class="enter-brand"><span class="logo">${icon("flow", 22)}</span>
          <div><b>AgentOS</b><small>目标驱动 · 可治理 · 可追溯的AI 集成工作台</small></div></div>
        ${reason ? `<div class="notice err">${esc(reason)}</div>` : ""}
        <button class="btn primary block" id="enter-guest" type="button">
          ${icon("eye", 16)} 以游客身份进入演示空间
        </button>
        <p class="enter-note">游客为只读角色：只能查看管理员发布的演示任务与已开放资源，
          不能创建、修改、审批或下载未授权内容。</p>
        <div class="notice" id="guest-hint" hidden></div>
        <div class="divider"><span>或</span></div>
        <div id="enterprise-logins" aria-label="企业账号登录"></div>
        <form id="lg-form" class="enter-admin">
          <input type="hidden" name="username" value="owner" autocomplete="username">
          <div class="field">
            <label for="lg-pass">管理员密码</label>
            <input id="lg-pass" type="password" autocomplete="current-password"
              placeholder="输入访问密码" required>
          </div>
          <label class="login-remember"><input id="lg-remember" type="checkbox" checked>
            保持登录 30 天</label>
          <div id="lg-err" class="field" hidden><span class="error-text"></span></div>
          <button class="btn block" type="submit" id="lg-submit">进入管理工作台</button>
        </form>
        <details class="login-advanced">
          <summary>使用访问令牌登录（高级）</summary>
          <div class="field" style="margin-top:10px">
            <input id="lg-token" type="password" autocomplete="off" placeholder="粘贴 owner 令牌">
            <button class="btn block small" id="lg-token-btn" type="button">用令牌连接</button>
          </div>
        </details>
        <p class="login-foot">按部署配置提供企业账号或本地登录</p>
      </div>
    </div>`;

  const hint = document.getElementById("guest-hint");
  const providers = await fetch("/v1/auth/providers").then(r => r.ok ? r.json() : []).catch(() => []);
  const enterprise = document.getElementById("enterprise-logins");
  for (const provider of providers) {
    const link = document.createElement("a");
    link.className = "btn primary block";
    link.textContent = `通过 ${provider.label} 企业账号登录`;
    link.href = `/v1/auth/oidc/${encodeURIComponent(provider.id)}/login`;
    enterprise.appendChild(link);
  }
  if (providers.length) {
    const logins = document.getElementById("enterprise-logins");
    document.querySelector(".enter-brand").after(logins);
    document.getElementById("lg-submit").textContent = "使用本地管理员账号登录";
  }
  const setup = await api.authSetup().catch(() => ({}));
  if (setup?.guest && setup.guest.open && !setup.guest.published) {
    hint.hidden = false;
    hint.textContent = "管理员已开放演示入口，但尚未发布内容；进入后会看到待发布提示。";
  } else if (setup?.guest && !setup.guest.open) {
    hint.hidden = false;
    hint.textContent = "管理员尚未开放免登演示；如需查看请联系管理员开放，或使用定向邀请。";
  }
  if (setup?.initial_password_pending) {
    document.getElementById("lg-err").hidden = false;
    document.getElementById("lg-err").querySelector(".error-text").textContent =
      "首次使用：初始密码在服务端数据目录的 owner_password.once 文件中，登录后请在「偏好设置」修改密码。";
  }

  const returnTo = /^#\/(tasks(?:\/[^?]+)?|library(?:\/[^?]+)?|hub|demo|settings)$/.test(location.hash)
    ? location.hash : "#/home";
  const enterGuest = async () => {
    const btn = document.getElementById("enter-guest");
    btn.disabled = true;
    try {
      await api.guestBootstrap();
      roleInfo = null; shellRole = null;
      go("#/guest");
    } catch (e) {
      hint.hidden = false;
      hint.textContent = e.message;
      btn.disabled = false;
    }
  };
  document.getElementById("enter-guest").addEventListener("click", enterGuest);

  document.getElementById("lg-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const btn = document.getElementById("lg-submit");
    const err = document.getElementById("lg-err");
    btn.disabled = true;
    err.hidden = true;
    try {
      await api.login(document.getElementById("lg-pass").value,
        document.getElementById("lg-remember").checked);
      roleInfo = null; shellRole = null;
      go(returnTo);
    } catch (e2) {
      err.hidden = false;
      err.querySelector(".error-text").textContent = e2.message;
    } finally { btn.disabled = false; }
  });

  document.getElementById("lg-token-btn").addEventListener("click", async () => {
    const token = document.getElementById("lg-token").value.trim();
    const err = document.getElementById("lg-err");
    if (!token) return;
    setStaticToken(token);
    try {
      await api.listRuns();                 // 鉴权探测：无效令牌绝不放行
      roleInfo = null; shellRole = null;
      go(returnTo);
    } catch (e2) {
      sessionStorage.removeItem("agentos_token");
      err.hidden = false;
      err.querySelector(".error-text").textContent =
        e2.isAuthExpired ? "令牌无效，请重新输入" : e2.message;
    }
  });
}

/* ---------- 路由 ---------- */
const ADMIN_ROUTES = { home: "新任务", tasks: "任务空间", library: "成果库", hub: "能力中心",
  settings: "偏好设置", workflows: "工作流", demo: "演示访问", inbox: "通知" };
const GUEST_ROUTES = { guest: "演示任务", "guest/artifacts": "演示成果", "guest-about": "访问说明" };

function ctxFor(el, params) {
  return { el, api, params, session, bindCommon, navigate: (h) => { location.hash = h; }, saveBlob };
}

let routeVersion = 0;

/** 导航到指定 hash：同一 hash 不会触发 hashchange，此时手动 route。 */
function go(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

async function route() {
  document.getElementById("sidebar")?.classList.remove("is-open");
  document.getElementById("side-scrim")?.classList.remove("on");
  const version = ++routeVersion;
  poller.stop();
  window.scrollTo(0, 0);
  if (typeof activeViewCleanup === "function") {
    try { activeViewCleanup(); } catch (_) { /* 视图自行忽略 */ }
    activeViewCleanup = null;
  }
  const hash = location.hash || "#/enter";
  const raw = hash.replace(/^#\//, "").split("?")[0];
  const parts = raw.split("/").filter(Boolean);
  const view = parts[0] || "";
  const query = new URLSearchParams(hash.split("?")[1] || "");
  const params = parts.slice(1);

  // 旧链接兼容：#/workbench 与 #/workbench?focus=approvals 进新的对应入口
  if (view === "workbench") {
    location.hash = query.get("focus") === "approvals" ? "#/inbox" : "#/home";
    return;
  }
  if (view === "guest" && query.get("focus") === "approvals") { location.hash = "#/guest"; return; }

  document.querySelectorAll("[data-nav]").forEach((a) => {
    a.classList.toggle("active", a.getAttribute("data-nav") === `#/${raw}`
      || (a.getAttribute("data-nav") === "#/tasks" && view === "tasks" && params[0]));
  });

  // 未连接：进入页（游客免登或管理员登录）；带 refresh 无 access 时 request() 会静默续期
  if (!api.hasSession() && view !== "guest-expired") {
    if (view.startsWith("guest")) { await autoEnterGuest(view, params); return; }
    document.getElementById("sidebar")?.classList.remove("is-open");
    await renderEnter(document.body, roleError);
    return;
  }
  document.body.classList.remove("login-mode");

  const resolved = await ensureRole();
  if (resolved === null) {
    if (roleError) {
      document.body.classList.remove("login-mode");
      ensureShell();
      const errorMount = document.getElementById("view");
      errorMount.innerHTML = `<div class="error-state"><div class="icon">${icon("warnsign", 30)}</div>
        <div class="title">无法确认访问角色</div>
        <div class="desc">${esc(roleError)}｜为避免误判权限，这里不假设你是管理员。</div>
        <div class="row" style="justify-content:center">
          <button class="btn" id="retry-role">重试</button>
          <button class="btn" id="back-enter">返回进入页</button></div></div>`;
      document.getElementById("retry-role").onclick = () => route();
      document.getElementById("back-enter").onclick = () => { location.hash = "#/enter"; };
      return;
    }
    await renderEnter(document.body, "");
    return;
  }
  if (resolved !== shellRole) { shellRole = resolved; buildShell(); }
  ensureShell();
  setPageContext(view, params, isGuest());
  if (isGuest() && !raw.startsWith("guest") && raw !== "") {
    location.hash = "#/guest";
    return;
  }
  if (!isGuest() && raw.startsWith("guest") && raw !== "guest-expired") {
    location.hash = "#/home";
    return;
  }

  // 外壳就绪后再取挂载点（进入页无外壳，早取会得到 null）
  const mount = document.getElementById("view");
  const el = document.createElement("div");
  mount.replaceChildren(el);
  const ctx = ctxFor(el, params);
  let out = null;
  try {
    if (isGuest()) {
      if (view === "guest" && params[0] === "tasks" && params[1]) {
        ctx.params = [params[1]];
        out = await renderGuestTask(ctx);
      } else if (view === "guest" && params[0] === "artifacts") {
        out = await renderGuestArtifacts(ctx);
      } else if (view === "guest" && !params.length) out = await renderGuestHome(ctx);
      else if (view === "guest-about") out = renderGuestAbout(ctx);
      else if (view === "guest-expired") {
        out = renderGuestExpired(ctx, sessionStorage.getItem("agentos_guest_expired")
          || "演示访问已过期或被管理员撤销。");
      } else { location.hash = "#/guest"; return; }
    } else if (view === "home" || view === "") out = await renderWorkspaceHome(ctx);
    else if (view === "tasks" && params[0] === "new") out = await renderTaskNew(ctx);
    else if (view === "tasks" && params[0]) { ctx.params = params; out = await renderThread(ctx); }
    else if (view === "tasks") out = await renderTasks(ctx, { refreshRecents });
    else if (view === "library" || view === "artifacts") out = await renderLibrary(ctx);  // 旧链接兼容
    else if (view === "workflows") {
      if (!document.getElementById("workflow-css")) {
        const link = document.createElement("link"); link.id = "workflow-css";
        link.rel = "stylesheet"; link.href = "/ui/workflow-dist/editor.css"; document.head.append(link);
      }
      const editor = await import("/ui/workflow-dist/editor.js");
      out = {destroy: editor.mount(el, {request: api.workflowRequest, navigate: go, confirm: body => confirmDialog({title: "替换当前草稿？", body, confirmText: "替换草稿"})})};
    }
    else if (view === "hub") out = await renderHub(ctx);
    else if (view === "inbox") out = await renderInbox(ctx);
    else if (view === "demo") out = await renderDemoAccess(ctx);
    else if (view === "settings") out = await renderSettings(ctx);
    else out = await renderWorkspaceHome(ctx);
  } catch (e) {
    if (isGuest() && (e.status === 401 || e.status === 403 || e.status === 404)) {
      renderGuestExpired(ctx, "访问已结束或被管理员撤销。");
      return;
    }
    el.innerHTML = `<div class="error-state"><div class="icon">${icon("warnsign", 30)}</div>
      <div class="title">页面加载失败</div><div class="desc">${esc(e.message)}</div>
      <div class="row" style="justify-content:center">
        <button class="btn" id="page-retry">重试</button>
        <a class="btn" href="#/home">返回工作区</a></div></div>`;
    document.getElementById("page-retry").onclick = () => route();
    return;
  }

  if (version !== routeVersion) { out?.destroy?.(); return; }
  if (out?.poll) poller.start(out.poll, 4000);
  if (out?.destroy) activeViewCleanup = () => { out.destroy?.(); closePreviews(); };
  else activeViewCleanup = null;
  if (!isGuest()) { refreshBadges(); loadRecents(); }
}

function ensureShell() {
  if (!document.querySelector(".shell")) buildShell();
}

/** 游客路由但尚无会话：默认角色免登进入，不再要求访问码。 */
async function autoEnterGuest(view, params) {
  if (!view.startsWith("guest")) return renderEnter(document.body, "");
  document.body.classList.remove("login-mode");
  ensureShell();
  const el = document.createElement("div");
  const mount = document.getElementById("view");
  mount.replaceChildren(el);
  const ctx = ctxFor(el, params);
  try {
    await api.guestBootstrap();
    roleInfo = null; shellRole = null;
    route();
  } catch (e) {
    el.innerHTML = `<div class="error-state"><div class="icon">${icon("shield", 30)}</div>
      <div class="title">暂时无法进入演示空间</div>
      <div class="desc">${esc(e.message)}</div>
      <div class="row" style="justify-content:center">
        <button class="btn" id="again">重试</button>
        <a class="btn" href="#/enter">返回进入页</a></div></div>`;
    document.getElementById("again").onclick = () => route();
  }
  return null;
}

function setPageContext(view, params, guest) {
  const label = document.getElementById("page-context");
  if (!label) return;
  if (guest) {
    const key = view === "guest" ? (params[0] === "artifacts" ? "guest/artifacts"
      : params[0] === "tasks" ? "任务详情" : "guest") : view;
    label.textContent = GUEST_ROUTES[key] || "演示任务";
    return;
  }
  label.textContent = view === "tasks" && params[0] ? "任务详情"
    : (ADMIN_ROUTES[view] || "我的工作空间");
}

async function loadRecents() {
  try {
    recents = (await api.listRuns()).slice(0, 8);
    drawRecents();
  } catch (_) { /* 侧栏最近任务失败不影响当前视图 */ }
}

function refreshRecents() { loadRecents(); }

/* 通知角标：管理员按部署范围统计待处理事项（不新建后端状态） */
async function refreshBadges() {
  try {
    const [approvals, runs] = await Promise.all([api.listApprovals(), api.listRuns()]);
    const pending = new Set(runs.filter(r => ATTENTION_STATES.has(r.state)).map(r => r.id));
    approvals.forEach(a => pending.add(String(a.call_id || a.id).split(":")[0]));
    const needAttention = pending.size;
    const badge = document.getElementById("ap-badge");
    if (badge) badge.textContent = needAttention ? String(needAttention) : "";
    recents = runs.slice(0, 8);
    drawRecents();
  } catch (e) {
    if (e instanceof ApiError && e.isAuthExpired) {
      await api.logout();
      roleInfo = null; shellRole = null;
      go("#/enter");
      return;
    }
    if (e instanceof ApiError && e.status === 0) toast("服务不可达，正在重试…", "err");
  }
}

/* ---------- boot ---------- */
try {
  const response = await fetch("/v1/auth/browser-session", { cache: "no-store" });
  if (response.ok) {
    setStaticToken("sso-cookie");
    if (!location.hash || location.hash === "#/enter") location.hash = "#/home";
  }
} catch (_) { /* local login remains usable */ }
buildShell();
if ("serviceWorker" in navigator) {
  // 静态外壳缓存升级由 sw.js 版本控制；API/私有素材不进缓存
  const hadController = Boolean(navigator.serviceWorker.controller);
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (hadController) toast("界面已有更新，请先保存未提交内容，再刷新载入。", "");
  });
  navigator.serviceWorker.register("/ui/sw.js").then((reg) => {
    const activate = () => reg.waiting?.postMessage({ type: "ACTIVATE_SHELL" });
    activate();
    reg.addEventListener("updatefound", () => reg.installing?.addEventListener("statechange", activate));
  }).catch(() => { /* 无 SW 也能用 */ });
}
window.addEventListener("hashchange", () => { window.scrollTo(0,0); route(); });
window.addEventListener("pagehide", () => { poller.stop(); activeViewCleanup?.(); });
window.addEventListener("pageshow", (e) => { if (e.persisted) route(); });
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    if (!isGuest()) { location.hash = "#/home"; setTimeout(() => {
      document.getElementById("goal-input")?.focus();
    }, 60); }
  }
});
route();
