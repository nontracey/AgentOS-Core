/* AgentOS 控制台：任务化（非会话化）工作台 —— 提交/审批/查看全局状态。
 *
 * 安全约定（步骤 1 修复）：
 * - 令牌只存 sessionStorage：标签页关闭即清除，不落盘、不跨标签页共享。
 * - 连接判定用带鉴权的 /v1/runs；无效令牌绝不显示“已连接”。
 * - 所有动态文本经 esc() 转义后进 innerHTML；进 JS 字符串上下文的 id 经 jsq()
 *   （esc/jsq 定义在 safe.js，先于本文件加载）。
 */
"use strict";

const $ = (id) => document.getElementById(id);
let token = sessionStorage.getItem("agentos_token") || "";
let selectedRun = null;
let refreshTimer = null;

const api = (path, options = {}) => {
  const opts = { headers: { Authorization: `Bearer ${token}` }, ...options };
  opts.headers = { ...{ "Content-Type": "application/json" }, ...opts.headers };
  return fetch(path, opts).then(async (r) => {
    if (r.status === 401 || r.status === 403) {
      setConn("token-invalid");
      throw new Error("令牌无效或已过期");
    }
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      throw new Error(body.detail?.message || body.detail?.reason || `HTTP ${r.status}`);
    }
    return r.json();
  });
};

function setConn(kind, detail = "") {
  const el = $("conn");
  el.classList.remove("on");
  el.classList.toggle("bad", kind !== "ok");
  if (kind === "ok") { el.textContent = "已连接"; el.classList.add("on"); }
  else if (kind === "token-invalid") el.textContent = "令牌无效——请重新输入";
  else if (kind === "down") el.textContent = "服务不可达";
  else el.textContent = "连接失败：" + detail;
}

function saveToken() {
  token = $("token").value.trim();
  sessionStorage.setItem("agentos_token", token);
  connect();
}

/* 连接探测：必须经过鉴权（/healthz 无鉴权，不能作为已连接依据） */
async function connect() {
  if (!token) { setConn("down", "未配置令牌"); return; }
  try {
    await api("/v1/runs");
    setConn("ok");
    if (!refreshTimer) refreshTimer = setInterval(refreshAll, 3000);
    refreshAll();
  } catch (e) {
    if (!$("conn").classList.contains("bad")) setConn("down", e.message);
  }
}

const st = (s) => `<span class="state ${esc(s)}">${esc(s)}</span>`;

async function refreshAll() {
  try {
    await Promise.all([loadOverview(), loadRuns(), loadApprovals(), loadSkills(), loadMcp(),
                       loadNodes(), loadAssets()]);
  } catch (e) {
    setConn("down", e.message);
  }
}

async function loadOverview() {
  const runs = await api("/v1/runs");
  const approvals = await api("/v1/approvals");
  $("apCount").textContent = approvals.length || "";
  const count = {};
  runs.forEach((r) => { count[r.state] = (count[r.state] || 0) + 1; });
  const cost = runs.reduce((s, r) => s + (r.cost?.settled || 0), 0);
  $("cards").innerHTML = [
    ["运行总数", runs.length], ["成功", count.succeeded || 0], ["失败", count.failed || 0],
    ["待审批", count.awaiting_approval || 0], ["运行中", count.running || 0],
    ["累计成本(μ$)", cost],
  ].map(([label, v]) => `<div class="card"><b>${esc(v)}</b><span>${esc(label)}</span></div>`).join("");
}

async function loadRuns() {
  const runs = await api("/v1/runs");
  $("runList").innerHTML = runs
    .map((r) => `<li onclick="showRun('${jsq(r.id)}')">
      <span>${esc((r.goal || r.id).slice(0, 26))}<br><small>${esc(r.id)}</small></span>${st(r.state)}
    </li>`)
    .join("");
  if (selectedRun) await showRun(selectedRun, true);
}

async function showRun(id, silent = false) {
  selectedRun = id;
  try {
    const d = await api(`/v1/tasks/${encodeURIComponent(id)}`);
    const run = d.run;
    let delivery = "";
    let timeline = "";
    try {
      const events = await api(`/v1/tasks/${encodeURIComponent(id)}/events?limit=500`);
      const last = [...events.events].reverse().find((e) => e.payload?.delivery);
      if (last) delivery = `<h3>交付</h3><pre>${esc(JSON.stringify(last.payload.delivery, null, 2))}</pre>`;
      // 模型与降级时间线：回答"用的什么模型、有没有降级"（禁静默降级）
      const lines = [];
      let model = null;
      for (const e of events.events) {
        if (e.kind === "model.usage" && e.payload?.model) {
          model = e.payload.model;
          lines.push(`<div class="row">🧠 模型 <b>${esc(e.payload.model)}</b> `
            + `tokens ${esc(e.payload.input_tokens)}/${esc(e.payload.output_tokens)}</div>`);
        } else if (e.kind === "model.fallback") {
          lines.push(`<div class="row warn">⚠️ 模型降级: ${esc(e.payload?.from)} → `
            + `${esc(e.payload?.to)}（${esc(e.payload?.reason)}）</div>`);
        } else if (e.kind === "step.state_changed" && e.payload?.to === "awaiting_approval") {
          lines.push(`<div class="row">⏸ 步骤 ${esc(e.payload?.step_id)} 等待审批</div>`);
        }
      }
      if (lines.length) {
        timeline = `<h3>模型与时间线</h3>${lines.join("")}`;
        if (model) {
          const tag = document.getElementById("modelTag");
          if (tag) tag.textContent = "模型: " + model;
        }
      }
    } catch (_) {}
    $("runDetail").innerHTML = `
      <h3>${esc(run.goal_text || run.id)} ${st(run.state)}</h3>
      <div class="muted">run: ${esc(run.id)} ｜ 成本: ${esc(JSON.stringify(d.budget))}</div>
      ${run.error_json ? `<pre>${esc(run.error_json)}</pre>` : ""}
      ${timeline}
      <h3>步骤</h3>
      ${d.steps.map((s) => `<div class="row">${st(s.state)} <code>${esc(s.id)}</code>
        <span class="muted">${esc(s.tool_name || "")}</span></div>`).join("") || "<i>无</i>"}
      ${delivery}
      <div style="margin-top:10px" class="row">
        <button class="primary" onclick="cancelRun('${jsq(id)}')">请求取消</button>
      </div>`;
  } catch (e) {
    if (!silent) $("runDetail").innerHTML = `<i>${esc(e.message)}</i>`;
  }
}

async function loadApprovals() {
  const list = await api("/v1/approvals");
  $("approvals").innerHTML = list.length
    ? list.map((a) => `<div class="panel row">
        <div><code>${esc(a.id)}</code><br><small>${esc(a.summary)} ｜ 主体 ${esc(a.subject)} ｜ 到期 ${esc(a.expires_at)}</small></div>
        <button class="primary" onclick="decide('${jsq(a.id)}','approve')">批准</button>
        <button onclick="decide('${jsq(a.id)}','reject')">拒绝</button>
      </div>`).join("")
    : "<i class='muted'>没有待审批项</i>";
}

async function decide(id, decision) {
  await api(`/v1/approvals/${encodeURIComponent(id)}/decision`, { method: "POST", body: JSON.stringify({ decision }) });
  refreshAll();
}

async function cancelRun(id) {
  await api(`/v1/tasks/${encodeURIComponent(id)}/cancel`, { method: "POST", body: "{}" });
  refreshAll();
}

async function loadSkills() {
  const skills = await api("/v1/skills").catch(() => []);
  $("skills").innerHTML = `<h3>技能 (SkillHub)</h3>` + (skills.length
    ? skills.map((s) => `<div class="row"><b>${esc(s.id)}</b> v${esc(s.version)}
        <span class="muted">${esc(s.description)}</span></div>`).join("")
    : "<i class='muted'>未安装技能（agentos skill install &lt;目录&gt;）</i>");
}

async function loadMcp() {
  const servers = await api("/v1/mcp/servers").catch(() => []);
  $("mcp").innerHTML = `<h3>MCP Servers</h3>` + (servers.length
    ? servers.map((s) => `<div class="row"><b>${esc(s.id)}</b> ${st(s.enabled ? "enabled" : "disabled")}
        <span class="muted">${esc(s.transport)} ｜ 工具 ${(s.tools || []).length} 个</span></div>`).join("")
    : "<i class='muted'>未注册（agentos mcp add …）</i>");
}

async function loadNodes() {
  const nodes = await api("/v1/nodes").catch(() => []);
  $("nodes").innerHTML = `<h3>计算节点（配对/心跳/能力）</h3>` + (nodes.length
    ? nodes.map((n) => `<div class="panel row">
        <div><b>${esc(n.node_id)}</b> <span class="state ${esc(n.status)}">${esc(n.status)}</span><br>
        <small>${esc(n.endpoint)} ｜ ${esc(n.os)}/${esc(n.arch)}</small><br>
        <small>心跳: ${esc(n.last_heartbeat || "—")} ｜ 能力: ${esc(String((n.capabilities_json || "[]").length))}B 快照</small></div>
        ${n.status === "paired"
          ? `<button onclick="revokeNode('${jsq(n.node_id)}')">撤销</button>`
          : `<button onclick="pairAgain('${jsq(n.node_id)}', '${jsq(n.endpoint)}')">重新配对</button>`}
      </div>`).join("")
    : "<i class='muted'>无节点——用 CLI: agentos nodes pair &lt;id&gt; &lt;endpoint&gt;</i>");
}

async function revokeNode(id) {
  if (!confirm(`撤销节点 ${id}？其令牌立即失效`)) return;
  await api(`/v1/nodes/${encodeURIComponent(id)}/revoke`, { method: "POST", body: "{}" });
  refreshAll();
}

async function pairAgain(id, endpoint) {
  const t = prompt(`输入 ${id} 的节点令牌（pair 时生成）`);
  if (!t) return;
  await api("/v1/nodes/pair", { method: "POST",
    body: JSON.stringify({ node_id: id, endpoint, token: t }) });
  refreshAll();
}

async function assetSignedUrl(id) {
  // N2a：主令牌不再进 URL；先换资源范围受限的短期签名 URL（10 分钟、单资源）
  const r = await api(`/v1/assets/${encodeURIComponent(id)}/signed-url`, { method: "POST", body: "{}" });
  return r.url;
}

async function loadAssets() {
  const assets = await api("/v1/assets").catch(() => []);
  const rows = await Promise.all(assets.map(async (a) => {
    const url = await assetSignedUrl(a.id).catch(() => "#");
    return `<div class="panel row">
        <div><code>${esc(a.id)}</code><br>
        <small>${esc(a.mime)} · ${esc(String(a.size))}B · sha256 ${esc(String(a.sha256).slice(0, 16))}… · 来源 ${esc(a.source_node || "local")}</small></div>
        ${String(a.mime).startsWith("image/")
          ? `<img class="thumb" src="${url}" alt="${esc(a.id)}">` : ""}
        <a class="primary" href="${url}"
           download="${esc(a.id)}">下载</a>
      </div>`;
  }));
  $("assets").innerHTML = `<h3>资产库（远程产物登记，hash 可校验）</h3>` +
    (rows.length ? rows.join("") : "<i class='muted'>暂无资产</i>");
}

/* ---------- 表单 ---------- */
$("submitForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const payload = { goal: $("goal").value };
  const planRaw = $("planJson").value.trim();
  if (planRaw) {
    const plan = JSON.parse(planRaw);
    payload.plan = plan;
  }
  payload.idempotency_key = "ui_" + crypto.randomUUID();
  try {
    const r = await api("/v1/tasks", { method: "POST", body: JSON.stringify(payload) });
    alert(`已受理：${r.run_id}`);
    $("goal").value = ""; $("planJson").value = "";
    refreshAll();
  } catch (err) {
    alert(`提交失败：${err.message}`);
  }
});

$("wfForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const tasks = JSON.parse($("wfTasks").value);
    const r = await api(`/v1/workflows/${encodeURIComponent($("wfId").value)}/publish`, {
      method: "POST", body: JSON.stringify({ name: $("wfName").value || $("wfId").value, tasks }),
    });
    $("wfResult").innerHTML = `<pre>发布成功：v${esc(r.version)} hash=${esc(r.hash.slice(0, 16))}…${r.reused ? "（复用既有版本）" : ""}</pre>`;
  } catch (err) {
    $("wfResult").innerHTML = `<pre>发布失败：${esc(err.message)}</pre>`;
  }
});

$("wfStart").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const r = await api(`/v1/workflows/${encodeURIComponent($("wfStartId").value)}/start`, {
      method: "POST",
      body: JSON.stringify({ version: Number($("wfVersion").value), inputs: {}, idempotency_key: "ui_" + crypto.randomUUID() }),
    });
    $("wfResult").innerHTML = `<pre>已启动：${esc(r.wf_run_id)}<br>内部 run：${esc(r.internal_run_id)}</pre>`;
    refreshAll();
  } catch (err) {
    $("wfResult").innerHTML = `<pre>启动失败：${esc(err.message)}</pre>`;
  }
});

/* ---------- 标签页 ---------- */
document.querySelectorAll("nav button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll("nav button").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    $(`tab-${b.dataset.tab}`).classList.add("active");
    refreshAll();
  })
);

/* 退出：清空本页令牌 */
$("logout").addEventListener("click", () => {
  sessionStorage.removeItem("agentos_token");
  token = "";
  $("token").value = "";
  setConn("down", "已退出");
});

if (token) { $("token").value = token; connect(); }
else setConn("down", "未配置令牌");
