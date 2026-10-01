/* API 客户端：双 token 管理 + DTO 归一。
 * - access（30min，sessionStorage）+ refresh（保持登录 30 天 → localStorage；否则 sessionStorage）；
 * - 401 自动用 refresh 续期一次并重放原请求；续期失败清会话回登录页；
 * - 静态令牌粘贴（高级回退）存 sessionStorage 的 agentos_token，同样可用；
 * - 403 显示权限不足，不误报会话失效；下载鉴权 fetch → Blob，令牌不进 URL。
 */
"use strict";

const ACCESS_KEY = "agentos_access";
const REFRESH_PERSIST_KEY = "agentos_refresh";        // 保持登录（localStorage）
const REFRESH_SESSION_KEY = "agentos_refresh_session"; // 不保持（sessionStorage）
const SESSION_KIND_KEY = "agentos_session_kind";
const STATIC_TOKEN_KEY = "agentos_token";              // 高级回退：粘贴令牌（sessionStorage）

export const TOKEN_KEY = STATIC_TOKEN_KEY;

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code || "unknown";
  }
  get isAuthExpired() { return this.status === 401; }
  get isForbidden() { return this.status === 403; }
}

export function getAccessToken() {
  return sessionStorage.getItem(ACCESS_KEY) || "";
}
export function getRefreshToken() {
  const temporary = sessionStorage.getItem(REFRESH_SESSION_KEY);
  if (sessionStorage.getItem(SESSION_KIND_KEY) === "guest") return temporary || "";
  return temporary || localStorage.getItem(REFRESH_PERSIST_KEY) || "";
}
export function getStaticToken() {
  return sessionStorage.getItem(STATIC_TOKEN_KEY) || "";
}
export function getToken() {
  return getAccessToken() || getStaticToken();
}
function storeTokens(access, refresh, remember, kind = sessionStorage.getItem(SESSION_KIND_KEY) || "admin") {
  sessionStorage.setItem(SESSION_KIND_KEY, kind);
  sessionStorage.setItem(ACCESS_KEY, access);
  if (refresh) {
    if (remember && kind !== "guest") {
      localStorage.setItem(REFRESH_PERSIST_KEY, refresh);
      sessionStorage.removeItem(REFRESH_SESSION_KEY);
    } else {
      sessionStorage.setItem(REFRESH_SESSION_KEY, refresh);
      if (kind !== "guest") localStorage.removeItem(REFRESH_PERSIST_KEY);
    }
  }
}
export function clearAuth() {
  sessionStorage.removeItem(ACCESS_KEY);
  sessionStorage.removeItem(REFRESH_SESSION_KEY);
  sessionStorage.removeItem(STATIC_TOKEN_KEY);
  if (sessionStorage.getItem(SESSION_KIND_KEY) !== "guest") localStorage.removeItem(REFRESH_PERSIST_KEY);
  Object.keys(sessionStorage).filter(k => k.startsWith("agentos_draft_")).forEach(k => sessionStorage.removeItem(k));
  sessionStorage.removeItem("agentos_goal_draft");
  sessionStorage.removeItem("agentos_revise_source");
  sessionStorage.removeItem("agentos_revise_asset");
}
/** 高级回退：粘贴令牌（静态 owner token），仅本标签页会话。 */
export function setStaticToken(t) {
  sessionStorage.setItem(SESSION_KIND_KEY, "admin");
  sessionStorage.setItem(STATIC_TOKEN_KEY, t);
}

export function createApi() {
  let refreshInFlight = null;

  async function rawFetch(path, options) {
    const headers = { ...(options.headers || {}) };
    const token = getToken();
    if (token) headers.Authorization = `Bearer ${token}`;
    if (options.body !== undefined && !(options.body instanceof Blob)
        && !(options.body instanceof FormData)) {
      headers["Content-Type"] = "application/json";
      options = { ...options, body: JSON.stringify(options.body) };
    }
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), options.timeoutMs ?? 30_000);
    let resp;
    try {
      resp = await fetch(path, { ...options, headers, signal: ctrl.signal });
    } catch (e) {
      throw new ApiError(0, "network",
        e.name === "AbortError" ? "请求超时，请重试" : "服务不可达，请检查网络");
    } finally {
      clearTimeout(timer);
    }
    if (resp.status === 204) return null;
    if (!resp.ok) {
      let code = "http_" + resp.status, message = `请求失败（HTTP ${resp.status}）`;
      try {
        const body = await resp.json();
        const d = body.detail ?? body;
        if (typeof d === "object") { code = d.code || code; message = d.message || message; }
        else if (typeof d === "string") { message = d; }
      } catch (_) { /* 非 JSON 错误体 */ }
      throw new ApiError(resp.status, code, message);
    }
    return resp.json();
  }

  async function refreshSession() {
    const refresh = getRefreshToken();
    if (!refresh) throw new ApiError(401, "denied", "无刷新凭据");
    if (!refreshInFlight) {
      refreshInFlight = (async () => {
        const headers = { "Content-Type": "application/json" };
        const resp = await fetch("/v1/auth/refresh", {
          method: "POST", headers,
          body: JSON.stringify({ refresh_token: refresh }),
        });
        if (!resp.ok) {
          clearAuth();
          throw new ApiError(401, "denied", "登录已过期，请重新登录");
        }
        const body = await resp.json();
        storeTokens(body.access_token, body.refresh_token,
          localStorage.getItem(REFRESH_PERSIST_KEY) !== null);
        return body;
      })().finally(() => { refreshInFlight = null; });
    }
    return refreshInFlight;
  }

  async function request(path, options = {}) {
    if (!getAccessToken() && getRefreshToken() && !path.startsWith("/v1/auth/")) {
      try { await refreshSession(); } catch (_) { /* 落到正常 401 流程 */ }
    }
    try {
      return await rawFetch(path, options);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401 && getRefreshToken()
          && !path.startsWith("/v1/auth/")) {
        await refreshSession(); // 失败会 clearAuth 并抛 401
        return rawFetch(path, options);
      }
      throw e;
    }
  }

  return {
    request,

    // ---- 登录 ----
    async login(password, remember) {
      const headers = { "Content-Type": "application/json" };
      const resp = await fetch("/v1/auth/login", {
        method: "POST", headers,
        body: JSON.stringify({ password, remember }),
      });
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        throw new ApiError(resp.status, body.detail?.code || "denied",
          body.detail?.message || `登录失败（HTTP ${resp.status}）`);
      }
      storeTokens(body.access_token, body.refresh_token, remember, "admin");
      return body;
    },
    async logout() {
      if (getStaticToken() === "sso-cookie") {
        await fetch("/v1/auth/browser-logout", { method: "POST" }).catch(() => {});
      }
      const refresh = getRefreshToken();
      if (refresh) {
        try {
          await fetch("/v1/auth/logout", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ refresh_token: refresh }),
          });
        } catch (_) { /* 本地清理为准 */ }
      }
      clearAuth();
    },
    async authSetup() {
      try {
        return await fetch("/v1/auth/setup", { timeoutMs: 8000 })
          .then((r) => (r.ok ? r.json() : {}));
      } catch (_) { return {}; }
    },
    async changePassword(currentPassword, newPassword) {
      return request("/v1/auth/password", {
        method: "POST",
        body: { current_password: currentPassword, new_password: newPassword },
      });
    },
    hasSession() {
      return Boolean(getAccessToken() || getRefreshToken() || getStaticToken());
    },

    // ---- 任务 ----
    listRuns: (filters = {}) => {
      const qs = new URLSearchParams();
      if (filters.state) qs.set("state", filters.state);
      if (filters.q) qs.set("q", filters.q);
      const suffix = qs.toString() ? `?${qs}` : "";
      return request(`/v1/runs${suffix}`);
    },
    getTask: (id) => request(`/v1/tasks/${encodeURIComponent(id)}`),
    getEvents: (id, cursor = 0, limit = 200) =>
      request(`/v1/tasks/${encodeURIComponent(id)}/events?cursor=${cursor}&limit=${limit}`),
    getTrace: (id, cursor = 0) =>
      request(`/v1/tasks/${encodeURIComponent(id)}/trace?cursor=${cursor}&limit=200`),
    workflowRequest: (path, body) => request(path, body === undefined ? {} : {method: "POST", body}),
    getCapabilities: () => request("/v1/capabilities"),
    submitTask: (payload) => request("/v1/tasks", { method: "POST", body: payload }),
    supplementTask: (id, body) => request(`/v1/tasks/${encodeURIComponent(id)}/inputs`, { method: "POST", body }),
    followupTask: (id, body) => request(`/v1/tasks/${encodeURIComponent(id)}/followups`, { method: "POST", body }),
    resolveTaskReference: (id, source, locator) => request(`/v1/tasks/${encodeURIComponent(id)}/references/resolve`, {
      method: "POST", body: { source_id: source, locator } }),
    cancelTask: (id) => request(`/v1/tasks/${encodeURIComponent(id)}/cancel`, { method: "POST", body: {} }),
    resumeTask: (id) => request(`/v1/tasks/${encodeURIComponent(id)}/resume`, { method: "POST", body: {} }),
    reconcileTask: (id, stepId) => request(`/v1/tasks/${encodeURIComponent(id)}/reconcile`, {
      method: "POST", body: { step_id: stepId } }),

    // ---- 审批 ----
    listApprovals: () => request("/v1/approvals"),
    decideApproval: (id, decision) =>
      request(`/v1/approvals/${encodeURIComponent(id)}/decision`, { method: "POST", body: { decision } }),

    // ---- 素材（N2a：文本上传/粘贴 → 稳定素材 ID）----
    uploadMaterial: (name, text) =>
      request("/v1/materials", { method: "POST", body: { name, content_text: text } }),

    // ---- 成果 ----
    listAssets: () => request("/v1/assets"),
    getAsset: (id) => request(`/v1/assets/${encodeURIComponent(id)}`),
    fetchNodeAsset: (node, body) => request(`/v1/nodes/${encodeURIComponent(node)}/assets/fetch`, { method: "POST", body }),
    /** 鉴权下载为 Blob（不用 token query）；调用方负责 revokeObjectURL。 */
    downloadAsset: async (id) => {
      const resp = await fetch(`/v1/assets/${encodeURIComponent(id)}/download`, {
        headers: { Authorization: `Bearer ${getToken()}` },
      });
      if (!resp.ok) {
        let message = `下载失败（HTTP ${resp.status}）`;
        try { const b = await resp.json(); message = b.detail?.message || message; } catch (_) {}
        throw new ApiError(resp.status, "download_failed", message);
      }
      return resp.blob();
    },

    // ---- 能力（只读健康）----
    listSkills: (all = false) => request(`/v1/skills${all ? "?include_disabled=true" : ""}`),
    listMcp: () => request("/v1/mcp/servers"),
    listNodes: () => request("/v1/nodes"),
    knowledgeCapabilities: () => request("/v1/knowledge/capabilities"),
    /** 能力中心的管理动作（服务端一律 admin 核权；游客/旧 UI 直调仍拒）。 */
    installSkill: (content_text) => request("/v1/skills", { method: "POST", body: { content_text } }),
    knowledgeIngest: (body) => request("/v1/knowledge/ingest", { method: "POST", body }),
    knowledgeSearch: (query) => request(`/v1/knowledge/search?q=${encodeURIComponent(query)}`),
    knowledgeResolve: (body) => request("/v1/knowledge/resolve", { method: "POST", body }),
    knowledgeDelete: (id) => request(`/v1/knowledge/sources/${encodeURIComponent(id)}`, { method: "DELETE" }),
    knowledgeSelect: (backend) => request("/v1/knowledge/backend", { method: "POST", body: { backend } }),
    registerMcp: (body) => request("/v1/mcp/servers", { method: "POST", body }),
    probeMcp: (id) => request(`/v1/mcp/servers/${encodeURIComponent(id)}/probe`,
      { method: "POST", body: {} }),
    toggleMcp: (id, enabled) => request(`/v1/mcp/servers/${encodeURIComponent(id)}/enabled`,
      { method: "POST", body: { enabled } }),
    toggleSkill: (id, version, enabled) =>
      request(`/v1/skills/${encodeURIComponent(id)}/enabled`,
        { method: "POST", body: { version, enabled } }),

    // ---- U4：访问角色 ----
    /** 当前会话的访问角色（admin/guest）；服务端裁定，前端不自报。 */
    async authMe() {
      const body = await request("/v1/auth/me");
      sessionStorage.setItem(SESSION_KIND_KEY, body.role || "guest");
      return body;
    },
    /**
     * U5-01：游客是默认角色——免登换取只读会话，不要求访问码。
     * 服务端裁定：未开放 403、尚未发布 409、名额满 503；会话只进 sessionStorage。
     */
    async guestBootstrap() {
      const resp = await fetch("/v1/auth/guest/bootstrap", { method: "POST" });
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        const d = body.detail ?? body;
        throw new ApiError(resp.status, d.code || "denied", d.message || "演示暂时不可进入");
      }
      sessionStorage.removeItem("agentos_guest_expired");
      storeTokens(body.access_token, body.refresh_token, false, "guest");
      return body;
    },
    /** 可选定向邀请（默认入口不需要）：一次性码 → 专用游客会话。 */
    async guestRedeem(code) {
      const headers = { "Content-Type": "application/json" };
      const resp = await fetch("/v1/auth/guest/redeem", {
        method: "POST", headers, body: JSON.stringify({ code: code.trim() }),
      });
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        throw new ApiError(resp.status, body.detail?.code || "denied",
          body.detail?.message || `兑换失败（HTTP ${resp.status}）`);
      }
      // 游客会话只在当前标签页：不写入 localStorage 的长期 refresh
      sessionStorage.removeItem("agentos_guest_expired");
      storeTokens(body.access_token, body.refresh_token, false, "guest");
      return body;
    },

    /** U4-04：任务事件流（鉴权 fetch + SSE）。返回中止函数；事件按到达顺序回调。 */
    streamTask(runId, opts = {}) {
      return streamSse(`/v1/tasks/${encodeURIComponent(runId)}/stream`, { ...opts, refresh: refreshSession });
    },
    /** U5：游客授权投影流（走 /v1/demo/*，与管理员原始流分开）。 */
    streamDemoTask(runId, opts = {}) {
      return streamSse(`/v1/demo/runs/${encodeURIComponent(runId)}/stream`, { ...opts, refresh: refreshSession });
    },

    // ---- U4：管理员演示访问管理 ----
    listGrants: (kind = "") => request(`/v1/demo/grants${kind ? `?kind=${kind}` : ""}`),
    /** U5-01：默认演示通道（免登游客可见的那一份清单）。 */
    publicChannel: () => request("/v1/demo/public"),
    publicOpen: () => request("/v1/demo/public/open", { method: "POST", body: {} }),
    publicClose: () => request("/v1/demo/public/close", { method: "POST", body: {} }),
    publishDefault: (runs, assets, refs = [], note = "") =>
      request("/v1/demo/grants/grant_public/publish", {
        method: "POST", body: { runs, assets, refs, note } }),
    createGrant: (label, ttlHours) => request("/v1/demo/grants", {
      method: "POST", body: { label, ttl_hours: ttlHours } }),
    publishManifest: (grantId, runs, assets, note = "") =>
      request(`/v1/demo/grants/${encodeURIComponent(grantId)}/publish`, {
        method: "POST", body: { runs, assets, note } }),
    revokeGrant: (grantId) => request(`/v1/demo/grants/${encodeURIComponent(grantId)}/revoke`, {
      method: "POST", body: {} }),
    previewDemo: (runs, assets) => request("/v1/demo/preview", {
      method: "POST", body: { runs, assets } }),
    voidAllDemo: (reason = "backup_restore") => request("/v1/demo/void-all", {
      method: "POST", body: { reason } }),

    // ---- U4：游客只读投影 ----
    demoHome: () => request("/v1/demo/home"),
    demoRun: (id) => request(`/v1/demo/runs/${encodeURIComponent(id)}`),
    demoEvents: (id, cursor = 0, limit = 200) =>
      request(`/v1/demo/runs/${encodeURIComponent(id)}/events?cursor=${cursor}&limit=${limit}`),
    demoAsset: (id) => request(`/v1/demo/assets/${encodeURIComponent(id)}`),
    /** 游客预览：走 preview 判据的只读字节（能预览不等于能下载）。 */
    demoPreview: async (id) => {
      const resp = await fetch(`/v1/demo/assets/${encodeURIComponent(id)}/preview`, {
        headers: { Authorization: `Bearer ${getToken()}` },
      });
      if (!resp.ok) {
        let message = "该资源未开放预览";
        try { const b = await resp.json(); if (resp.status !== 404) message = b.detail?.message || message; }
        catch (_) { /* 统一 404 不泄露存在性 */ }
        throw new ApiError(resp.status, "preview_failed", message);
      }
      return { blob: await resp.blob(), sha256: resp.headers.get("X-Asset-Sha256") || "" };
    },
    /** 游客下载：鉴权 fetch → Blob（不复用 owner 签名 URL；令牌不进 URL）。 */
    demoDownload: async (id) => {
      const resp = await fetch(`/v1/demo/assets/${encodeURIComponent(id)}/download`, {
        headers: { Authorization: `Bearer ${getToken()}` },
      });
      if (!resp.ok) {
        let message = "该资源未开放下载";
        try { const b = await resp.json(); if (resp.status !== 404) message = b.detail?.message || message; }
        catch (_) { /* 统一 404 不泄露存在性 */ }
        throw new ApiError(resp.status, "download_failed", message);
      }
      return { blob: await resp.blob(), sha256: resp.headers.get("X-Asset-Sha256") || "" };
    },
  };
}

/** 鉴权 SSE 读取：同一个游标语时可用于管理员原始流与游客投影流。 */
function streamSse(base, { cursor = 0, onEvent, onError, follow = 1, refresh } = {}) {
  const ctrl = new AbortController();
  let retryTimer;
  const delay = ms => new Promise(resolve => {
    const done = () => { clearTimeout(retryTimer); ctrl.signal.removeEventListener("abort", done); resolve(); };
    retryTimer = setTimeout(done, ms);
    ctrl.signal.addEventListener("abort", done, {once:true});
  });
  (async () => {
    let failures = 0;
    while (!ctrl.signal.aborted) {
      try {
        let resp = await fetch(`${base}?cursor=${cursor}&follow=${follow}`, {
          headers: { Authorization: `Bearer ${getToken()}` }, signal: ctrl.signal,
        });
        if (resp.status === 401 && getRefreshToken() && refresh) {
          await refresh();
          resp = await fetch(`${base}?cursor=${cursor}&follow=${follow}`, {
            headers: { Authorization: `Bearer ${getToken()}` }, signal: ctrl.signal,
          });
        }
        if (!resp.ok || !resp.body) throw new ApiError(resp.status, "stream_failed", `流式连接失败（HTTP ${resp.status}）`);
        const reader = resp.body.getReader(), decoder = new TextDecoder();
        let buffer = "";
        try {
          for (;;) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer = (buffer + decoder.decode(value, { stream: true })).replace(/\r\n/g, "\n");
            const frames = buffer.split("\n\n"); buffer = frames.pop() || "";
            for (const frame of frames) {
              const lines = frame.split("\n").filter(l => l.startsWith("data:"));
              if (!lines.length) continue;
              let event;
              try { event = JSON.parse(lines.map(l => l.slice(5).trimStart()).join("\n")); } catch { continue; }
              onEvent?.(event);
              if (Number.isInteger(event.seq)) cursor = Math.max(cursor, event.seq);
              failures = 0;
              if (event.kind === "stream.end" || event.kind === "access.revoked") return;
            }
          }
        } finally { await reader.cancel().catch(() => {}); }
        if (!follow) return;
      } catch (err) {
        if (ctrl.signal.aborted) return;
        onError?.(err);
        if ([401, 403, 404].includes(err.status)) return;
      }
      failures++;
      await delay(Math.min(1000 * 2 ** Math.min(failures - 1, 5), 30000));
    }
  })();
  return () => { ctrl.abort(); clearTimeout(retryTimer); };
}

/** 游客会话专属清理：撤权/退出时释放 Blob 与视图缓存（不可收回已交给浏览器的字节）。 */
export function clearGuestResources() {
  document.querySelectorAll("img[data-blob-url], a[data-blob-url]").forEach((node) => {
    const url = node.dataset.blobUrl;
    if (url && url.startsWith("blob:")) URL.revokeObjectURL(url);
  });
}
