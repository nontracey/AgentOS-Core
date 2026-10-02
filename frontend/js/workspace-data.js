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
  if (tool.name?.startsWith("workflow.")) return "已发布工作流";
  if (tool.description?.startsWith("[External Agent via MCP]")) return "外部 Agent · MCP";
  // MCP names are namespaced by the existing adapter; no guessed host identity.
  return tool.name.includes(":") ? "MCP 服务" : "核心工具";
}
export function riskLabels(risks = []) {
  const labels = {read:"读取", write:"写入", delete:"删除", exec:"执行命令", egress:"外部通信"};
  return risks.map(r => labels[r] || r);
}

export function isExampleTool(tool) { return ["math.add", "log.append", "flaky.timeout"].includes(tool.name); }
export function capabilityLabel(tool) {
  return {"material.list":"任务材料清单", "material.read":"读取授权材料", "data.csv_profile":"CSV 数据检查",
    "data.json_query":"JSON 字段提取", "artifact.create":"生成文件成果", "fs.write_text":"沙箱文本保存",
    "skill.read":"读取任务技能", "knw.search":"检索授权知识", "clock.now":"当前时间",
    "workflow.status":"查询流程进展", "math.add":"加法示例", "log.append":"日志幂等示例",
    "flaky.timeout":"故障恢复示例"}[tool.name] || tool.name;
}
export function outputAssets(assets) { return assets.filter(a => Boolean(a.run_id)); }
export function shortGoal(goal, limit = 76) {
  const value = String(goal || "未命名任务").replace(/\s+/g, " ");
  return value.length > limit ? value.slice(0, limit) + "…" : value;
}
