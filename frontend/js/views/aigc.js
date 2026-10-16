/* Local production projects: save, generate candidates, review, and compose selected shots. */
"use strict";
import {closeFacePickers, pickFaceRegion} from "./aigc-face-picker.js";
import { openPreview } from "../preview.js";
import { esc } from "../safe-export.js";
import { confirmDialog, icon, toast } from "../ui.js";
import {directorPanel, enableProduction, productionPanel} from "./aigc-production.js";

const ROLE = {lipsync_video:"口型候选",keyframe:"分镜参考图",animatic:"动态分镜粗剪",raw_video:"镜头原片",alpha_frames:"透明帧",playback_video:"桌宠播放素材",film:"成片",speech:"配音",subtitles:"字幕",preview:"预览图",pet_mask:"命中遮罩",pet_manifest:"角色定位",pet_bundle:"桌宠素材包",production_record:"制作记录",comfy_workflow:"生成配方"};
const artifactName = a => (ROLE[a.role] || a.role || "素材") + (String(a.asset_ref || "").match(/\.[a-z0-9]{1,8}$/i)?.[0] || "");
const STATE = {created:"已受理",running:"正在制作",completed:"已完成",succeeded:"已完成",awaiting_resources:"排队中",awaiting_approval:"等待确认",uncertain:"需要核对",failed:"失败",paused:"已暂停",cancelled:"已取消"};
export async function renderAigc(ctx) {
  const {el, api} = ctx;
  const request = api.workflowRequest;
  const [catalog, projects, media] = await Promise.all([request("/v1/aigc/catalog"), request("/v1/aigc/projects"), api.listAssets()]);
  let stopped = false, dirty = false, current = null, runs = [], busy = false;
  const nodes = [...new Set(catalog.tools.filter(t=>t.name.endsWith("_aigc_generate")).map(t=>t.name.slice(5,-14)))];
  let selected = ctx.params[0] ? projects.find(p=>p.id===ctx.params[0]) : projects[0];
  if (selected) current = structuredClone(selected);
  const assetOptions = (kind, id) => `<option value="">选择${kind==="image"?"参考图片":kind==="audio"?"音频":"视频"}</option>` + media.filter(a=>String(a.mime).startsWith(kind+"/")).map(a=>`<option value="${esc(a.id)}" ${a.id===id?"selected":""}>${esc(a.name||a.id)}</option>`).join("");
  el.innerHTML = `<section class="page aigc-page"><div class="page-heading"><div><h2>AIGC 制作</h2><p>从角色与镜头开始，保存每次候选，选片后再合成。</p></div><button class="btn primary" id="aigc-new">${icon("plus",16)} 新建项目</button></div>
    <div class="aigc-layout"><aside class="aigc-projects panel"><h3>我的项目</h3><div id="aigc-project-list"></div><p class="side-caption">桌宠动作 · 短剧镜头<br>沿用 Windows 本地生成能力</p></aside><div id="aigc-editor"></div></div></section>`;
  function drawList() {
    el.querySelector("#aigc-project-list").innerHTML = projects.map(p=>`<button class="aigc-project ${current?.id===p.id?"active":""}" data-project="${esc(p.id)}"><b>${esc(p.payload.name)}</b><small>${p.payload.kind==="pet"?"桌宠":"短剧"} · 版本 ${p.revision}</small></button>`).join("") || '<p class="muted">还没有项目</p>';
    el.querySelectorAll("[data-project]").forEach(b=>b.onclick=async()=>{if(dirty&&!await confirmDialog({title:"切换项目",body:"当前修改尚未保存，是否放弃？",confirmText:"放弃修改"}))return;current=structuredClone(projects.find(p=>p.id===b.dataset.project));dirty=false;runs=[];draw();await loadRuns();});
  }
  async function upload(file, shot = null, audioField = null) {
    if(!file)return;
    const mime = file.type || (/\.wav$/i.test(file.name)?"audio/wav":/\.mp3$/i.test(file.name)?"audio/mpeg":"");
    const a = await api.uploadAigc(file,mime);
    media.push({id:a.asset_id,mime,name:file.name});
    if(shot){shot.input_asset_id=a.asset_id;shot.keyframe_review="pending";if(current.payload.production)current.payload.production.animatic_review="pending";shot.review="pending";shot.selected_asset_id=null;shot.selected_run_id=null;}
    if(audioField)current.payload[audioField]=a.asset_id;
    dirty=true;draw();
  }
  function draw() {
    if(stopped)return;
    drawList();
    const target=el.querySelector("#aigc-editor");
    if(!current){target.innerHTML='<div class="empty-state"><div class="title">开始一段本地创作</div><div class="desc">新建桌宠或短剧项目，上传参考图，填写每段动作或镜头。</div></div>';return;}
    const p=current.payload;
    target.innerHTML=`<div class="panel aigc-form"><div class="row"><span class="tag">${p.kind==="pet"?"桌宠动作":"短剧"}</span><span class="muted">${current.revision?"已保存版本 "+current.revision:"尚未保存"}${dirty?" · 有修改":""}</span></div>
      <label>项目名称<input id="aigc-name" value="${esc(p.name)}" maxlength="120"></label>
      <div class="aigc-fields"><label>执行机器<select id="aigc-node">${nodes.map(n=>`<option ${n===p.node_id?"selected":""}>${esc(n)}</option>`).join("")}</select></label><label>宽度<input id="aigc-width" type="number" min="256" max="1024" step="16" value="${p.width}"></label><label>高度<input id="aigc-height" type="number" min="256" max="1024" step="16" value="${p.height}"></label><label>帧率<input id="aigc-fps" type="number" min="1" max="30" value="${p.fps}"></label></div>
      ${!catalog.engine_enabled?'<p class="error-state">工作流引擎暂不可用，可以编辑和保存项目。</p>':!nodes.length?'<p class="error-state">Windows 制作能力未连接，请到能力中心查看节点。</p>':''}
      ${p.kind==="pet"?`<details><summary>角色固定定位</summary><p class="muted">扩充已有桌宠时填写母版中的身体高度、中心与地面位置，让每段动作沿用同一比例。留空则生成待审查的定位建议。</p><div class="aigc-fields">${[["bodyHeight","身体高度"],["anchorX","水平中心"],["floor","地面位置"]].map(([k,label])=>`<label>${label}<input type="number" data-layout="${k}" min="0" max="${k==="anchorX"?p.width:p.height}" value="${p.pet_layout?.[k]??""}"></label>`).join("")}</div></details>`:""}
      ${productionPanel(p,assetOptions)}
      <div class="row"><h3>${p.kind==="pet"?"动作清单":"分镜清单"}</h3><button class="btn small" id="aigc-add">增加一段</button></div><div id="aigc-shots">${p.shots.map((s,i)=>`<article class="aigc-shot" data-shot="${i}"><div class="row"><b>${i+1}. ${esc(s.label||"未命名")}</b><span class="tag">${s.review==="accepted"?"已选用":s.review==="rejected"?"需重做":"待审查"}</span><button class="btn small" data-remove="${i}">移除</button></div>
        <div class="aigc-fields"><label>名称<input data-field="label" value="${esc(s.label)}" maxlength="80"></label><label>角色<input data-field="role" value="${esc(s.role||"")}" maxlength="80"></label></div>
        <label>场景<input data-field="scene" value="${esc(s.scene||"")}" maxlength="200"></label>
        <div class="aigc-fields"><label>参考图<select data-field="input_asset_id">${assetOptions("image",s.input_asset_id)}</select></label><label>上传图片<input type="file" data-upload="${i}" accept="image/png,image/jpeg,image/webp"></label></div>
        ${directorPanel(p,s)}
        <label>动作或镜头描述<textarea data-field="prompt" rows="3" maxlength="4000">${esc(s.prompt)}</textarea></label>
        <details><summary>生成参数</summary><div class="aigc-fields"><label>随机种子<input type="number" data-field="seed" min="0" value="${s.seed}"></label><label>原生帧数<input type="number" data-field="frames" min="5" max="81" step="4" value="${s.frames}"></label></div><label>避免的画面<textarea data-field="negative" maxlength="4000">${esc(s.negative)}</textarea></label></details>
        <div class="row"><span class="muted">约 ${(s.frames/p.fps).toFixed(1)} 秒 · 原生生成${s.duration?" · 剪辑 "+s.duration+" 秒":""}</span><button class="btn small" data-generate="${esc(s.id)}">单独生成候选</button></div>
        ${s.selected_asset_id?`<button class="btn small" data-preview="${esc(s.selected_asset_id)}">预览已选片段</button>`:""}</article>`).join("")}</div>
      ${p.kind==="story"?`<details class="aigc-audio"><summary>配音与字幕</summary><div class="aigc-fields"><label>成片配音<select id="aigc-audio">${assetOptions("audio",p.audio_asset_id)}</select></label><label>上传配音<input type="file" id="aigc-audio-upload" accept="audio/wav,audio/mpeg"></label></div><label>旁白<textarea id="aigc-narration" maxlength="2000">${esc(p.narration||"")}</textarea></label><div class="aigc-fields"><label>参考声线<select id="aigc-voice">${assetOptions("audio",p.voice_asset_id)}</select></label><label>上传声线样本<input type="file" id="aigc-voice-upload" accept="audio/wav,audio/mpeg"></label></div><button class="btn small" id="aigc-speak" ${catalog.tools.some(t=>t.name===`node_${p.node_id}_aigc_speak`)?"":"disabled"}>${p.production?"生成分角色对白":"生成本地配音"}</button><p class="muted">字幕格式：开始秒数 | 结束秒数 | 字幕内容，每行一条。</p><textarea id="aigc-subtitles" rows="4" placeholder="0 | 3 | 故事从这里开始">${esc((p.subtitles||[]).map(s=>`${s.start} | ${s.end} | ${s.text}`).join("\n"))}</textarea></details>`:""}
      <div class="row aigc-actions"><button class="btn" id="aigc-save">保存项目</button>${p.production?'<button class="btn" id="aigc-export">导出 Win 本地制作项目</button>':""}<button class="btn primary" id="aigc-generate">生成全部候选</button>${p.kind==="story"?'<button class="btn" id="aigc-compose">合成已选镜头</button>':""}</div><p class="side-caption">每次提交固定项目版本；审查候选后选择使用。任务中断时到任务详情核对同一任务。</p></div><div class="panel aigc-results"><div class="row"><h3>候选与成片</h3><button class="btn small" id="aigc-refresh">刷新结果</button></div><div id="aigc-runs"></div></div>`;
    for(const field of ["name","node_id","width","height","fps"]){const id=field==="node_id"?"node":field;target.querySelector(`#aigc-${id}`).oninput=e=>{p[field]=["width","height","fps"].includes(field)?Number(e.target.value):e.target.value;dirty=true;if(field!=="name"){if(p.production)p.production.animatic_review="pending";p.shots.forEach(s=>{s.review="pending";s.selected_asset_id=null;s.selected_run_id=null;});}};}
    target.querySelectorAll("[data-layout]").forEach(input=>input.oninput=()=>{const values=[...target.querySelectorAll("[data-layout]")];p.pet_layout=values.every(v=>v.value==="")?null:Object.fromEntries(values.map(v=>[v.dataset.layout,v.value===""?null:Number(v.value)]));dirty=true;});
    target.querySelectorAll("[data-shot]").forEach(row=>{const s=p.shots[Number(row.dataset.shot)];row.querySelectorAll("[data-field]").forEach(input=>input.oninput=e=>{const f=input.dataset.field;if(p.production){s.keyframe_review="pending";invalidateProduction();}s[f]=["seed","frames"].includes(f)?Number(e.target.value):e.target.value;dirty=true;if(s.review==="accepted"&&["input_asset_id","prompt","role","scene","seed","frames","negative"].includes(f)){s.review="pending";s.selected_asset_id=null;s.selected_run_id=null;if(s.lipsync){s.lipsync.asset_id=null;s.lipsync.run_id=null;s.lipsync.review="pending";}}});});
    target.querySelectorAll("[data-upload]").forEach(input=>input.onchange=()=>action(()=>upload(input.files[0],p.shots[Number(input.dataset.upload)])));
    target.querySelectorAll("[data-remove]").forEach(b=>b.onclick=()=>{if(p.shots.length===1)return toast("至少保留一段","err");p.shots.splice(Number(b.dataset.remove),1);dirty=true;draw();});
    target.querySelector("#aigc-add").onclick=()=>{if(p.shots.length>=30)return toast("每个项目最多 30 段","err");p.shots.push(shot(p.shots.length+1));dirty=true;draw();};
    target.querySelectorAll("[data-generate]").forEach(b=>b.onclick=()=>action(()=>start("generate",b.dataset.generate)));
    target.querySelector("#aigc-save").onclick=()=>action(save);
    target.querySelector("#aigc-export")?.addEventListener("click",()=>action(async()=>{if(dirty)await save();const result=await request(`/v1/aigc/projects/${current.id}/export`,{});const blob=await api.downloadAsset(result.asset_id);const url=URL.createObjectURL(blob);const link=document.createElement("a");link.href=url;link.download=current.payload.name+" · Win 本地制作项目.zip";link.click();setTimeout(()=>URL.revokeObjectURL(url),60000);toast("本地制作项目已导出");}));
    target.querySelector("#aigc-generate").onclick=()=>action(()=>start("generate"));
    target.querySelector("#aigc-compose")?.addEventListener("click",()=>action(()=>start("compose")));
    target.querySelector("#aigc-refresh").onclick=()=>action(loadRuns);
    if(p.kind==="story"){
      for(const [dom,field] of [["audio","audio_asset_id"],["voice","voice_asset_id"],["narration","narration"]])target.querySelector(`#aigc-${dom}`).oninput=e=>{p[field]=e.target.value||(field==="narration"?"":null);dirty=true;};
      for(const field of ["audio","voice"])target.querySelector(`#aigc-${field}-upload`).onchange=e=>action(()=>upload(e.target.files[0],null,field+"_asset_id"));
      target.querySelector("#aigc-subtitles").oninput=e=>{p.subtitles=e.target.value.split("\n").filter(l=>l.trim()).map(l=>{const [start,end,...text]=l.split("|");return {start:Number(start),end:Number(end),text:text.join("|").trim()};});dirty=true;};
      target.querySelector("#aigc-speak").onclick=()=>action(()=>start("speak"));
    }
    target.querySelectorAll("[data-preview]").forEach(b=>b.onclick=()=>action(()=>openPreview(api,b.dataset.preview,{mime:"video/mp4",title:"已选片段"})));
    bindProduction(target);
    drawRuns();
  }
  function invalidateProduction({motion=true,shots=current.payload.shots}={}){const p=current.payload;if(p.production)p.production.animatic_review="pending";for(const s of shots){if(motion){s.keyframe_review="pending";s.review="pending";s.selected_asset_id=null;s.selected_run_id=null;}if(s.lipsync){s.lipsync.asset_id=null;s.lipsync.run_id=null;s.lipsync.review="pending";}}}
  function bindProduction(target){
    const p=current.payload;
    target.querySelector("#aigc-enable-production")?.addEventListener("click",()=>{enableProduction(p);dirty=true;draw();});
    if(!p.production)return;
    const plan=p.production;
    target.querySelectorAll("[data-plan-field]").forEach(input=>input.oninput=()=>{
      const {planScope:scope,planId:id,planField:field}=input.dataset;
      const obj=scope==="plan"?plan:scope==="shot"?p.shots.find(s=>s.id===id):scope==="blocking"?p.shots.find(s=>s.id===id.split(":")[0]).cast.find(b=>b.character_id===id.split(":")[1]):plan[scope].find(c=>c.id===id);
      if(field.startsWith("reference_")){const refs=[...target.querySelectorAll(`[data-plan-scope="${scope}"][data-plan-id="${id}"][data-plan-field^="reference_"]`)];obj.reference_asset_ids=refs.map(r=>r.value).filter(Boolean);}
      else if(field==="style_caption")plan.style_enhancers=input.value.trim()?{content:"subject",style:input.value}:null;
      else if(field==="fixed_traits")obj[field]=input.value.split("\n").map(v=>v.trim()).filter(Boolean);
      else if(field.startsWith("state_")){obj[field]=Object.fromEntries(input.value.split("\n").filter(v=>v.trim()).map(v=>{const index=v.indexOf("=");return index<1?[v,""]:[v.slice(0,index).trim(),v.slice(index+1).trim()];}));}
      else obj[field]=["duration","trim_in","start","end","image_width","image_height","reference_index"].includes(field)?(input.value===""?null:Number(input.value)):input.value||(["scene_id","voice_asset_id","audio_asset_id","style_reference_asset_id"].includes(field)?null:"");
      const timelineOnly=scope==="dialogue"||scope==="plan"&&["script","style_caption","style_transfer_recipe"].includes(field)||scope==="shot"&&["duration","trim_in"].includes(field)||scope==="characters"&&["personality","voice_asset_id","style_content"].includes(field);
      if((scope==="characters"||scope==="scenes")&&!timelineOnly)obj.review="pending";
      if(scope==="shot"&&!timelineOnly)obj.keyframe_review="pending";
      const affected=scope==="shot"?[obj]:scope==="blocking"?[p.shots.find(s=>s.id===id.split(":")[0])]:scope==="characters"?p.shots.filter(s=>s.cast?.some(b=>b.character_id===id)):scope==="scenes"?p.shots.filter(s=>s.scene_id===id):p.shots;
      invalidateProduction({motion:!timelineOnly,shots:affected});dirty=true;
    });
    target.querySelectorAll("[data-lip-speaker]").forEach(input=>input.onchange=()=>{const shot=p.shots.find(s=>s.id===input.dataset.lipSpeaker);shot.lipsync=input.value?{character_id:input.value,face_box:null,asset_id:null,run_id:null,review:"pending"}:null;invalidateProduction({motion:false,shots:[shot]});dirty=true;draw();});
    target.querySelectorAll("[data-face-pick]").forEach(button=>button.onclick=()=>action(async()=>{const shot=p.shots.find(s=>s.id===button.dataset.facePick);if(!shot.input_asset_id)throw new Error("先绑定这个镜头的分镜图");const box=await pickFaceRegion(api,shot.input_asset_id,"框选说话角色的人脸",shot.lipsync.face_box);if(box){shot.lipsync.face_box=box;invalidateProduction({motion:false,shots:[shot]});dirty=true;draw();}}));
    target.querySelectorAll("[data-lip-start]").forEach(button=>button.onclick=()=>action(()=>start("lipsync",button.dataset.lipStart)));
    target.querySelectorAll("[data-cast-toggle]").forEach(input=>input.onchange=()=>{const shot=p.shots.find(s=>s.id===input.dataset.shotId);shot.cast=(shot.cast||[]).filter(b=>b.character_id!==input.dataset.castToggle);if(input.checked)shot.cast.push({character_id:input.dataset.castToggle,position:"",eyeline:"",expression:""});shot.keyframe_review="pending";invalidateProduction();dirty=true;draw();});
    target.querySelectorAll("[data-plan-add]").forEach(b=>b.onclick=()=>{const scope=b.dataset.planAdd,id=(scope==="characters"?"c_":scope==="scenes"?"scene_":"line_")+crypto.randomUUID().slice(0,8);plan[scope].push(scope==="characters"?{id,name:"新角色",appearance:"",fixed_traits:[],personality:"",wardrobe:"",reference_asset_ids:[],voice_asset_id:null,review:"pending"}:scope==="scenes"?{id,name:"新场景",description:"",layout:"",lighting:"",reference_asset_ids:[],review:"pending"}:{id,character_id:plan.characters[0]?.id||"",text:"",start:0,end:1,audio_asset_id:null});invalidateProduction();dirty=true;draw();});
    target.querySelectorAll("[data-plan-remove]").forEach(b=>b.onclick=()=>{const scope=b.dataset.planRemove,id=b.dataset.planId;if(scope!=="dialogue"&&plan[scope].length===1)return toast("至少保留一项","err");if(scope==="characters"&&(p.shots.some(s=>s.cast?.some(c=>c.character_id===id))||plan.dialogue.some(d=>d.character_id===id)))return toast("先移除分镜与对白中的角色引用","err");if(scope==="scenes"&&p.shots.some(s=>s.scene_id===id))return toast("先调整分镜的场景引用","err");plan[scope]=plan[scope].filter(c=>c.id!==id);invalidateProduction();dirty=true;draw();});
    target.querySelectorAll("[data-card-upload]").forEach(input=>input.onchange=()=>action(async()=>{const file=input.files[0];if(!file)return;const card=plan[input.dataset.cardUpload].find(c=>c.id===input.dataset.cardId);if(card.reference_asset_ids.length>=3)throw new Error("每张设定卡最多三张独立参考图，请先调整现有参考图");const result=await api.uploadAigc(file,file.type);if(card.reference_asset_ids.includes(result.asset_id))throw new Error("这张参考图已经在当前设定卡中");if(!media.some(m=>m.id===result.asset_id))media.push({id:result.asset_id,mime:file.type,name:file.name});card.reference_asset_ids.push(result.asset_id);card.review="pending";invalidateProduction();dirty=true;draw();}));
    target.querySelector("#aigc-style-upload")?.addEventListener("change",event=>action(async()=>{const file=event.target.files[0];if(!file)return;const result=await api.uploadAigc(file,file.type);if(!media.some(m=>m.id===result.asset_id))media.push({id:result.asset_id,mime:file.type,name:file.name});plan.style_reference_asset_id=result.asset_id;invalidateProduction();dirty=true;draw();}));
    target.querySelectorAll("[data-card-style]").forEach(b=>b.onclick=()=>action(()=>start("harmonize",b.dataset.cardStyle)));
    target.querySelectorAll("[data-card-generate]").forEach(b=>b.onclick=()=>action(()=>start("cards",b.dataset.cardGenerate)));
    target.querySelectorAll("[data-card-review]").forEach(b=>b.onclick=()=>action(async()=>{const card=plan[b.dataset.cardReview].find(c=>c.id===b.dataset.planId);if(!card.reference_asset_ids.length)throw new Error("先绑定参考图");await openPreview(api,card.reference_asset_ids[0],{mime:"image/png",title:card.name});if(await confirmDialog({title:"审查设定卡",body:"检查造型、比例、固定特征与风格，并确认所有参考图一致。",confirmText:"已审查，使用"})){card.review="accepted";dirty=true;draw();}}));
    target.querySelectorAll("[data-frame-review]").forEach(b=>b.onclick=()=>action(async()=>{const shot=p.shots.find(s=>s.id===b.dataset.frameReview);if(!shot.input_asset_id)throw new Error("先绑定分镜图");await openPreview(api,shot.input_asset_id,{mime:"image/png",title:shot.label});if(await confirmDialog({title:"审查分镜图",body:"检查人物身份、站位、视线、道具状态和构图。",confirmText:"已审查，使用"})){shot.keyframe_review="accepted";dirty=true;draw();}}));
    target.querySelector("#aigc-keyframes").onclick=()=>action(()=>start("keyframes"));
    target.querySelector("#aigc-animatic").onclick=()=>action(()=>start("animatic"));
    target.querySelector("#aigc-review-animatic")?.addEventListener("click",()=>action(async()=>{await openPreview(api,plan.animatic_asset_id,{mime:"video/mp4",title:"分镜粗剪"});if(await confirmDialog({title:"审查动态分镜",body:"检查对白、剧情节奏、人物关系与镜头衔接。",confirmText:"已审查，使用"})){plan.animatic_review="accepted";dirty=true;await save();}}));
  }
  function shot(i){return {id:"s_"+crypto.randomUUID().slice(0,8),label:"第 "+i+" 段",role:"",scene:"",input_asset_id:"",prompt:"",negative:"deformed limbs, extra fingers, cropped body",seed:i,frames:65,selected_asset_id:null,review:"pending",cast:[],scene_id:null,purpose:"",camera:"",action_start:"",action_end:"",state_before:{},state_after:{},duration:null,trim_in:0,keyframe_review:"pending"};}
  async function action(fn){if(busy)return;busy=true;el.inert=true;el.setAttribute("aria-busy","true");try{await fn();}catch(e){toast(e.message,"err");}finally{busy=false;el.inert=false;el.removeAttribute("aria-busy");}}
  async function save(){if(stopped)return;const result=await request(`/v1/aigc/projects/${current.id}`,{payload:current.payload,expected_revision:current.revision});current.revision=result.revision;const index=projects.findIndex(x=>x.id===current.id);if(index<0)projects.push(structuredClone(current));else projects[index]=structuredClone(current);dirty=false;draw();toast("项目已保存");}
  async function start(stage,shot_id){if(!catalog.engine_enabled||!nodes.length)throw new Error("制作服务尚未连接");if(dirty||!current.revision)await save();const key=`agentos_aigc_submit_${current.id}`;const body={revision:current.revision,stage,shot_id,idempotency_key:crypto.randomUUID()};const pending=sessionStorage.getItem(key);if(pending){const previous=JSON.parse(pending);if(previous.revision===body.revision&&previous.stage===stage&&previous.shot_id===shot_id)body.idempotency_key=previous.idempotency_key;}sessionStorage.setItem(key,JSON.stringify(body));const result=await request(`/v1/aigc/projects/${current.id}/start`,body);sessionStorage.removeItem(key);toast("制作任务已受理，可以离开此页");await loadRuns();return result;}
  async function loadRuns(){if(!current?.revision||stopped)return;const id=current.id;const rows=await request(`/v1/aigc/projects/${id}/runs`);if(stopped||current.id!==id)return;runs=rows;for(const run of runs)for(const output of Object.values(run.outputs))for(const a of output.assets||[])if(!media.some(m=>m.id===a.asset_id))media.push({id:a.asset_id,mime:a.mime,name:ROLE[a.role]||a.role});drawRuns();}
  function drawRuns(){const box=el.querySelector("#aigc-runs");if(!box)return;box.innerHTML=runs.slice().reverse().map(run=>`<article class="aigc-run"><div class="row"><b>${esc(STATE[run.state]||run.state)}</b><a class="btn small" href="#/tasks/${esc(run.run_id)}">打开任务与核对</a></div>${Object.entries(run.outputs).map(([node,v])=>`<div class="aigc-candidate"><b>${esc(node.startsWith("shot_")?current.payload.shots.find(s=>"shot_"+s.id===node)?.label||node:node.startsWith("pack_")?"桌宠封装":node==="film"?"短剧成片":node==="animatic"?"分镜粗剪":node.startsWith("lip_")?"口型候选":node.startsWith("frame_")?"分镜图":node.startsWith("character_")?"角色定妆":node.startsWith("scene_")?"场景定妆":"本地配音")}</b><div class="row">${(v.assets||[]).map(a=>`<button class="btn small" data-result-preview="${esc(a.asset_id)}" data-mime="${esc(a.mime)}">${esc(ROLE[a.role]||a.role)}</button><button class="btn small" data-artifact-download="${esc(a.asset_id)}" data-name="${esc(artifactName(a))}" aria-label="下载${esc(ROLE[a.role]||a.role)}">${icon("download",12)}</button>`).join("")}</div>${v.asset_id&&node.startsWith("frame_")?`<button class="btn small" data-use-frame="${esc(node.slice(6))}" data-asset="${esc(v.asset_id)}">绑定为待审查分镜图</button>`:v.asset_id&&node.startsWith("character_")?`<button class="btn small" data-use-card="characters" data-card-id="${esc(node.slice(10))}" data-asset="${esc(v.asset_id)}">绑定到角色卡</button>`:v.asset_id&&node.startsWith("scene_")?`<button class="btn small" data-use-card="scenes" data-card-id="${esc(node.slice(6))}" data-asset="${esc(v.asset_id)}">绑定到场景卡</button>`:v.asset_id&&node==="animatic"?`<button class="btn small" data-use-animatic="${esc(v.asset_id)}" data-run="${esc(run.run_id)}">绑定待审查粗剪</button>`:""}${node.startsWith("lip_")&&v.asset_id?`<button class="btn small" data-use-lip="${esc(node.slice(4))}" data-asset="${esc(v.asset_id)}" data-run="${esc(run.run_id)}">审查口型后使用</button>`:""}${node.startsWith("shot_")&&v.asset_id?`<button class="btn small" data-select="${esc(node.slice(5))}" data-asset="${esc(v.asset_id)}" data-run="${esc(run.run_id)}">审查后选用这段</button><button class="btn small" data-reject="${esc(node.slice(5))}">标记需重做</button>`:node.startsWith("speech")&&v.asset_id?`<button class="btn small" data-speech="${esc(v.asset_id)}" data-speech-node="${esc(node)}">${node==="speech"?"用于成片配音":"绑定到这句对白"}</button>`:""}</div>`).join("")}</article>`).join("")||'<p class="muted">制作结果会显示在这里。原片与参数记录可随时下载。</p>';
    box.querySelectorAll("[data-result-preview]").forEach(b=>b.onclick=()=>action(()=>openPreview(api,b.dataset.resultPreview,{mime:b.dataset.mime,title:b.textContent})));
    box.querySelectorAll("[data-use-lip]").forEach(button=>button.onclick=()=>action(async()=>{await openPreview(api,button.dataset.asset,{mime:"video/mp4",title:"口型候选"});if(!await confirmDialog({title:"审查口型候选",body:"检查声音和口型时序、嘴部造型、小虎牙，以及其他角色是否保持原样。",confirmText:"已审查，使用"}))return;const shot=current.payload.shots.find(s=>s.id===button.dataset.useLip);if(shot?.lipsync){Object.assign(shot.lipsync,{asset_id:button.dataset.asset,run_id:button.dataset.run,review:"accepted"});dirty=true;await save();}}));
    box.querySelectorAll("[data-select]").forEach(b=>b.onclick=()=>action(async()=>{if(!await confirmDialog({title:"选用这段镜头",body:"请先预览，检查角色、肢体、动作收尾和裁切。选用后该镜头可参加合成。",confirmText:"已审查，选用"}))return;const s=current.payload.shots.find(s=>s.id===b.dataset.select);if(s){s.selected_asset_id=b.dataset.asset;s.selected_run_id=b.dataset.run;s.review="accepted";if(s.lipsync){s.lipsync.asset_id=null;s.lipsync.run_id=null;s.lipsync.review="pending";}dirty=true;await save();}}));
    box.querySelectorAll("[data-reject]").forEach(b=>b.onclick=()=>action(async()=>{const s=current.payload.shots.find(s=>s.id===b.dataset.reject);if(s){s.review="rejected";s.selected_asset_id=null;s.selected_run_id=null;dirty=true;await save();}}));
    box.querySelectorAll("[data-speech]").forEach(b=>b.onclick=()=>action(async()=>{if(b.dataset.speechNode==="speech")current.payload.audio_asset_id=b.dataset.speech;else{const line=current.payload.production?.dialogue.find(d=>"speech_"+d.id===b.dataset.speechNode);if(line)line.audio_asset_id=b.dataset.speech;invalidateProduction({motion:false});}dirty=true;await save();}));box.querySelectorAll("[data-use-frame]").forEach(b=>b.onclick=()=>action(async()=>{const shot=current.payload.shots.find(s=>s.id===b.dataset.useFrame);if(shot){shot.input_asset_id=b.dataset.asset;shot.keyframe_review="pending";invalidateProduction();dirty=true;await save();}}));
    box.querySelectorAll("[data-use-card]").forEach(b=>b.onclick=()=>action(async()=>{const card=current.payload.production?.[b.dataset.useCard].find(c=>c.id===b.dataset.cardId);if(card){card.reference_asset_ids=[b.dataset.asset];card.review="pending";invalidateProduction();dirty=true;await save();}}));
    box.querySelectorAll("[data-use-animatic]").forEach(b=>b.onclick=()=>action(async()=>{const plan=current.payload.production;if(plan){plan.animatic_asset_id=b.dataset.useAnimatic;plan.animatic_run_id=b.dataset.run;plan.animatic_review="pending";dirty=true;await save();}}));
    ctx.bindCommon();
  }
  el.querySelector("#aigc-new").onclick=()=>action(async()=>{if(dirty&&!await confirmDialog({title:"新建项目",body:"放弃未保存的修改并新建？",confirmText:"新建"}))return;const kind=await pickTemplate();if(!kind)return;const pet=kind==="pet";current={id:"p_"+crypto.randomUUID().replaceAll("-",""),revision:0,payload:{name:pet?"新的桌宠项目":"新的短剧项目",kind:pet?"pet":"story",node_id:nodes[0]||"",width:512,height:800,fps:16,shots:[shot(1)],narration:"",audio_asset_id:null,voice_asset_id:null,subtitles:[]}};dirty=true;runs=[];draw();});
  ctx.registerGuard?.(async()=>!dirty||await confirmDialog({title:"离开制作项目",body:"项目有未保存的修改，是否放弃？",confirmText:"放弃修改"}));
  draw();await loadRuns();
  return {poll:loadRuns,destroy:()=>{stopped=true;closeFacePickers();}};
}

function pickTemplate(){return new Promise(resolve=>{
  const mask=document.createElement("div");mask.className="dialog-mask";
  mask.innerHTML=`<div class="dialog" role="dialog" aria-modal="true" aria-label="选择制作模板"><h3>选择制作模板</h3><p>桌宠保存透明动作素材；短剧按镜头生成，选片后合成。</p><div class="row"><button class="btn primary" data-kind="pet">桌宠动作</button><button class="btn" data-kind="story">短剧镜头</button><button class="btn" data-cancel>取消</button></div></div>`;
  const close=kind=>{mask.remove();resolve(kind);};
  mask.querySelectorAll("[data-kind]").forEach(b=>b.onclick=()=>close(b.dataset.kind));
  mask.querySelector("[data-cancel]").onclick=()=>close(null);
  mask.onclick=e=>{if(e.target===mask)close(null);};mask.onkeydown=e=>{if(e.key==="Escape")close(null);};
  document.body.appendChild(mask);mask.querySelector("[data-kind]").focus();
});}
