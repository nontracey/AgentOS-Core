/* Shared goal/material composer. Drafts and retry keys survive rerenders and reloads. */
import { esc } from "./safe-export.js";
import { humanError, icon, toast } from "./ui.js";

const MAX_FILE = 64 * 1024, MAX_TOTAL = 128 * 1024, MAX_FILES = 3;

export function mountComposer(el, { api, scope = "new", goal = "", template = "", contextRun = "",
  parentAsset = "", supplement = () => false, onAccepted, isBlocked = () => false }) {
  const draftKey = `agentos_draft_${scope}`;
  let state;
  try { state = JSON.parse(sessionStorage.getItem(draftKey)); } catch { /* invalid draft */ }
  state ||= { goal, template, materials: [], key: `ui_${crypto.randomUUID()}`, signature: "", skillId: "" };
  let busy = false, destroyed = false;
  const persist = () => sessionStorage.setItem(draftKey, JSON.stringify(state));
  el.innerHTML = `<form class="composer" aria-label="${contextRun ? "修改任务" : "新任务"}">
    <label class="sr-only" for="composer-goal">${contextRun ? "补充要求或修改说明" : "任务目标"}</label>
    <textarea id="composer-goal" maxlength="2000" required placeholder="${contextRun
      ? "告诉我哪里需要修改，我会保留原材料和上一版成果…" : "描述你想完成的事，也可以附上资料…"}">${esc(state.goal)}</textarea>
    <div class="composer-materials"></div>
    <div class="composer-foot"><div class="composer-tools">
      <button type="button" class="chip" data-paste>${icon("doc", 14)} 粘贴材料</button>
      <label class="chip file-chip">${icon("upload", 14)} 上传文件<input type="file" accept=".txt,.md" multiple hidden aria-label="上传文本材料"></label>
      ${contextRun ? "" : `<label class="composer-template"><span class="sr-only">执行方式</span>
        <select aria-label="执行方式"><option value="">自动执行</option><option value="material-report">资料报告</option><option value="activity-collaboration">活动协作（研究／视觉／评审）</option></select></label>`}
      ${contextRun ? "" : `<select data-skill aria-label="使用技能"><option value="">默认技能</option></select>`}
    </div><button class="send" type="submit" aria-label="${contextRun ? "提交修改" : "开始任务"}">${icon("send", 18)}</button></div>
    <div class="composer-note">${contextRun ? "沿用授权材料和必要上下文；文件成果保留旧版。" : "支持 UTF-8 .txt / .md，最多3份，单份64 KiB、合计128 KiB。"}</div>
    <div class="error-text" role="alert" hidden></div>
  </form>`;
  const form = el.querySelector("form"), input = el.querySelector("textarea"), error = el.querySelector('[role="alert"]');
  const select = el.querySelector(".composer-template select");
  const skillSelect = el.querySelector("[data-skill]");
  if (skillSelect) {
    skillSelect.addEventListener("change", () => { state.skillId = skillSelect.value; persist(); });
    api.listSkills().then(skills => {
      if (destroyed) return;
      skillSelect.innerHTML = '<option value="">默认技能</option>' + skills.map(s =>
        `<option value="${esc(s.id)}">${esc(s.id)} v${esc(s.version)}</option>`).join("");
      skillSelect.value = state.skillId || "";
    }).catch(() => { skillSelect.hidden = true; });
  }
  if (select) select.value = state.template;
  input.addEventListener("input", () => { state.goal = input.value; persist(); });
  select?.addEventListener("change", () => { state.template = select.value; persist(); });
  function drawMaterials() {
    el.querySelector(".composer-materials").innerHTML = state.materials.map((m, i) =>
      `<span class="material-chip">${icon("doc", 13)} ${esc(m.name)} <small>${m.size.toLocaleString()} 字节</small>
        <button type="button" data-remove="${i}" aria-label="移除 ${esc(m.name)}">${icon("close", 13)}</button></span>`).join("");
    el.querySelectorAll("[data-remove]").forEach(b => b.addEventListener("click", () => {
      if (busy) return;
      state.materials.splice(Number(b.dataset.remove), 1); persist(); drawMaterials();
    }));
  }
  function addMaterial(name, text) {
    const size = new TextEncoder().encode(text).length;
    if (!text.trim()) throw new Error("材料内容不能为空");
    if (size > MAX_FILE) throw new Error("单份材料不能超过64 KiB");
    if (state.materials.length >= MAX_FILES) throw new Error("最多添加3份材料");
    if (state.materials.reduce((n, m) => n + m.size, size) > MAX_TOTAL) throw new Error("材料合计不能超过128 KiB");
    state.materials.push({ name, text, size }); persist(); drawMaterials();
  }
  el.querySelector("input[type=file]").addEventListener("change", async e => {
    for (const file of e.target.files) {
      try {
        if (!/\.(txt|md)$/i.test(file.name)) throw new Error("仅支持 .txt / .md 文件");
        if (file.size > MAX_FILE) throw new Error("单份材料不能超过64 KiB");
        const text = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
        if (destroyed) return;
        addMaterial(file.name, text);
      } catch (err) { toast(err instanceof TypeError ? "文件不是有效的 UTF-8 文本" : err.message, "err"); }
    }
    e.target.value = "";
  });
  let dialog;
  el.querySelector("[data-paste]").addEventListener("click", () => {
    dialog = document.createElement("div"); dialog.className = "dialog-mask";
    dialog.innerHTML = `<form class="dialog" role="dialog" aria-modal="true" aria-label="添加文本材料">
      <div class="side-head"><b>添加文本材料</b><button type="button" class="icon-btn" data-close aria-label="关闭">${icon("close", 16)}</button></div>
      <div class="field"><label for="material-name">材料名称</label><input id="material-name" value="文本材料.md" maxlength="100" required></div>
      <div class="field"><label for="material-text">材料内容</label><textarea id="material-text" rows="8" required></textarea></div>
      <div class="error-text" role="alert"></div><div class="dialog-actions"><button type="button" class="btn" data-close>取消</button><button class="btn primary" type="submit">添加材料</button></div>
    </form>`;
    const close = () => { dialog.remove(); el.querySelector("[data-paste]")?.focus(); };
    dialog.querySelectorAll("[data-close]").forEach(b => b.onclick = close);
    dialog.addEventListener("keydown", e => { if (e.key === "Escape") close(); });
    dialog.addEventListener("click", e => { if (e.target === dialog) close(); });
    dialog.querySelector("form").onsubmit = e => {
      e.preventDefault();
      try { addMaterial(dialog.querySelector("input").value.trim(), dialog.querySelector("textarea").value); close(); }
      catch (err) { dialog.querySelector('[role="alert"]').textContent = err.message; }
    };
    document.body.appendChild(dialog); dialog.querySelector("textarea").focus();
  });
  form.addEventListener("submit", async e => {
    e.preventDefault(); if (busy || isBlocked()) return;
    state.goal = input.value.trim(); if (!state.goal) return;
    const signature = JSON.stringify([state.goal, state.template, state.skillId, state.materials, contextRun, parentAsset]);
    if (state.signature && state.signature !== signature) state.key = `ui_${crypto.randomUUID()}`;
    state.signature = signature; persist(); busy = true;
    error.hidden = true;
    form.querySelectorAll("input,textarea,select,button").forEach(n => n.disabled = true);
    try {
      const materialIds = [];
      for (const m of state.materials) materialIds.push((await api.uploadMaterial(m.name, m.text)).asset_id);
      const receipt = contextRun ? await (supplement() ? api.supplementTask : api.followupTask)(contextRun, {
        message: state.goal, material_asset_ids: materialIds, parent_asset_id: parentAsset || undefined, idempotency_key: state.key,
      }) : await api.submitTask({ payload: { goal: state.goal,
        template: state.template || (materialIds.length ? "material-report" : undefined),
        skill_ids: state.skillId ? [state.skillId] : [] },
        material_asset_ids: materialIds, idempotency_key: state.key });
      sessionStorage.removeItem(draftKey);
      if (!destroyed) onAccepted(receipt);
    } catch (err) { if (!destroyed) { error.hidden = false; error.textContent = humanError(err.message); } }
    finally { busy = false; form.querySelectorAll("input,textarea,select,button").forEach(n => n.disabled = false); }
  });
  drawMaterials();
  return {
    setGoal(value, useTemplate = false) { state.goal = value; input.value = value;
      if (useTemplate) { state.template = "material-report"; if (select) select.value = state.template; }
      persist(); input.focus(); },
    focus: () => input.focus(), destroy: () => { destroyed = true; dialog?.remove(); },
  };
}
