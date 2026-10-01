import {Component, useState, useCallback, useEffect, useRef, type ReactNode} from 'react';
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
type Props = {request:(path:string, body?:unknown)=>Promise<any>; navigate:(hash:string)=>void; confirm:(message:string)=>Promise<boolean>};
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
function Editor({request,navigate,confirm}:Props) {
  const [editing,setEditing]=useState(false); const [dirty,setDirty]=useState(false);
  const [query,setQuery]=useState(''); const [draftStarted,setDraftStarted]=useState(false);
  const [listError,setListError]=useState(''); const [ready,setReady]=useState(false);
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
  const submission=useRef<{signature:string;key:string}|null>(null);
  const chosen=nodes.find(n=>n.id===selected);
  const cfg=(chosen?.data.config||{}) as Record<string,unknown>;
  const tool=(catalog?.tools as Capability[]|undefined)?.find(t=>t.name===cfg.tool);
  const edit=(key:string,value:unknown)=>{const config={...cfg,[key]:value};setNodes(ns=>ns.map(n=>n.id===selected?{...n,data:{...n.data,config}}:n));setDraft(JSON.stringify(config,null,2));setVersion(null);setDirty(true);};
  const graph=():Graph=>({id,name,input_schema:JSON.parse(schema),nodes:nodes.map(n=>({id:n.id,
    label:String(n.data.label),kind:n.data.kind as Kind,config:n.data.config as Record<string,unknown>,
    depends_on:edges.filter(e=>e.target===n.id).map(e=>e.source),when:conditions[n.id]||{}}))});
  const act=async(fn:()=>Promise<void>)=>{setBusy(true);try{await fn();}catch(e){const message=e instanceof Error?e.message:'操作失败';setNotice(message);if(!editing)setListError(message);}finally{setBusy(false);}};
  const refresh=()=>act(async()=>{setCatalog(await request('/v1/workflows/catalog'));setSaved(await request('/v1/workflow-definitions'));setListError('');setReady(true);});
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
    void request('/v1/workflow-definitions').then(v=>{if(!stopped){setSaved(v);setReady(true);}}).catch(()=>{if(!stopped){setListError('流程列表暂时无法读取，请重试；不会清除当前草稿。');setReady(true);}});
    const timer=window.setInterval(()=>void sync(),15000);
    const focus=()=>void sync();window.addEventListener('focus',focus);
    document.addEventListener('visibilitychange',focus);
    return ()=>{stopped=true;clearInterval(timer);window.removeEventListener('focus',focus);document.removeEventListener('visibilitychange',focus);};
  },[]);
  const load=(g:Graph)=>{setId(g.id);setName(g.name);setSchema(JSON.stringify(g.input_schema||{},null,2));
    setNodes(g.nodes.map((n,i)=>({id:n.id,position:{x:40+(i%3)*230,y:40+Math.floor(i/3)*150},data:{label:n.label||labels[n.kind],kind:n.kind,config:n.config}})));
    setEdges(g.nodes.flatMap(n=>n.depends_on.map(d=>({id:d+'-'+n.id,source:d,target:n.id}))));
    setConditions(Object.fromEntries(g.nodes.map(n=>[n.id,n.when||{}])));setSelected(null);setVersion(null);setDirty(false);setDraftStarted(true);};
  const add=(kind:Kind)=>{const nid='node-'+crypto.randomUUID().slice(0,8);setNodes(ns=>[...ns,{id:nid,position:{x:40+ns.length%3*230,y:40+Math.floor(ns.length/3)*150},data:{label:labels[kind],kind,config:structuredClone(configs[kind])}}]);setVersion(null);setDirty(true);};
  const connect=useCallback((c:Connection)=>{setEdges(es=>addEdge(c,es));setVersion(null);setDirty(true);},[setEdges]);
  const choose=(n:Node)=>{setSelected(n.id);setDraft(JSON.stringify(n.data.config,null,2));const w=conditions[n.id]||{};const first=Object.entries(w)[0];setBranch(first?first[0]+':'+first[1]:'always');};
  const apply=()=>{try{const config=JSON.parse(draft);if(!config||Array.isArray(config)||typeof config!=='object')throw Error('配置必须是对象');
    setNodes(ns=>ns.map(n=>n.id===selected?{...n,data:{...n.data,config}}:n));setVersion(null);setDirty(true);setNotice('节点配置已保存到草稿。');}catch(e){setNotice(String(e));}};
  const publish=()=>act(async()=>{const result=await request('/v1/workflows/'+encodeURIComponent(id)+'/publish',graph());setVersion(result.version);setDirty(false);setSaved(await request('/v1/workflow-definitions').catch(()=>saved));setNotice('已发布版本 '+result.version+'，运行使用此版本。');});
  const start=()=>act(async()=>{if(!version)throw Error('请先发布当前草稿');
    const parsed=JSON.parse(inputs);const signature=JSON.stringify([id,version,parsed]);
    if(submission.current?.signature!==signature)submission.current={signature,key:crypto.randomUUID()};
    const result=await request('/v1/workflows/'+encodeURIComponent(id)+'/start',
      {version,inputs:parsed,idempotency_key:submission.current.key});
    navigate('#/tasks/'+result.internal_run_id);});
  const exportFile=()=>{try{const url=URL.createObjectURL(new Blob([JSON.stringify({definition:graph()},null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=id+'.json';a.click();URL.revokeObjectURL(url);}catch(e){setNotice(String(e));}};
  const canReplace=async()=>!dirty||await confirm('当前草稿尚未发布。替换后将丢弃本页的修改，可先返回编辑并导出。');
  const open=async(flowId:string,flowVersion:number)=>{if(!await canReplace())return;
    await act(async()=>{const r=await request('/v1/workflows/'+encodeURIComponent(flowId)+'/versions/'+flowVersion);
      load(r.definition);setVersion(flowVersion);setInputs('{}');setEditing(true);setNotice('正在查看已发布版本 '+flowVersion+'；修改后需发布新版本。');});};
  const create=async()=>{if(!await canReplace())return;
    load({id:'flow-'+crypto.randomUUID().slice(0,8),name:'我的集成流程',nodes:[],input_schema:{}});
    setInputs('{}');setEditing(true);setNotice('添加节点并连接。发布固定版本后才能运行。');};
  const groups=Array.from(new Set(saved.map(f=>f.id))).map(flowId=>{
    const versions=saved.filter(f=>f.id===flowId).sort((a,b)=>b.version-a.version);
    return {latest:versions[0],versions};
  }).filter(g=>(g.latest.name+' '+g.latest.id).toLowerCase().includes(query.trim().toLowerCase()));
  if(!editing)return <section className="flow-editor flow-list">
    <header className="flow-list-heading"><div><h1>工作流</h1><p>把已有能力连接成流程，发布后可重复运行。</p></div>
      <button className="flow-primary" disabled={busy} onClick={()=>void create()}>新建流程</button></header>
    <div className="flow-list-tools"><input type="search" aria-label="搜索流程" placeholder="搜索名称或流程 ID…" value={query} onChange={e=>setQuery(e.target.value)}/>
      <button onClick={refresh} disabled={busy}>刷新能力与流程</button>
      {draftStarted&&<button onClick={()=>setEditing(true)}>继续当前{dirty?'草稿':'流程'}</button>}</div>
    <p className="flow-engine">{catalog?.engine_enabled?'工作流服务已连接':catalog?.engine_status==='unavailable'?'工作流服务暂不可用，仍可编辑草稿':'按需连接 Temporal 后可运行流程'}</p>
    {listError&&<p role="alert" className="flow-notice">{listError}</p>}
    <div className="flow-cards">{groups.map(({latest,versions})=><article className="flow-card" key={latest.id}>
      <div><h2>{latest.name}</h2><span className="flow-version">已发布 · v{latest.version}</span></div>
      <p>流程 ID：{latest.id}</p><button className="flow-primary" disabled={busy} onClick={()=>void open(latest.id,latest.version)}>打开流程</button>
      <details><summary>历史版本 · {versions.length}</summary><div className="flow-history">{versions.map(v=><button disabled={busy} key={v.version} onClick={()=>void open(v.id,v.version)}>查看 v{v.version}</button>)}</div></details>
    </article>)}</div>
    {!ready&&!listError&&<p role="status">正在读取流程…</p>}
    {ready&&!groups.length&&!listError&&<div className="flow-empty"><h2>{query?'没有匹配的流程':'从一个简单流程开始'}</h2><p>{query?'试试其他名称或流程 ID。':'先添加一个工具节点，再选择已接入能力；参数表单会自动出现。'}</p></div>}
    <p className="flow-footnote">发布固定工具契约；执行权限、审批与预算仍由服务端裁定。</p>
  </section>;
  return <section className="flow-editor"><header className="flow-list-heading"><div><button className="flow-back" onClick={()=>setEditing(false)}>← 返回流程列表</button><h1>{name||'未命名流程'}</h1>
    <p>{dirty||version===null?'未发布草稿':`已发布版本 v${version}`} · {nodes.length} 个节点</p></div>
    <div className="flow-main-actions"><button className="flow-primary" onClick={publish} disabled={busy||!nodes.length}>发布{version?'新版本':''}</button><button onClick={start} disabled={busy||!version||!catalog?.engine_enabled} title={catalog?.engine_enabled?'运行已发布版本':'需要连接可用的 Temporal 工作流服务'}>运行</button></div></header>
    <div className="flow-toolbar"><label>名称<input value={name} onChange={e=>{setName(e.target.value);setVersion(null);setDirty(true);}}/></label>
      <details className="flow-file-tools"><summary>流程标识与导入导出</summary><label>流程 ID<input value={id} onChange={e=>{setId(e.target.value);setVersion(null);setDirty(true);}}/></label>
        <div className="flow-history"><button onClick={exportFile}>导出草稿</button><label className="flow-import">导入草稿<input type="file" aria-label="导入流程" accept="application/json,.json" onChange={e=>{const f=e.target.files?.[0];e.target.value='';if(f)void act(async()=>{if(await canReplace()){load(JSON.parse(await f.text()).definition);setDirty(true);setNotice('草稿已导入，检查后发布。');}});}}/></label></div></details>
      <span className="flow-engine">{catalog?.engine_enabled?'Temporal 已连接':'工作流服务未就绪'}</span></div>
    <div className="flow-notice" role="status">{notice}</div>
    <div className="flow-toolbar flow-node-tools"><span>添加节点</span>{(Object.keys(labels) as Kind[]).map(k=><button key={k} onClick={()=>add(k)}>＋{labels[k]}</button>)}</div>
    <div className="flow-layout"><div className="flow-canvas"><ReactFlow nodes={nodes} edges={edges} onNodesChange={changes=>{onNodesChange(changes);if(changes.some(c=>c.type==='remove')){setVersion(null);setDirty(true);}}}
      onEdgesChange={changes=>{onEdgesChange(changes);if(changes.some(c=>c.type==='remove'||c.type==='add')){setVersion(null);setDirty(true);}}} onConnect={connect} onNodeClick={(_,n)=>choose(n)} fitView fitViewOptions={{maxZoom:1}} deleteKeyCode={['Backspace','Delete']}>
      <Background color="var(--line-strong)"/><Controls/><MiniMap nodeColor="var(--accent)" maskColor="var(--panel)"/></ReactFlow></div>
      <aside className="flow-config">{selected?<><h2>节点配置</h2><label>节点名称<input value={String(chosen?.data.label||'')} onChange={e=>{setNodes(ns=>ns.map(n=>n.id===selected?{...n,data:{...n.data,label:e.target.value}}:n));setVersion(null);setDirty(true);}}/></label>
        {chosen?.data.kind==='agent'&&<><label>任务指令<textarea value={String(cfg.prompt||'')} onChange={e=>edit('prompt',e.target.value)}/></label><label>使用工具（逗号分隔）<input value={(cfg.tools as string[]||[]).join(',')} onChange={e=>edit('tools',e.target.value.split(',').map(v=>v.trim()).filter(Boolean))}/></label></>}
        {chosen?.data.kind==='tool'&&<><label>工具能力<select value={String(cfg.tool||'')} onChange={e=>{
          const config={tool:e.target.value,arguments:{}};
          setNodes(ns=>ns.map(n=>n.id===selected?{...n,data:{...n.data,config}}:n));setDraft(JSON.stringify(config,null,2));setVersion(null);setDirty(true);
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
        <details><summary>高级配置与输入映射</summary><p>节点引用：{selected}</p><textarea aria-label="节点配置" value={draft} onChange={e=>{setDraft(e.target.value);setVersion(null);setDirty(true);}}/><button onClick={apply}>应用高级配置</button></details>
        <label>条件分支<select value={branch} onChange={e=>setBranch(e.target.value)}><option value="always">无条件</option>{edges.filter(e=>e.target===selected&&nodes.find(n=>n.id===e.source)?.data.kind==='condition').flatMap(e=>[<option key={e.source+'t'} value={e.source+':true'}>{e.source} 为真</option>,<option key={e.source+'f'} value={e.source+':false'}>{e.source} 为假</option>])}</select></label>
        <button onClick={()=>{const v=branch.split(':');setConditions(c=>({...c,[selected]:branch==='always'?{}:{[v[0]]:v[1]==='true'}}));setVersion(null);setDirty(true);}}>应用分支</button>
        <button onClick={()=>{setNodes(ns=>ns.filter(n=>n.id!==selected));setEdges(es=>es.filter(e=>e.source!==selected&&e.target!==selected));setSelected(null);setVersion(null);setDirty(true);}}>删除节点</button></>:<p>选择节点编辑配置。连线表示依赖，条件节点可控制后续分支。</p>}
      <details><summary>运行输入（高级）</summary><textarea aria-label="运行输入" value={inputs} onChange={e=>setInputs(e.target.value)}/><p>使用 {"{\"$ref\":\"inputs.field\"}"} 引用输入，或 nodes.节点ID 引用已声明依赖的输出。</p></details>
      <details><summary>输入校验（高级）</summary><textarea aria-label="输入校验" value={schema} onChange={e=>{setSchema(e.target.value);setVersion(null);setDirty(true);}}/></details></aside></div>
  </section>;
}
export function mount(container:HTMLElement, props:Props){const root=createRoot(container);root.render(<Editor {...props}/>);return ()=>root.unmount();}
