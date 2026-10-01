import {Component, useState, useCallback, useEffect, type ReactNode} from 'react';
import Form from '@rjsf/core';
import validator from '@rjsf/validator-ajv8';
import type {RJSFSchema} from '@rjsf/utils';
import {createRoot} from 'react-dom/client';
import {ReactFlow, Background, Controls, MiniMap, addEdge, useNodesState, useEdgesState, type Node, type Edge, type Connection} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import './editor.css';

type Kind = 'agent'|'tool'|'condition'|'wait'|'custom';
type FlowNode = {id:string; kind:Kind; label:string; depends_on:string[]; config:Record<string,unknown>; when:Record<string,boolean>};
type Graph = {id:string; name:string; nodes:FlowNode[]; input_schema:Record<string,unknown>};
type Props = {request:(path:string, body?:unknown)=>Promise<any>; navigate:(hash:string)=>void};
const configs: Record<Kind, Record<string,unknown>> = {
  agent:{prompt:'根据输入完成任务',tools:[],inputs:{$ref:'inputs'}},
  tool:{tool:'math.add',arguments:{a:2,b:3}}, condition:{path:'inputs.approved',operator:'eq',value:true},
  wait:{seconds:5}, custom:{adapter:'passthrough',inputs:{$ref:'inputs'}},
};
const labels:Record<Kind,string>={agent:'AI 节点',tool:'工具节点',condition:'条件节点',wait:'等待节点',custom:'自定义节点'};
type Capability = {name:string;description:string;input_schema:RJSFSchema;version:string;revision:string;risks:string[];discovery_status:string;available:boolean;required_scopes:string[]};
class FormBoundary extends Component<{children:ReactNode},{failed:boolean}> {
  state={failed:false};
  static getDerivedStateFromError(){return {failed:true};}
  render(){return this.state.failed?<p role="alert">此工具的参数定义无法生成表单，请使用高级配置，或联系接入方修正定义。</p>:this.props.children;}
}
const hasMapping=(value:unknown):boolean=>Boolean(value&&typeof value==='object'&&
  (('$ref' in value)||Object.values(value).some(hasMapping)));
function Editor({request,navigate}:Props) {
  const [nodes,setNodes,onNodesChange]=useNodesState<Node>([]);
  const [edges,setEdges,onEdgesChange]=useEdgesState<Edge>([]);
  const [id,setId]=useState('integration-flow'); const [name,setName]=useState('我的集成流程');
  const [selected,setSelected]=useState<string|null>(null); const [draft,setDraft]=useState('{}');
  const [inputs,setInputs]=useState('{}'); const [schema,setSchema]=useState('{}');
  const [notice,setNotice]=useState('连接节点定义执行顺序。发布后版本固定，修改需发布新版本。');
  const [version,setVersion]=useState<number|null>(null); const [busy,setBusy]=useState(false);
  const [catalog,setCatalog]=useState<any>(null); const [saved,setSaved]=useState<any[]>([]);
  const [conditions,setConditions]=useState<Record<string,Record<string,boolean>>>({});
  const [branch,setBranch]=useState('always');
  const chosen=nodes.find(n=>n.id===selected);
  const cfg=(chosen?.data.config||{}) as Record<string,unknown>;
  const tool=(catalog?.tools as Capability[]|undefined)?.find(t=>t.name===cfg.tool);
  const edit=(key:string,value:unknown)=>{const config={...cfg,[key]:value};setNodes(ns=>ns.map(n=>n.id===selected?{...n,data:{...n.data,config}}:n));setDraft(JSON.stringify(config,null,2));setVersion(null);};
  const graph=():Graph=>({id,name,input_schema:JSON.parse(schema),nodes:nodes.map(n=>({id:n.id,
    label:String(n.data.label),kind:n.data.kind as Kind,config:n.data.config as Record<string,unknown>,
    depends_on:edges.filter(e=>e.target===n.id).map(e=>e.source),when:conditions[n.id]||{}}))});
  const act=async(fn:()=>Promise<void>)=>{setBusy(true);try{await fn();}catch(e){setNotice(e instanceof Error?e.message:'操作失败');}finally{setBusy(false);}};
  const refresh=()=>act(async()=>{setCatalog(await request('/v1/workflows/catalog'));setSaved(await request('/v1/workflow-definitions'));});
  useEffect(()=>{
    let stopped=false, inflight=false;
    const sync=async()=>{
      if(stopped||inflight||document.visibilityState==='hidden')return;
      inflight=true;
      try{const next=await request('/v1/workflows/catalog');if(!stopped)setCatalog(next);}
      catch{if(!stopped)setNotice('能力目录暂时无法刷新，保留当前草稿；执行时仍由服务端核验。');}
      finally{inflight=false;}
    };
    void sync();
    void request('/v1/workflow-definitions').then(v=>{if(!stopped)setSaved(v);}).catch(()=>{});
    const timer=window.setInterval(()=>void sync(),15000);
    const focus=()=>void sync();window.addEventListener('focus',focus);
    document.addEventListener('visibilitychange',focus);
    return ()=>{stopped=true;clearInterval(timer);window.removeEventListener('focus',focus);document.removeEventListener('visibilitychange',focus);};
  },[]);
  const load=(g:Graph)=>{setId(g.id);setName(g.name);setSchema(JSON.stringify(g.input_schema||{},null,2));
    setNodes(g.nodes.map((n,i)=>({id:n.id,position:{x:40+(i%3)*230,y:40+Math.floor(i/3)*150},data:{label:n.label||labels[n.kind],kind:n.kind,config:n.config}})));
    setEdges(g.nodes.flatMap(n=>n.depends_on.map(d=>({id:d+'-'+n.id,source:d,target:n.id}))));
    setConditions(Object.fromEntries(g.nodes.map(n=>[n.id,n.when||{}])));setSelected(null);setVersion(null);};
  const add=(kind:Kind)=>{const nid='node-'+crypto.randomUUID().slice(0,8);setNodes(ns=>[...ns,{id:nid,position:{x:40+ns.length%3*230,y:40+Math.floor(ns.length/3)*150},data:{label:labels[kind],kind,config:structuredClone(configs[kind])}}]);setVersion(null);};
  const connect=useCallback((c:Connection)=>{setEdges(es=>addEdge(c,es));setVersion(null);},[setEdges]);
  const choose=(n:Node)=>{setSelected(n.id);setDraft(JSON.stringify(n.data.config,null,2));const w=conditions[n.id]||{};const first=Object.entries(w)[0];setBranch(first?first[0]+':'+first[1]:'always');};
  const apply=()=>{try{const config=JSON.parse(draft);if(!config||Array.isArray(config)||typeof config!=='object')throw Error('配置必须是对象');
    setNodes(ns=>ns.map(n=>n.id===selected?{...n,data:{...n.data,config}}:n));setVersion(null);setNotice('节点配置已保存到草稿。');}catch(e){setNotice(String(e));}};
  const publish=()=>act(async()=>{const result=await request('/v1/workflows/'+encodeURIComponent(id)+'/publish',graph());setVersion(result.version);setNotice('已发布版本 '+result.version+'，运行使用此版本。');});
  const start=()=>act(async()=>{if(!version)throw Error('请先发布当前草稿');const result=await request('/v1/workflows/'+encodeURIComponent(id)+'/start',{version,inputs:JSON.parse(inputs),idempotency_key:crypto.randomUUID()});navigate('#/tasks/'+result.internal_run_id);});
  const exportFile=()=>{try{const url=URL.createObjectURL(new Blob([JSON.stringify({definition:graph()},null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=id+'.json';a.click();URL.revokeObjectURL(url);}catch(e){setNotice(String(e));}};
  return <section className="flow-editor"><header><h1>工作流</h1><p>把模型、工具和已有系统连接为可复用流程。</p></header>
    <div className="flow-toolbar"><label>流程 ID<input value={id} onChange={e=>{setId(e.target.value);setVersion(null);}}/></label><label>名称<input value={name} onChange={e=>{setName(e.target.value);setVersion(null);}}/></label>
      <button onClick={refresh} disabled={busy}>刷新能力与流程</button><button onClick={exportFile}>导出</button>
      <label className="flow-import">导入<input type="file" accept="application/json,.json" onChange={e=>{const f=e.target.files?.[0];if(f)void act(async()=>load(JSON.parse(await f.text()).definition));}}/></label>
      <button onClick={publish} disabled={busy}>发布{version?' · v'+version:''}</button><button onClick={start} disabled={busy||!version||!catalog?.engine_enabled} title={catalog?.engine_enabled?"运行已发布版本":"需要连接可用的 Temporal 工作流服务"}>运行</button></div>
    <div className="flow-notice" role="status">{notice}</div>
    {catalog&&<p>执行引擎：{catalog.engine_enabled?'Temporal 已装配':catalog.engine_status==='unavailable'?'Temporal 暂不可用':'未配置 Temporal'}；能力每 15 秒自动同步。自定义节点：{catalog.custom_nodes.join('、')}</p>}
    {saved.length>0&&<div className="flow-saved">{saved.map(s=><button key={s.id+'@'+s.version} onClick={()=>act(async()=>{const r=await request('/v1/workflows/'+encodeURIComponent(s.id)+'/versions/'+s.version);load(r.definition);setVersion(s.version);})}>{s.name} · v{s.version}</button>)}</div>}
    <div className="flow-toolbar">{(Object.keys(labels) as Kind[]).map(k=><button key={k} onClick={()=>add(k)}>＋{labels[k]}</button>)}</div>
    <div className="flow-layout"><div className="flow-canvas"><ReactFlow nodes={nodes} edges={edges} onNodesChange={changes=>{onNodesChange(changes);if(changes.some(c=>c.type==='remove'))setVersion(null);}}
      onEdgesChange={changes=>{onEdgesChange(changes);setVersion(null);}} onConnect={connect} onNodeClick={(_,n)=>choose(n)} fitView deleteKeyCode={['Backspace','Delete']}>
      <Background/><Controls/><MiniMap/></ReactFlow></div>
      <aside className="flow-config">{selected?<><h2>节点配置</h2><label>节点名称<input value={String(chosen?.data.label||'')} onChange={e=>{setNodes(ns=>ns.map(n=>n.id===selected?{...n,data:{...n.data,label:e.target.value}}:n));setVersion(null);}}/></label>
        {chosen?.data.kind==='agent'&&<><label>任务指令<textarea value={String(cfg.prompt||'')} onChange={e=>edit('prompt',e.target.value)}/></label><label>使用工具（逗号分隔）<input value={(cfg.tools as string[]||[]).join(',')} onChange={e=>edit('tools',e.target.value.split(',').map(v=>v.trim()).filter(Boolean))}/></label></>}
        {chosen?.data.kind==='tool'&&<><label>工具能力<select value={String(cfg.tool||'')} onChange={e=>{
          const config={tool:e.target.value,arguments:{}};
          setNodes(ns=>ns.map(n=>n.id===selected?{...n,data:{...n.data,config}}:n));setDraft(JSON.stringify(config,null,2));setVersion(null);
        }}>
          {!tool&&<option value={String(cfg.tool||'')}>{String(cfg.tool||'请选择能力')}（未发现）</option>}
          {(catalog?.tools as Capability[]|undefined)?.map(t=><option key={t.name} value={t.name} disabled={t.available===false}>{t.name}{t.available===false?' · 暂不可用':''}</option>)}
        </select></label>
        {tool?<><p>{tool.description}</p><p className="flow-cap-meta">版本 {tool.version||'未记录'} · 风险 {tool.risks?.join('、')||'未声明'} · {tool.discovery_status==='unavailable'?'发现失败':tool.discovery_status==='local'?'本地能力':'缓存发现结果，调用时核验'}</p>
          {Boolean((cfg.capability_bindings as Record<string,string>|undefined)?.[tool.name]&&
            (cfg.capability_bindings as Record<string,string>)[tool.name]!==tool.revision)&&<p role="alert">工具契约已变化，旧发布版本会阻止执行；请检查参数并重新发布。</p>}
          {!tool.input_schema?<p>当前服务尚未提供参数定义，请使用高级配置；重启升级后的核心可生成参数表单。</p>:hasMapping(cfg.arguments)?<p>参数使用了流程输入映射，请在高级配置中编辑，表单不会覆盖映射。</p>:
            <FormBoundary key={tool.name+tool.revision+selected}><Form schema={tool.input_schema} validator={validator}
              formData={cfg.arguments||{}} idPrefix={'args-'+selected} liveValidate
              uiSchema={{'ui:submitButtonOptions':{norender:true}}}
              onChange={e=>edit('arguments',e.formData||{})}><span/></Form></FormBoundary>}
        </>:<p role="alert">此能力未接入、已停用或不在当前权限范围，请选择可用能力。</p>}</>}
        {chosen?.data.kind==='wait'&&<label>等待秒数<input type="number" min="0" max="2592000" value={Number(cfg.seconds||0)} onChange={e=>edit('seconds',Number(e.target.value))}/></label>}
        {chosen?.data.kind==='condition'&&<><label>判断字段<input value={String(cfg.path||'')} onChange={e=>edit('path',e.target.value)}/></label><label>判断方式<select value={String(cfg.operator||'eq')} onChange={e=>edit('operator',e.target.value)}><option value="eq">等于</option><option value="ne">不等于</option><option value="contains">包含</option><option value="exists">存在</option></select></label><label>比较值<input value={String(cfg.value??'')} onChange={e=>{let value:unknown=e.target.value;try{value=JSON.parse(e.target.value);}catch{}edit('value',value);}}/></label></>}
        {chosen?.data.kind==='custom'&&<label>已注册能力<select value={String(cfg.adapter||'passthrough')} onChange={e=>edit('adapter',e.target.value)}>{(catalog?.custom_nodes||['passthrough']).map((v:string)=><option key={v} value={v}>{v}</option>)}</select></label>}
        <details><summary>高级配置与输入映射</summary><p>节点引用：{selected}</p><textarea aria-label="节点配置" value={draft} onChange={e=>{setDraft(e.target.value);setVersion(null);}}/><button onClick={apply}>应用高级配置</button></details>
        <label>条件分支<select value={branch} onChange={e=>setBranch(e.target.value)}><option value="always">无条件</option>{edges.filter(e=>e.target===selected&&nodes.find(n=>n.id===e.source)?.data.kind==='condition').flatMap(e=>[<option key={e.source+'t'} value={e.source+':true'}>{e.source} 为真</option>,<option key={e.source+'f'} value={e.source+':false'}>{e.source} 为假</option>])}</select></label>
        <button onClick={()=>{const v=branch.split(':');setConditions(c=>({...c,[selected]:branch==='always'?{}:{[v[0]]:v[1]==='true'}}));setVersion(null);}}>应用分支</button>
        <button onClick={()=>{setNodes(ns=>ns.filter(n=>n.id!==selected));setEdges(es=>es.filter(e=>e.source!==selected&&e.target!==selected));setSelected(null);setVersion(null);}}>删除节点</button></>:<p>选择节点编辑配置。连线表示依赖，条件节点可控制后续分支。</p>}
      <details><summary>运行输入（高级）</summary><textarea aria-label="运行输入" value={inputs} onChange={e=>setInputs(e.target.value)}/><p>使用 {"{\"$ref\":\"inputs.field\"}"} 引用输入，或 nodes.节点ID 引用已声明依赖的输出。</p></details>
      <details><summary>输入校验（高级）</summary><textarea aria-label="输入校验" value={schema} onChange={e=>{setSchema(e.target.value);setVersion(null);}}/></details></aside></div>
  </section>;
}
export function mount(container:HTMLElement, props:Props){const root=createRoot(container);root.render(<Editor {...props}/>);return ()=>root.unmount();}
