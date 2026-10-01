/* U5-05 能力中心：MCP 工具 / Skills / 知识后端 / 执行设备 四类边界。
 * 数据全部来自服务端；配置声明、探针结果与实际调用证据三者分开表述，
 * 不把"已配置"说成"已连通"，也不给未实现的操作伪装成按钮。 */
"use strict";

import { esc } from "../safe-export.js";
import { emptyState, icon, modelZh, timeShort, toast } from "../ui.js";

const TABS = [
  ["all", "全部"], ["mcp", "MCP 工具"], ["skill", "Skills"],
  ["knowledge", "知识后端"], ["nodes", "执行设备"],
];

export async function renderHub(ctx) {
  const { el, api } = ctx;
  let stopped = false, tab = "all", snapshot = null, admin = false;
  const dialogs = new Set();

  el.innerHTML = `
    <section class="page">
      <div class="page-heading">
        <div><h2>能力中心</h2><p>连接工具、加载技能，为任务提供合适的能力。</p></div>
        <button class="btn primary" id="hub-refresh">${icon("refresh", 15)} 重新读取</button>
      </div>
      <div class="tabs" role="tablist" aria-label="能力分类">
        ${TABS.map(([key, name]) => `<button data-hub="${key}" class="${key === tab ? "on" : ""}"
          role="tab">${name}</button>`).join("")}
      </div>
      <div id="hub-body"><div class="skeleton" style="height:160px"></div></div>
      <p class="side-caption">安装或连接能力不自动授予执行权限；每次调用仍由服务端策略、审批与预算裁定。
        知识后端切换只影响新任务，已有任务固定其来源与版本。</p>
    </section>`;

  el.querySelectorAll("[data-hub]").forEach((btn) => btn.addEventListener("click", () => {
    tab = btn.dataset.hub;
    el.querySelectorAll("[data-hub]").forEach((b) => b.classList.toggle("on", b === btn));
    draw();
  }));
  el.querySelector("#hub-refresh").addEventListener("click", () => load().catch(() => {}));

  async function load() {
    if (stopped) return false;
    const me = await api.authMe();
    admin = me.role === "admin";
    const [capabilities, servers, skills, nodes, knowledge] = await Promise.all([
      api.getCapabilities(),
      api.listMcp(), api.listSkills(admin), api.listNodes(), api.knowledgeCapabilities(),
    ]);
    if (stopped) return false;
    snapshot = { capabilities, servers, skills, nodes, knowledge };
    draw();
    return true;
  }

  function card({ glyph, title, status, tone, desc, foot, actions = "" }) {
    return `<article class="cap-card">
      <div class="cap-top"><span class="cap-icon">${icon(glyph, 18)}</span>
        <h3>${esc(title)}</h3><span class="badge ${esc(tone || "")}">${esc(status)}</span></div>
      <p>${desc}</p>
      <div class="cap-foot"><span class="muted small">${esc(foot)}</span><div class="cap-actions">${actions}</div></div>
    </article>`;
  }

  function draw() {
    if (!snapshot) return;
    const { capabilities, servers, skills, nodes, knowledge } = snapshot;
    const cards = [];
    const actions = admin ? `<div class="hub-manage-actions">
      ${tab === "all" || tab === "mcp" ? '<button class="btn" data-hub-add="mcp">连接 MCP</button>' : ""}
      ${tab === "all" || tab === "skill" ? '<button class="btn" data-hub-add="skill">安装 Skill</button>' : ""}
      ${tab === "all" || tab === "knowledge" ? '<button class="btn" data-hub-add="ingest">添加知识</button><button class="btn" data-hub-add="search">检索知识</button>' : ""}
    </div>` : "";

    if (tab === "all" || tab === "mcp") {
      for (const s of servers) {
        const tools = (s.tools || []).map((t) => t.name || t).slice(0, 6);
        cards.push(card({
          glyph: "plug", title: s.name || s.id,
          status: s.enabled ? "已启用" : "已停用", tone: s.enabled ? "good" : "",
          desc: `协议接入的互操作工具入口。已发现工具 ${
            (s.tools || []).length ? (s.tools || []).length : "未记录"} 个${
            tools.length ? `：${esc(tools.join("、"))}` : ""}。`,
          foot: `传输 ${esc(s.transport || "streamable_http")} · 端点 ${esc(s.url || (s.command_present ? "本机命令" : "未记录"))}`,
          actions: admin ? `<button class="link-btn" data-probe="${esc(s.id)}">重新探针</button>
            <button class="link-btn" data-toggle-mcp="${esc(s.id)}"
              data-enabled="${s.enabled ? "0" : "1"}">${s.enabled ? "停用" : "启用"}</button>` : "",
        }));
      }
      if (!servers.length && tab !== "all") {
        cards.push(emptyState(icon("plug", 24), "还没有 MCP 服务",
          "可在服务端配置接入；接入后这里显示已发现工具与授权范围。"));
      }
    }

    if (tab === "all" || tab === "skill") {
      for (const s of skills) {
        cards.push(card({
          glyph: "doc", title: s.id, status: `v${s.version || "未记录"} · ${s.enabled === false ? "已停用" : "已启用"}`,
          desc: esc(s.description || "该 Skill 没有描述。") +
            ` 允许工具：${esc((s.allowed_tools || []).join("、") || "未声明")}。`,
          foot: `版本固定 · 摘要 ${String(s.digest || "").slice(0, 12) || "未记录"} · 正文按需加载`,
          actions: `<button class="link-btn" data-skill-body="${esc(s.id)}"
            data-version="${esc(s.version || "")}">查看声明范围</button>${admin ? `<button class="link-btn" data-toggle-skill="${esc(s.id)}"
            data-version="${esc(s.version)}" data-enabled="${s.enabled === false ? "1" : "0"}">${s.enabled === false ? "启用" : "停用"}</button>` : ""}`,
        }));
      }
      if (!skills.length && tab !== "all") {
        cards.push(emptyState(icon("doc", 24), "没有已启用的 Skill",
          "Skill 需要显式安装并固定版本；安装本身不授予工具执行权限。"));
      }
    }

    if (tab === "all" || tab === "knowledge") {
      const backend = capabilities.knowledge_backend || knowledge.backend || "builtin";
      cards.push(card({
        glyph: "files", title: `${backend === "weknora" ? "WeKnora" : "内置检索"}（当前）`,
        status: "配置已生效", tone: "active",
        desc: `统一 KnowledgePort 提供 ingest / search / resolve / delete 语义。`
          + `已声明能力：${esc(Object.keys(knowledge).filter(k => knowledge[k] === true).join("、") || "未记录")}。`,
        foot: `版本 ${knowledge.version || "未记录"} · 新任务生效，旧任务保持绑定`,
        actions: admin ? (knowledge.choices || []).filter(b => b.backend !== backend).map(b =>
          `<button class="link-btn" data-backend="${esc(b.backend)}">切换到 ${esc(b.backend)}</button>`).join("") : "",
      }));
      cards.push(card({
        glyph: "shield", title: "游客读取范围", status: "只读投影",
        desc: "游客只能读取管理员在演示清单中显式发布的引用片段，"
          + "不能发起实时检索，也不能由引用跳到整库或原文。",
        foot: "服务端逐资源裁定，前端不判定权限",
      }));
    }

    if (tab === "all" || tab === "nodes") {
      for (const n of nodes) {
        const paired = n.status === "paired";
        cards.push(card({
          glyph: "node", title: n.node_id || n.id,
          status: paired ? "已配对" : esc(n.status || "未配对"), tone: paired ? "good" : "",
          desc: `${esc(n.os || "系统未记录")} · ${esc(n.arch || "架构未记录")} · `
            + `提供 ${esc((n.tools || []).slice(0, 6).join("、") || "未记录工具")}。`,
          foot: `最近心跳 ${timeShort(n.last_heartbeat) || "无"} · 实际派发在任务详情中核对`,
        }));
      }
      if (!nodes.length && tab !== "all") {
        cards.push(emptyState(icon("node", 24), "没有已配对的执行设备",
          "默认单机运行即可完成任务；确需远端能力时再配对一个节点。"));
      }
    }

    if (tab === "all" || tab === "mcp") {
      const model = capabilities.model || {};
      cards.push(card({
        glyph: "sparkles", title: `模型 · ${modelZh(model.name)}`,
        status: model.provider === "scripted" ? "脚本验证环境" : "已配置",
        tone: model.provider === "scripted" ? "" : "active",
        desc: `提供方 ${esc(model.provider || "未记录")}。`
          + (capabilities.fallback?.configured
            ? `已配置兜底端点 ${esc(modelZh(capabilities.fallback.name))}。`
            : "未配置兜底端点。")
          + " 健康状态未做在线探针，以真实任务调用为准。",
        foot: `模板：${esc(capabilities.template?.id || "未记录")} v${esc(capabilities.template?.version || "—")}`,
      }));
    }

    el.querySelector("#hub-body").innerHTML = `${actions}<div class="cap-grid">${cards.join("")}</div>`;

    el.querySelectorAll("[data-hub-add]").forEach(btn => btn.addEventListener("click", () => openManage(btn.dataset.hubAdd)));
    el.querySelectorAll("[data-backend]").forEach(btn => btn.addEventListener("click", async () => {
      btn.disabled = true;
      try { await api.knowledgeSelect(btn.dataset.backend); toast("新任务将使用选定后端", "ok"); await load(); }
      catch (e) { toast(e.message, "err"); btn.disabled = false; }
    }));
    el.querySelectorAll("[data-toggle-skill]").forEach(btn => btn.addEventListener("click", async () => {
      try { await api.toggleSkill(btn.dataset.toggleSkill, btn.dataset.version, btn.dataset.enabled === "1"); await load(); }
      catch (e) { toast(e.message, "err"); }
    }));
    el.querySelectorAll("[data-toggle-mcp]").forEach((btn) => btn.addEventListener("click", async () => {
      try {
        await api.toggleMcp(btn.dataset.toggleMcp, btn.dataset.enabled === "1");
        toast(btn.dataset.enabled === "1" ? "已启用" : "已停用", "ok");
        await load();
      } catch (e) { toast(e.isForbidden ? "权限不足" : e.message, "err"); }
    }));
    el.querySelectorAll("[data-probe]").forEach((btn) => btn.addEventListener("click", async () => {
      try {
        const out = await api.probeMcp(btn.dataset.probe);
        toast(out?.ok === false ? `探针未通过：${out.error || "原因未记录"}` : "探针完成，工具清单已更新",
          out?.ok === false ? "err" : "ok");
        await load();
      } catch (e) { toast(e.message, "err"); }
    }));
    el.querySelectorAll("[data-skill-body]").forEach((btn) => btn.addEventListener("click", async () => {
      try {
        const out = await api.request(`/v1/skills/${encodeURIComponent(btn.dataset.skillBody)}/body${
          btn.dataset.version ? `?version=${encodeURIComponent(btn.dataset.version)}` : ""}`);
        showSkillDialog(btn.dataset.skillBody, out);
      } catch (e) { toast(e.message, "err"); }
    }));
  }

  function dialog(title, content) {
    const mask = document.createElement("div");
    mask.className = "dialog-mask";
    mask.innerHTML = `<div class="dialog" role="dialog" aria-modal="true" aria-label="${esc(title)}">
      <div class="side-head"><b>${esc(title)}</b><button class="icon-btn" data-close aria-label="关闭">${icon("close",15)}</button></div>${content}</div>`;
    const close = () => { dialogs.delete(mask); mask.remove(); };
    mask.addEventListener("click", e => { if (e.target === mask || e.target.closest("[data-close]")) close(); });
    document.body.appendChild(mask); dialogs.add(mask);
    mask.querySelector("input,textarea")?.focus();
    return { mask, close };
  }

  function openManage(kind) {
    const field = (name, label, area = false, value = "") => `<label class="field">${label}${area
      ? `<textarea name="${name}" rows="7">${esc(value)}</textarea>`
      : `<input name="${name}" value="${esc(value)}">`}</label>`;
    const definitions = {
      mcp: ["连接 MCP", field("id", "服务名称") + field("url", "HTTP 地址（使用本机命令时留空）")
        + `<details><summary>本机命令</summary>${field("command", "命令与参数，每行一项", true)}</details>`],
      skill: ["安装 Skill", `<p class="muted small">粘贴 SKILL.md，版本相同的内容不可改写；安装后仍须逐次授权工具。</p>`
        + field("text", "SKILL.md 内容", true, "---\nname: report-helper\nversion: 1\ndescription: 根据材料写报告\nallowed_tools: material.read\n---\n只能使用授权材料，事实不足时明确标注。")],
      ingest: ["添加知识", field("id", "来源名称") + field("text", "知识正文", true)
        + `<p class="muted small">默认只授权给当前账号；添加不会自动发布给游客。</p>`],
      search: ["检索知识", field("query", "检索内容")],
    };
    const [title, fields] = definitions[kind];
    const { mask, close } = dialog(title, `<form>${fields}<p role="status" class="form-error"></p>
      <button class="btn primary" type="submit">${kind === "search" ? "检索" : "保存"}</button></form><div data-results></div>`);
    const form = mask.querySelector("form");
    form.addEventListener("submit", async e => {
      e.preventDefault();
      const data = new FormData(form), get = name => String(data.get(name) || "").trim();
      const submit = form.querySelector('[type="submit"]'); submit.disabled = true;
      try {
        if (kind === "mcp") {
          const command = get("command").split("\n").map(v => v.trim()).filter(Boolean);
          await api.registerMcp({ server_id: get("id"), ...(get("url") ? { url: get("url") } : { command }) });
        } else if (kind === "skill") await api.installSkill(get("text"));
        else if (kind === "ingest") await api.knowledgeIngest({ source_id: get("id"), text: get("text") });
        else {
          const result = await api.knowledgeSearch(get("query"));
          const target = mask.querySelector("[data-results]");
          target.innerHTML = result.hits.length ? result.hits.map((hit, i) => `<article class="knowledge-hit">
            <p>${esc(hit.text)}</p><button class="link-btn" data-ref="${i}">回查来源</button>
            ${admin ? `<button class="link-btn" data-delete-source="${esc(hit.refs?.[0]?.source_id || "")}">删除来源</button>` : ""}</article>`).join("") : '<p class="muted">没有可见的匹配内容。</p>';
          target.querySelectorAll("[data-ref]").forEach(btn => btn.addEventListener("click", async () => {
            try { const out = await api.knowledgeResolve({...hitRef(result.hits[Number(btn.dataset.ref)]), binding: result.binding});
              dialog("来源片段", `<p>${esc(out.quote || out.text || "")}</p><p class="muted small">${esc(out.source_id || "")}</p>`);
            } catch (error) { toast(error.message, "err"); }
          }));
          target.querySelectorAll("[data-delete-source]").forEach(btn => btn.addEventListener("click", () => {
            const confirmation = dialog("删除来源", `<p>删除后，这份知识的检索与引用将失效。</p><button class="btn danger" data-confirm>删除 ${esc(btn.dataset.deleteSource)}</button>`);
            confirmation.mask.querySelector("[data-confirm]").addEventListener("click", async () => {
              try { await api.knowledgeDelete(btn.dataset.deleteSource); confirmation.close(); target.innerHTML = '<p class="muted">来源已删除，请重新检索。</p>'; }
              catch (error) { toast(error.message, "err"); }
            });
          }));
          return;
        }
        close(); toast("已保存", "ok"); await load();
      } catch (error) { mask.querySelector("[role=status]").textContent = error.message; }
      finally { submit.disabled = false; }
    });
  }
  const hitRef = hit => hit.refs?.[0] || {};

  function showSkillDialog(id, out) {
    const mask = document.createElement("div");
    mask.className = "dialog-mask";
    mask.innerHTML = `<div class="dialog" role="dialog" aria-modal="true" aria-label="Skill 声明范围">
      <div class="side-head"><b>${esc(id)}</b>
        <button class="icon-btn" data-close aria-label="关闭">${icon("close", 15)}</button></div>
      <p class="muted small">版本 ${esc(out.version || "未记录")} · 正文按需加载；
        模型建议与 Skill 文本都没有授权效力。</p>
      <pre class="step-io">${esc(String(out.body || out.content || "正文未提供"))}</pre>
    </div>`;
    mask.addEventListener("click", (e) => {
      if (e.target === mask || e.target.closest("[data-close]")) { dialogs.delete(mask); mask.remove(); }
    });
    document.body.appendChild(mask); dialogs.add(mask);
  }

  await load().catch((e) => {
    if (!stopped) toast(e.message, "err");
  });
  return { destroy: () => { stopped = true; dialogs.forEach(mask => mask.remove()); dialogs.clear(); } };
}
