import {Component, useState, useCallback, useEffect, useRef, type ReactNode} from 'react';
import Form from '@rjsf/core';
import validator from '@rjsf/validator-ajv8';
import type {RJSFSchema} from '@rjsf/utils';
import {createRoot} from 'react-dom/client';
import {ReactFlow, Background, Controls, MiniMap, addEdge, useNodesState, useEdgesState, type Node, type Edge, type Connection} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import './editor.css';
import {DraftSession} from './drafts';

type Kind = 'agent'|'tool'|'condition'|'wait'|'custom';
type FlowNode = {id:string; kind:Kind; label:string; depends_on:string[]; config:Record<string,unknown>; when:Record<string,boolean>};
type Graph = {id:string; name:string; nodes:FlowNode[]; input_schema:Record<string,unknown>};
type Props = {request:(path:string, body?:unknown)=>Promise<any>; navigate:(hash:string)=>void;
  confirm:(message:string)=>Promise<boolean>; registerGuard?:(guard:(()=>Promise<boolean>)|null)=>void};
const configs: Record<Kind, Record<string,unknown>> = {
  agent:{prompt:'根据输入完成任务',tools:[],inputs:{$ref:'inputs'}},
  tool:{tool:'',arguments:{}}, condition:{path:'inputs.approved',operator:'eq',value:true},
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
function ToolArguments({spec,args,selected,change}:{spec:RJSFSchema;args:Record<string,unknown>;selected:string;change:(value:unknown)=>void}) {
  const mapped=Object.fromEntries(Object.entries(args).filter(([,v])=>hasMapping(v)));
  const properties=Object.fromEntries(Object.entries(spec.properties||{}).filter(([k])=>!(k in mapped)));
  const schema=Object.keys(mapped).length?{...spec,properties,required:(spec.required||[]).filter(k=>!(k in mapped))}:spec;
  const data=Object.fromEntries(Object.entries(args).filter(([k])=>!(k in mapped)));
  return <><Form schema={schema} validator={validator} formData={data} idPrefix={'args-'+selected} liveValidate
    uiSchema={{'ui:submitButtonOptions':{norender:true}}} onChange={e=>change({...mapped,...e.formData})}><span/></Form>
    {Object.keys(mapped).length>0&&<p>已绑定参数从下方数据来源取值，其他参数仍可直接填写。</p>}</>;
}
function InputFields({schema,change}:{schema:string;change:(value:string)=>void}) {
  const [key,setKey]=useState('');const [type,setType]=useState('string');const [required,setRequired]=useState(false);
  let parsed:any;try{parsed=JSON.parse(schema);}catch{return null;}
  if(!parsed||typeof parsed!=='object'||Array.isArray(parsed)||
    (parsed.properties&&(typeof parsed.properties!=='object'||Array.isArray(parsed.properties)))||
    (parsed.required&&!Array.isArray(parsed.required)))return <p>输入定义格式有误，请使用高级配置修正。</p>;
  if(parsed.type&&parsed.type!=='object')return <p>复杂校验定义请使用高级配置。</p>;
  const add=()=>{
    if(!/^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(key)||parsed.properties?.[key])return;
    change(JSON.stringify({...parsed,type:'object',properties:{...parsed.properties,[key]:{type,title:key}},
      required:Array.from(new Set([...(parsed.required||[]),...(required?[key]:[])]))},null,2));setKey('');
  };
  return <fieldset><legend>流程输入字段</legend>
    {Object.keys(parsed.properties||{}).map(k=><p key={k}>{k} · {parsed.properties[k].type||'复杂类型'} {parsed.required?.includes(k)?'必填':''}
      <button onClick={()=>{const properties={...parsed.properties};delete properties[k];change(JSON.stringify({...parsed,properties,required:(parsed.required||[]).filter((v:string)=>v!==k)}));}}>删除字段</button></p>)}
    <label>新字段名称<input value={key} onChange={e=>setKey(e.target.value)} placeholder="例如 topic"/></label>
    <label>类型<select value={type} onChange={e=>setType(e.target.value)}><option value="string">文本</option><option value="number">数字</option><option value="boolean">是 / 否</option></select></label>
    <label><input type="checkbox" checked={required} onChange={e=>setRequired(e.target.checked)}/>必填</label>
    <button disabled={!/^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(key)||Boolean(parsed.properties?.[key])} onClick={add}>添加输入字段</button>
  </fieldset>;
}
function Editor({request,navigate,confirm,registerGuard}:Props) {
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
  const canEdit=catalog?.can_edit!==false;
  const [conditions,setConditions]=useState<Record<string,Record<string,boolean>>>({});
  const [branch,setBranch]=useState('always');
  const [drafts,setDrafts]=useState<any[]>([]);
  const [,setSaveTick]=useState(0);
  const draftSession=useRef<DraftSession|null>(null);
  const snapshotRef=useRef<Record<string,unknown>>({});
  const [mappingField,setMappingField]=useState('');
  const [mappingRef,setMappingRef]=useState('');
  const submission=useRef<{signature:string;key:string}|null>(null);
  const snapshot={id,name,nodes:nodes.map(n=>({id:n.id,position:n.position,data:n.data})),
    edges:edges.map(e=>({id:e.id,source:e.source,target:e.target})),conditions,schema,inputs,
    version,dirty,selected,draft,submission:submission.current};
  snapshotRef.current=snapshot;
  const saveState=draftSession.current;
  const unsaved=Boolean(saveState&&saveState.saved!==JSON.stringify(snapshot));
  const persist=async()=>{if(draftSession.current)await draftSession.current.flush(snapshotRef.current);};
  useEffect(()=>{
    if(!draftSession.current||!draftStarted||draftSession.current.error)return;
    const timer=setTimeout(()=>void persist().catch(()=>{}),650);
    return ()=>clearTimeout(timer);
  },[id,name,nodes,edges,conditions,schema,inputs,version,dirty,selected,draft,draftStarted]);
  useEffect(()=>{
    registerGuard?.(async()=>{try{await persist();return true;}catch{
      setNotice('草稿尚未确认保存，已保留当前页面。请重试或导出后再离开。');return false;
    }});
    const warn=(e:BeforeUnloadEvent)=>{
      const session=draftSession.current;
      if(session&&session.saved!==JSON.stringify(snapshotRef.current)){e.preventDefault();e.returnValue='';}
    };
    window.addEventListener('beforeunload',warn);
    return ()=>{registerGuard?.(null);window.removeEventListener('beforeunload',warn);void persist().catch(()=>{});};
  },[]);
  const newDraftSession=()=>{draftSession.current=canEdit?new DraftSession('draft-'+crypto.randomUUID().slice(0,24),0,null,request,()=>setSaveTick(n=>n+1)):null;};
  const loadSnapshot=(p:any)=>{
    if(!p||!Array.isArray(p.nodes)||!Array.isArray(p.edges)||typeof p.id!=='string'||typeof p.name!=='string')throw Error('草稿格式无法读取，可下载数据检查。');
    setId(p.id);setName(p.name);setNodes(p.nodes);setEdges(p.edges);setConditions(p.conditions||{});
    setSchema(p.schema||'{}');setInputs(p.inputs||'{}');setVersion(p.version);setDirty(Boolean(p.dirty));
    setSelected(p.selected||null);setDraft(p.draft||'{}');submission.current=p.submission||null;
  };
  const restore=async(key:string)=>{
    await act(async()=>{
      if(draftSession.current?.error&&draftSession.current.id===key){
        if(!await confirm('重新读取会替换当前本地修改。请先导出需要保留的内容，是否继续？'))return;
      }else await persist();
      const stored=await request('/v1/workflow-drafts/'+encodeURIComponent(key));
      const p=stored.payload;
      loadSnapshot(p);
      draftSession.current=canEdit?new DraftSession(key,stored.revision,p,request,()=>setSaveTick(n=>n+1)):null;
      setDraftStarted(true);setEditing(true);setNotice('已恢复服务端草稿和画布位置；发布版本保持独立。');
    });
  };
  const chosen=nodes.find(n=>n.id===selected);
  const cfg=(chosen?.data.config||{}) as Record<string,unknown>;
  const tool=(catalog?.tools as Capability[]|undefined)?.find(t=>t.name===cfg.tool);
  const edit=(key:string,value:unknown)=>{const config={...cfg,[key]:value};setNodes(ns=>ns.map(n=>n.id===selected?{...n,data:{...n.data,config}}:n));setDraft(JSON.stringify(config,null,2));setVersion(null);setDirty(true);};
  const graph=():Graph=>({id,name,input_schema:JSON.parse(schema),nodes:nodes.map(n=>({id:n.id,
    label:String(n.data.label),kind:n.data.kind as Kind,config:n.data.config as Record<string,unknown>,
    depends_on:edges.filter(e=>e.target===n.id).map(e=>e.source),when:conditions[n.id]||{}}))});
  const act=async(fn:()=>Promise<void>)=>{setBusy(true);try{await fn();}catch(e){const message=e instanceof Error?e.message:'操作失败';setNotice(message);if(!editing)setListError(message);}finally{setBusy(false);}};
  const refresh=()=>act(async()=>{setCatalog(await request('/v1/workflows/catalog'));setSaved(await request('/v1/workflow-definitions'));setDrafts(await request('/v1/workflow-drafts'));setListError('');setReady(true);});
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
    void request('/v1/workflow-drafts').then(v=>{if(!stopped)setDrafts(v);}).catch(()=>{if(!stopped)setListError('草稿暂时无法读取，请刷新重试。');});
    const timer=window.setInterval(()=>void sync(),15000);
    const focus=()=>void sync();window.addEventListener('focus',focus);
    document.addEventListener('visibilitychange',focus);
    return ()=>{stopped=true;clearInterval(timer);window.removeEventListener('focus',focus);document.removeEventListener('visibilitychange',focus);};
  },[]);
  const load=(g:Graph)=>{setId(g.id);setName(g.name);setSchema(JSON.stringify(g.input_schema||{},null,2));
    setNodes(g.nodes.map((n,i)=>({id:n.id,position:{x:40+(i%3)*230,y:40+Math.floor(i/3)*150},data:{label:n.label||labels[n.kind],kind:n.kind,config:n.config}})));
    setEdges(g.nodes.flatMap(n=>n.depends_on.map(d=>({id:d+'-'+n.id,source:d,target:n.id}))));
    setConditions(Object.fromEntries(g.nodes.map(n=>[n.id,n.when||{}])));setSelected(null);setVersion(null);setDirty(false);setDraftStarted(true);};
  const add=(kind:Kind)=>{
    const nid='node-'+crypto.randomUUID().slice(0,8);const config=structuredClone(configs[kind]);
    if(kind==='tool') {
      const tools=(catalog?.tools as Capability[]|undefined)||[];
      const available=tools.filter(t=>t.available!==false&&(!t.name.startsWith('workflow.')||t.name==='workflow.status'));
      config.tool=(available.find(t=>t.name==='material.list')||available.find(t=>t.name==='clock.now')||available[0])?.name||'';
    }
    setNodes(ns=>[...ns,{id:nid,position:{x:40+ns.length%3*230,y:40+Math.floor(ns.length/3)*150},data:{label:labels[kind],kind,config}}]);
    setSelected(nid);setDraft(JSON.stringify(config,null,2));setMappingField('');setMappingRef('');setBranch('always');setVersion(null);setDirty(true);
  };
  const connect=useCallback((c:Connection)=>{setEdges(es=>addEdge(c,es));setVersion(null);setDirty(true);},[setEdges]);
  const choose=(n:Node)=>{setMappingField('');setMappingRef('');setSelected(n.id);setDraft(JSON.stringify(n.data.config,null,2));const w=conditions[n.id]||{};const first=Object.entries(w)[0];setBranch(first?first[0]+':'+first[1]:'always');};
  const apply=()=>{try{const config=JSON.parse(draft);if(!config||Array.isArray(config)||typeof config!=='object')throw Error('配置必须是对象');
    setNodes(ns=>ns.map(n=>n.id===selected?{...n,data:{...n.data,config}}:n));setVersion(null);setDirty(true);setNotice('节点配置已保存到草稿。');}catch(e){setNotice(String(e));}};
  const publish=()=>act(async()=>{if(selected&&draft!==JSON.stringify(cfg,null,2))throw Error('请先应用节点的高级配置，再发布。');const result=await request('/v1/workflows/'+encodeURIComponent(id)+'/publish',graph());setVersion(result.version);setDirty(false);setSaved(await request('/v1/workflow-definitions').catch(()=>saved));setNotice('已发布版本 '+result.version+'，运行使用此版本。');});
  const start=()=>act(async()=>{if(!version)throw Error('请先发布当前草稿');
    const parsed=JSON.parse(inputs);const signature=JSON.stringify([id,version,parsed]);
    if(submission.current?.signature!==signature)submission.current={signature,key:crypto.randomUUID()};
    snapshotRef.current={...snapshotRef.current,submission:submission.current};
    await persist();
    const result=await request('/v1/workflows/'+encodeURIComponent(id)+'/start',
      {version,inputs:parsed,idempotency_key:submission.current.key});
    navigate('#/tasks/'+result.internal_run_id);});
  const exportFile=()=>{try{const payload:Record<string,unknown>={editor_draft:snapshotRef.current};
    try{payload.definition=graph();}catch{/* Incomplete schema stays recoverable in the private editor snapshot. */}
    const url=URL.createObjectURL(new Blob([JSON.stringify(payload,null,2)],{type:'application/json'}));
    const a=document.createElement('a');a.href=url;a.download=id+'.json';document.body.appendChild(a);a.click();a.remove();
    setTimeout(()=>URL.revokeObjectURL(url),10000);
  }catch{setNotice('无法导出草稿，请重试。');}};
  const canReplace=async()=>{try{await persist();return !dirty||await confirm('当前修改已保存为独立草稿。是否打开其他流程？');}catch{setNotice('请先重试保存，或导出草稿，避免丢失修改。');return false;}};
  const open=async(flowId:string,flowVersion:number)=>{if(!await canReplace())return;
    await act(async()=>{const r=await request('/v1/workflows/'+encodeURIComponent(flowId)+'/versions/'+flowVersion);
      load(r.definition);newDraftSession();submission.current=null;setVersion(flowVersion);setInputs('{}');setEditing(true);setNotice('正在查看已发布版本 '+flowVersion+'；修改后需发布新版本。');});};
  const create=async()=>{if(!await canReplace())return;
    load({id:'flow-'+crypto.randomUUID().slice(0,8),name:'我的集成流程',nodes:[],input_schema:{}});
    newDraftSession();submission.current=null;setInputs('{}');setEditing(true);setNotice('添加节点并连接。发布固定版本后才能运行。');};
  const groups=Array.from(new Set(saved.map(f=>f.id))).map(flowId=>{
    const versions=saved.filter(f=>f.id===flowId).sort((a,b)=>b.version-a.version);
    return {latest:versions[0],versions};
  }).filter(g=>(g.latest.name+' '+g.latest.id).toLowerCase().includes(query.trim().toLowerCase()));
  if(!editing)return <section className="flow-editor flow-list">
    <header className="flow-list-heading"><div><h1>工作流</h1><p>把已有能力连接成流程，发布后可重复运行。</p></div>
      <button className="flow-primary" disabled={busy||!catalog||!canEdit} onClick={()=>void create()}>新建流程</button></header>
    <div className="flow-list-tools"><input type="search" aria-label="搜索流程" placeholder="搜索名称或流程 ID…" value={query} onChange={e=>setQuery(e.target.value)}/>
      <button onClick={refresh} disabled={busy}>刷新能力与流程</button>
      {draftStarted&&<button onClick={()=>setEditing(true)}>继续当前{dirty?'草稿':'流程'}</button>}</div>
    <p className="flow-engine">{catalog?.engine_enabled?'工作流服务已连接':catalog?.engine_status==='unavailable'?'工作流服务暂不可用，仍可编辑草稿':'按需连接 Temporal 后可运行流程'}</p>
    {listError&&<p role="alert" className="flow-notice">{listError}</p>}
    {drafts.length>0&&<><h2>我的草稿</h2><div className="flow-cards">{drafts.map(d=><article className="flow-card" key={d.id}>
      <h3>{d.payload.name||'未命名草稿'}</h3><p>已保存 · {new Date(d.updated_at).toLocaleString()}</p>
      <button disabled={busy} onClick={()=>void restore(d.id)}>恢复草稿</button>
      <button disabled={busy||draftSession.current?.id===d.id} onClick={()=>void act(async()=>{
        if(!await confirm('删除此草稿？已发布版本不受影响。'))return;
        await request('/v1/workflow-drafts/'+encodeURIComponent(d.id)+'/delete',{expected_revision:d.revision});
        setDrafts(await request('/v1/workflow-drafts'));
      })}>删除草稿</button></article>)}</div></>}
    <h2>已发布流程</h2><div className="flow-cards">{groups.map(({latest,versions})=><article className="flow-card" key={latest.id}>
      <div><h2>{latest.name}</h2><span className="flow-version">已发布 · v{latest.version}</span></div>
      <p>流程 ID：{latest.id}</p><button className="flow-primary" disabled={busy} onClick={()=>void open(latest.id,latest.version)}>打开流程</button>
      <details><summary>历史版本 · {versions.length}</summary><div className="flow-history">{versions.map(v=><button disabled={busy} key={v.version} onClick={()=>void open(v.id,v.version)}>查看 v{v.version}</button>)}</div></details>
    </article>)}</div>
    {!ready&&!listError&&<p role="status">正在读取流程…</p>}
    {ready&&!groups.length&&!listError&&<div className="flow-empty"><h2>{query?'没有匹配的流程':'从一个简单流程开始'}</h2><p>{query?'试试其他名称或流程 ID。':'先添加一个工具节点，再选择已接入能力；参数表单会自动出现。'}</p></div>}
    <p className="flow-footnote">发布固定工具契约；执行权限、审批与预算仍由服务端裁定。</p>
  </section>;
  return <section className="flow-editor"><header className="flow-list-heading"><div><button className="flow-back" onClick={()=>void act(async()=>{await persist();setDrafts(await request('/v1/workflow-drafts'));setEditing(false);})}>← 返回流程列表</button><h1>{name||'未命名流程'}</h1>
    <p>{dirty||version===null?'未发布草稿':`已发布版本 v${version}`} · {nodes.length} 个节点</p></div>
    <div className="flow-main-actions"><button className="flow-primary" onClick={publish} disabled={busy||!canEdit||!nodes.length}>发布{version?'新版本':''}</button><button onClick={start} disabled={busy||!canEdit||!version||!catalog?.engine_enabled} title={catalog?.engine_enabled?'运行已发布版本':'需要连接可用的 Temporal 工作流服务'}>运行</button></div></header>
    {!canEdit&&<p>当前权限为只读，可以查看已发布版本。</p>}
    <fieldset disabled={!canEdit} className="flow-content">
    <div className="flow-toolbar"><label>名称<input value={name} onChange={e=>{setName(e.target.value);setVersion(null);setDirty(true);}}/></label>
      <details className="flow-file-tools"><summary>流程标识与导入导出</summary><label>流程 ID<input value={id} onChange={e=>{setId(e.target.value);setVersion(null);setDirty(true);}}/></label>
        <div className="flow-history"><button onClick={exportFile}>导出草稿</button><label className="flow-import">导入草稿<input type="file" aria-label="导入流程" accept="application/json,.json" onChange={e=>{const f=e.target.files?.[0];e.target.value='';if(f)void act(async()=>{if(await canReplace()){const imported=JSON.parse(await f.text());if(imported.editor_draft){loadSnapshot(imported.editor_draft);setVersion(null);setDraftStarted(true);}else load(imported.definition);newDraftSession();submission.current=null;setDirty(true);setNotice('草稿已导入，检查后发布。');}});}}/></label></div></details>
      <span className="flow-engine">{catalog?.engine_enabled?'Temporal 已连接':'工作流服务未就绪'}</span></div>
    <div className="flow-notice" role="status">{notice}</div>
    <div className="flow-toolbar"><span role="status">{saveState?.error|| (saveState?.running?'正在保存草稿…':unsaved?'草稿待保存':'草稿已保存')}</span>
      <button disabled={busy} onClick={()=>void act(persist)}>保存草稿 / 重试</button></div>
    <div className="flow-toolbar flow-node-tools"><span>添加节点</span>{(Object.keys(labels) as Kind[]).map(k=><button key={k} onClick={()=>add(k)}>＋{labels[k]}</button>)}</div>
    <div className="flow-layout"><div className="flow-canvas"><ReactFlow nodes={nodes} edges={edges} onNodesChange={changes=>{onNodesChange(changes);if(changes.some(c=>c.type==='remove')){setVersion(null);setDirty(true);}}}
      onEdgesChange={changes=>{onEdgesChange(changes);if(changes.some(c=>c.type==='remove'||c.type==='add')){setVersion(null);setDirty(true);}}} onConnect={connect} onNodeClick={(_,n)=>choose(n)} nodesDraggable={canEdit} nodesConnectable={canEdit} fitView fitViewOptions={{maxZoom:1}} deleteKeyCode={canEdit?['Backspace','Delete']:null}>
      <Background color="var(--line-strong)"/><Controls/><MiniMap nodeColor="var(--accent)" maskColor="var(--panel)"/></ReactFlow></div>
      <aside className="flow-config">{selected?<><h2>节点配置</h2><label>节点名称<input value={String(chosen?.data.label||'')} onChange={e=>{setNodes(ns=>ns.map(n=>n.id===selected?{...n,data:{...n.data,label:e.target.value}}:n));setVersion(null);setDirty(true);}}/></label>
        {chosen?.data.kind==='agent'&&<><label>任务指令<textarea value={String(cfg.prompt||'')} onChange={e=>edit('prompt',e.target.value)}/></label><label>使用工具（逗号分隔）<input value={(cfg.tools as string[]||[]).join(',')} onChange={e=>edit('tools',e.target.value.split(',').map(v=>v.trim()).filter(Boolean))}/></label></>}
        {chosen?.data.kind==='tool'&&<><label>工具能力<select value={String(cfg.tool||'')} onChange={e=>{
          const config={tool:e.target.value,arguments:{}};
          setNodes(ns=>ns.map(n=>n.id===selected?{...n,data:{...n.data,config}}:n));setDraft(JSON.stringify(config,null,2));setVersion(null);setDirty(true);
        }}>
          {!tool&&<option value={String(cfg.tool||'')}>{String(cfg.tool||'请选择能力')}（未发现）</option>}
          {(catalog?.tools as Capability[]|undefined)?.map(t=><option key={t.name} value={t.name} disabled={t.available===false||(t.name.startsWith('workflow.')&&t.name!=='workflow.status')}>{t.name}{t.available===false?' · 暂不可用':''}</option>)}
        </select></label>
        {tool?<><p>{tool.description}</p><p className="flow-cap-meta">版本 {tool.version||'未记录'} · 风险 {tool.risks?.join('、')||'未声明'} · {tool.discovery_status==='unavailable'?'发现失败':tool.discovery_status==='local'?'本地能力':'缓存发现结果，调用时核验'}</p>
          {Boolean((cfg.capability_bindings as Record<string,string>|undefined)?.[tool.name]&&
            (cfg.capability_bindings as Record<string,string>)[tool.name]!==tool.revision)&&<p role="alert">工具契约已变化，旧发布版本会阻止执行；请检查参数并重新发布。</p>}
          {!tool.input_schema?<p>当前服务尚未提供参数定义，请使用高级配置。</p>:
            <FormBoundary key={tool.name+tool.revision+selected}><ToolArguments spec={tool.input_schema}
              args={(cfg.arguments||{}) as Record<string,unknown>} selected={selected}
              change={value=>edit('arguments',value)}/></FormBoundary>}
        </>:<p role="alert">此能力未接入、已停用或不在当前权限范围，请选择可用能力。</p>}</>}
        {chosen?.data.kind==='wait'&&<label>等待秒数<input type="number" min="0" max="2592000" value={Number(cfg.seconds||0)} onChange={e=>edit('seconds',Number(e.target.value))}/></label>}
        {chosen?.data.kind==='condition'&&<><label>判断字段<input value={String(cfg.path||'')} onChange={e=>edit('path',e.target.value)}/></label><label>判断方式<select value={String(cfg.operator||'eq')} onChange={e=>edit('operator',e.target.value)}><option value="eq">等于</option><option value="ne">不等于</option><option value="contains">包含</option><option value="exists">存在</option></select></label><label>比较值<input value={String(cfg.value??'')} onChange={e=>{let value:unknown=e.target.value;try{value=JSON.parse(e.target.value);}catch{}edit('value',value);}}/></label></>}
        {chosen?.data.kind==='custom'&&<label>已注册能力<select value={String(cfg.adapter||'passthrough')} onChange={e=>edit('adapter',e.target.value)}>{(catalog?.custom_nodes||['passthrough']).map((v:string)=><option key={v} value={v}>{v}</option>)}</select></label>}
        {chosen?.data.kind==='tool'&&tool&&<fieldset><legend>从流程输入或上游节点取值</legend>
          <label>目标参数<select value={mappingField} onChange={e=>setMappingField(e.target.value)}><option value="">选择参数</option>
            {Object.keys(tool.input_schema?.properties||{}).map(k=><option key={k}>{k}</option>)}</select></label>
          <label>数据来源<select value={mappingRef} onChange={e=>setMappingRef(e.target.value)}><option value="">选择来源</option>
            {(()=>{try{return Object.keys(JSON.parse(schema).properties||{}).map(k=><option key={k} value={'inputs.'+k}>流程输入 · {k}</option>);}catch{return [];}})()}
            {edges.filter(e=>e.target===selected).map(e=><option key={e.source} value={'nodes.'+e.source}>上游节点 · {String(nodes.find(n=>n.id===e.source)?.data.label||e.source)}</option>)}
          </select></label>
          <label>输出中的字段（选填）<input id="mapping-path" placeholder="例如 sum；留空使用完整输出"/></label>
          <button disabled={!mappingField||!mappingRef} onClick={()=>{
            const path=(document.getElementById('mapping-path') as HTMLInputElement)?.value.trim();
            if(path&&!/^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$/.test(path)){setNotice('字段路径格式有误');return;}
            edit('arguments',{...(cfg.arguments as object||{}),[mappingField]:{$ref:mappingRef+(path?'.'+path:'')}});
          }}>绑定参数</button>
          {Object.entries((cfg.arguments||{}) as Record<string,unknown>).filter(([,v])=>hasMapping(v)).map(([k,v])=><p key={k}>{k} ← {String((v as any).$ref||'嵌套映射')}
            <button onClick={()=>{const a={...(cfg.arguments as Record<string,unknown>)};delete a[k];edit('arguments',a);}}>取消绑定</button></p>)}
        </fieldset>}
        <details><summary>高级配置与输入映射</summary><p>节点引用：{selected}</p><textarea aria-label="节点配置" value={draft} onChange={e=>{setDraft(e.target.value);setVersion(null);setDirty(true);}}/><button onClick={apply}>应用高级配置</button></details>
        <label>条件分支<select value={branch} onChange={e=>setBranch(e.target.value)}><option value="always">无条件</option>{edges.filter(e=>e.target===selected&&nodes.find(n=>n.id===e.source)?.data.kind==='condition').flatMap(e=>[<option key={e.source+'t'} value={e.source+':true'}>{e.source} 为真</option>,<option key={e.source+'f'} value={e.source+':false'}>{e.source} 为假</option>])}</select></label>
        <button onClick={()=>{const v=branch.split(':');setConditions(c=>({...c,[selected]:branch==='always'?{}:{[v[0]]:v[1]==='true'}}));setVersion(null);setDirty(true);}}>应用分支</button>
        <button onClick={()=>{setNodes(ns=>ns.filter(n=>n.id!==selected));setEdges(es=>es.filter(e=>e.source!==selected&&e.target!==selected));setSelected(null);setVersion(null);setDirty(true);}}>删除节点</button></>:<p>选择节点编辑配置。连线表示依赖，条件节点可控制后续分支。</p>}
      <h2>运行输入</h2>{(()=>{try{const parsed=JSON.parse(schema);if(!Object.keys(parsed.properties||{}).length)return <p>尚未定义输入字段；可在下方添加。</p>;
        return <FormBoundary key={schema}><Form schema={parsed} validator={validator} formData={JSON.parse(inputs)}
          uiSchema={{'ui:submitButtonOptions':{norender:true}}} onChange={e=>setInputs(JSON.stringify(e.formData||{}))}><span/></Form></FormBoundary>;
      }catch{return <p role="alert">输入或校验定义格式有误，请在高级配置中修正。</p>;}})()}
      <InputFields schema={schema} change={value=>{setSchema(value);setVersion(null);setDirty(true);}}/>
      <details><summary>运行输入（高级）</summary><textarea aria-label="运行输入" value={inputs} onChange={e=>setInputs(e.target.value)}/><p>使用 {"{\"$ref\":\"inputs.field\"}"} 引用输入，或 nodes.节点ID 引用已声明依赖的输出。</p></details>
      <details><summary>输入校验（高级）</summary><textarea aria-label="输入校验" value={schema} onChange={e=>{setSchema(e.target.value);setVersion(null);setDirty(true);}}/></details></aside></div>
    </fieldset></section>;
}
export function mount(container:HTMLElement, props:Props){const root=createRoot(container);root.render(<Editor {...props}/>);return ()=>root.unmount();}
