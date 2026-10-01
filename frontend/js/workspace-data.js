/* Shared presentation of server facts; never makes authorization decisions. */
export const ATTENTION_STATES = new Set(["awaiting_approval", "awaiting_input", "awaiting_review", "paused", "failed"]);
export const ACTIVE_STATES = new Set(["created", "planning", "running", "cancel_requested"]);
export function filterRuns(runs, filter = "all", query = "") {
  const q = query.trim().toLowerCase();
  return runs.filter(r => (filter !== "attention" || ATTENTION_STATES.has(r.state))
    && (!q || String(r.goal || r.id || "").toLowerCase().includes(q)));
}
export function roleLabel(role) {
  return {admin:"管理员", member:"成员", guest:"游客（只读）"}[role] || "角色未确认";
}
export function taskTitle(run) {
  const goal = String(run.goal || run.id || "未命名任务");
  return goal.startsWith("workflow:") ? "流程 · " + goal.slice(9) : goal;
}
export function capabilitySource(tool) {
  // MCP names are namespaced by the existing adapter; no guessed host identity.
  return tool.name.includes(":") ? "MCP 服务" : "核心工具";
}
export function riskLabels(risks = []) {
  const labels = {read:"读取", write:"写入", delete:"删除", exec:"执行命令", egress:"外部通信"};
  return risks.map(r => labels[r] || r);
}
