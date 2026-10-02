/* Evidence projection only: missing events remain missing, no synthetic progress. */
import { capabilityLabel } from "./workspace-data.js";
import { esc } from './safe-export.js';
import { stateBadge, fmtCost } from './ui.js';
import * as ui from './ui.js';

export function executionMetrics(run, steps, events, budget) {
  const usage = events.filter(e => e.kind === 'model.usage');
  const tokens = usage.length ? usage.reduce((n, e) => n + Number(e.payload?.input_tokens || 0) + Number(e.payload?.output_tokens || 0), 0) : null;
  const end = ['succeeded','failed','cancelled'].includes(run.state) ? run.updated_at : new Date().toISOString();
  const ms = new Date(end) - new Date(run.created_at);
  return { tokens, calls: usage.length, duration: Number.isFinite(ms) ? Math.max(0, ms) : null,
    completed: steps.filter(s => s.state === 'completed').length, steps: steps.length,
    retries: steps.reduce((n,s) => n + Math.max(0, (s.attempt || 1) - 1), 0), cost: budget?.settled == null ? '未记录' : budget.settled > 0 || usage.some(e => e.payload?.pricing_configured) ? fmtCost(budget.settled) : '未计价' };
}
export function duration(ms) {
  if (ms == null) return '未记录';
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.floor(ms / 60000)}m ${Math.floor(ms / 1000 % 60)}s`;
}
function toolLabel(name, args = {}) {
  const label=capabilityLabel({name});if(/^(artifact\.|data\.|material\.)/.test(name) && label!==name)return label;
  if (name === 'skill.read') return '加载任务技能';
  if (name === 'fs.write_text') return '保存成果文件';
  if (name === 'material.read') return '读取授权材料';
  if (/video.*generate/.test(name)) return '生成视频';
  if (/image.*generate/.test(name)) return '生成图片';
  if (/comfyui.*generate/.test(name)) return /wan|video/i.test(args.model || '') ? '生成视频' : '生成图片';
  return name;
}
export function projectExecution(d, events) {
  const prompts = events.filter(e => e.kind === 'model.request');
  const usage = events.filter(e => e.kind === 'model.usage');
  const finished = events.filter(e => e.kind === 'model.completed');
  const selections = events.filter(e => e.kind === 'model.tool_selection');
  const prepared = events.filter(e => e.kind === 'tool.prepared');
  const routes = events.filter(e => e.kind === 'node.dispatched');
  const checks = [...new Map(events.filter(e => e.kind.startsWith('report.') && e.kind.endsWith('_check')).map(e => [e.kind,e])).values()];
  const nodes = [{ id: 'goal', type: 'input', title: '目标受理', subtitle: '自然语言 · HTTP 入口', state: 'completed',
    fields: [['用户目标', d.run?.goal_text || d.run?.goal || '未记录'], ['执行模式', d.plan?.source === 'template' ? '资料报告模板 v1' : '目标驱动 · 按实际工具调用展开']],
    note: '服务端已受理任务，执行与审批均记录在同一任务账本。' }];
  const modelIds = [...new Set([...prompts, ...usage, ...finished].map(e => e.payload?.call_id || 'model-legacy'))];
  for (const id of modelIds) {
    const p = prompts.find(e => e.payload?.call_id === id)?.payload;
    const u = usage.find(e => (e.payload?.call_id || 'model-legacy') === id)?.payload;
    const done = finished.find(e => e.payload?.call_id === id)?.payload;
    const selection = selections.find(e => e.payload?.call_id === id)?.payload;
    nodes.push({ id, type: 'model', title: '模型调用', subtitle: u?.model || done?.model || '模型名称未记录', state: u || done ? 'completed' : (['failed','cancelled'].includes(d.run?.state) ? d.run.state : 'dispatched'),
      fields: [['调用 ID', id], ['模型耗时', duration(done?.latency_ms ?? null)], ['输入 / 输出 token', u ? `${u.input_tokens ?? '未记录'} / ${u.output_tokens ?? '未记录'}` : '未记录'],
        ['工具选择', selection?.tools?.map(t => t.name).join('、') || '未记录工具选择']],
      prompts: p?.messages || [], note: selection?.basis || p?.note || '历史任务未保存提示词；不会根据目标补造。', time: prompts.find(e => e.payload?.call_id === id)?.created_at });
  }
  for (const s of d.steps || []) {
    const p = prepared.find(e => e.payload?.step_id === s.id)?.payload;
    const r = routes.find(e => e.payload?.step_id === s.id)?.payload;
    let result;
    try { result = JSON.parse(s.result_json || '{}'); } catch { result = {}; }
    const values = p?.arguments || {};
    nodes.push({ id: s.id, type: r ? 'node' : 'tool', title: toolLabel(s.tool_name || s.id, values),
      subtitle: r ? `${r.os || '远程'} · ${r.node_id} · ${r.tool}` : s.tool_name || '受控工具调用', state: s.state,
      fields: [['策略裁定', {allowed:'允许执行',needs_approval:'需要人工审批',denied:'拒绝执行'}[p?.policy] || '历史裁定未记录'],
        ['裁定依据', p?.reason || '未记录'], ['风险', p?.risks?.join(' / ') || '未记录'],
        ['实际路由', r ? `${r.node_id} · ${r.os || 'OS 未记录'}` : '未记录节点派发'],
        ['路由依据', r?.basis || '仅显示有事件证明的节点路由'], ['恢复策略', p?.recovery || '未记录'],
        ['步骤耗时', s.started_at && s.finished_at ? duration(new Date(s.finished_at)-new Date(s.started_at)) : '未记录']],
      args: values, result, error: s.error_json, approval: s.approval_id,
      note: '参数与策略由本次执行事件提供。审批绑定调用与参数指纹，不会改变模型建议。' });
  }
  for (const e of checks) nodes.push({ id: `check-${e.seq}`, type: 'check', title: ({'report.structure_check':'报告结构检查','report.citation_check':'来源引用检查','report.fact_check':'日期与金额检查','report.length_check':'报告字数检查'})[e.kind] || '报告检查', subtitle: '确定性校验', state: e.payload?.passed ? 'completed' : 'failed',
    fields: [['检查结论', e.payload?.passed ? '通过' : '未通过'], ['检查依据', e.payload?.detail || '见高级事件']], note: '由服务端检查规则裁定。' });
  if ((d.artifacts || []).length) nodes.push({ id: 'delivery', type: 'asset', title: '成果交付', subtitle: `${d.artifacts.length} 份不可变成果`, state: 'completed',
    fields: d.artifacts.map(a => [ui.assetTitle?.(a) || a.name || a.id, `${a.version ? `v${a.version} · ` : ''}${a.mime}`]), note: '成果预览与下载沿用主体权限，版本不可覆盖。' });
  if (d.plan?.source !== 'template' && d.plan?.tasks?.length) {
    const titles = {'research-1':'材料研究','design-1':'视觉设计','aggregate-1':'汇总方案','review-1':'来源评审','deliver-1':'成果交付'};
    for (const task of d.plan.tasks) {
      const started = events.findLast(e => e.kind === 'plan.task_started' && e.payload?.task_id === task.task_id)?.payload;
      const finishedTask = events.findLast(e => e.kind === 'plan.task_finished' && e.payload?.task_id === task.task_id)?.payload;
      nodes.push({id:`task:${task.task_id}`,type:'plan',title:task.title || titles[task.task_id] || task.task_id,
        subtitle:d.plan.source === 'temporal' ? `${task.role_id} · 工作流节点`
          : `${task.role_id} · ${started?.node_id || task.required_os || 'local'}`,
        state:finishedTask?.state || (started ? 'running' : task.state || 'pending'),
        fields:[['计划版本',String(d.plan.version || '未记录')],['角色',task.role_id],
          ['依赖步骤',(task.depends_on || []).join('、') || '无'],['指定能力',(task.required_capabilities || []).join('、') || '本地研究/检查'],
          ['实际设备',started?.node_id || '尚未记录'],['工具版本',started?.tool_version || '未记录'],
          ['成果',JSON.stringify(finishedTask?.output || finishedTask?.issues || finishedTask?.reused_visual || finishedTask?.output_sha256 || '未完成')]],
        note:'计划依赖来自服务端合法 DAG；返修历史独立保留，不改写已经完成的视觉步骤。'});
    }
  }
  // Events provide actual chronology across model rounds and tools.
  const eventOrder = id => events.findIndex(e => e.payload?.step_id === id || e.payload?.call_id === id);
  const middle = nodes.slice(1).filter(n => !['check','asset'].includes(n.type));
  middle.sort((a,b) => (eventOrder(a.id) < 0 ? 1e9 : eventOrder(a.id)) - (eventOrder(b.id) < 0 ? 1e9 : eventOrder(b.id)));
  return [nodes[0], ...middle, ...nodes.slice(1).filter(n => ['check','asset'].includes(n.type))];
}
export function flowNode(node, i, selected) {
  const glyph = { input:'↗', model:'✦', tool:'⌘', node:'⇄', plan:'◇', check:'✓', asset:'▤' }[node.type];
  return `<button class="flow-node ${selected ? 'selected' : ''}" data-node="${esc(node.id)}" aria-pressed="${selected}">
    <span class="flow-index">${String(i+1).padStart(2,'0')}</span><span class="flow-glyph ${esc(node.type)}">${glyph}</span>
    <span class="flow-label"><b>${esc(node.title)}</b><span>${esc(node.subtitle)}</span></span>${stateBadge(node.state)}<span class="flow-chevron">›</span></button>`;
}
export function inspector(node) {
  return `<div class="inspector-heading"><span class="eyebrow">STEP INSPECTOR</span><h3>${esc(node.title)}</h3>${stateBadge(node.state)}</div>
    <div class="inspector-note">${esc(node.note)}</div><dl class="inspector-kv">${node.fields.map(([k,v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>
    ${node.prompts ? `<h4>实际提示词 <span class="tag">系统 / 用户指令</span></h4>${node.prompts.length ? node.prompts.map((p,i) => `<details class="prompt-block" data-open-key="${esc(node.id)}-prompt-${i}" open><summary>${p.role === 'system' ? '系统指令' : '用户输入'}</summary><pre>${esc(p.content)}</pre></details>`).join('') : '<p class="small muted">该调用没有保存提示词证据。</p>'}` : ''}
    ${node.args ? `<h4>实际工具参数</h4>${Object.entries(node.args).length ? Object.entries(node.args).map(([k,v]) => `<div class="param-block"><span>${esc(k)}</span>${typeof v === 'object' ? `<details class="tech-details"><summary>高级：结构化参数</summary><pre>${esc(JSON.stringify(v,null,2))}</pre></details>` : `<pre>${esc(String(v))}</pre>`}</div>`).join('') : '<p class="small muted">历史任务未记录参数正文，仅保留参数指纹。</p>'}` : ''}
    ${node.error ? `<p class="error-text">执行错误：${esc(node.error)}</p>` : ''}
    ${node.result ? `<details class="tech-details" data-open-key="${esc(node.id)}-result"><summary>高级：工具回执</summary><pre class="step-io">${esc(JSON.stringify(node.result,null,2))}</pre></details>` : ''}`;
}
