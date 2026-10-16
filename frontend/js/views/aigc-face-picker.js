/* Authenticated still preview for choosing one speaking face; no tokens in media URLs. */
"use strict";
import {esc} from "../safe-export.js";
const active = new Set();
export function closeFacePickers(){for(const close of [...active])close(null);}
export async function pickFaceRegion(api, assetId, title, initial){
  const blob=await api.downloadAsset(assetId),url=URL.createObjectURL(blob),image=new Image();
  try{image.src=url;await image.decode();}catch(error){URL.revokeObjectURL(url);throw error;}
  return new Promise(resolve=>{
    const previous=document.activeElement,mask=document.createElement("div");mask.className="dialog-mask preview-mask";
    mask.innerHTML=`<div class="dialog preview-dialog" role="dialog" aria-modal="true" aria-label="框选说话角色的人脸"><h3>${esc(title)}</h3><p>拖动框住这个角色的整张脸，包括额头、眼睛和下巴，避开其他角色。静止或小幅动作镜头更适合口型处理。</p><canvas aria-label="拖动框选人脸" style="display:block;max-width:100%;max-height:60vh;touch-action:none;margin:auto"></canvas><div class="row"><button class="btn primary" data-use>使用这个区域</button><button class="btn" data-cancel>取消</button></div><p role="status"></p></div>`;
    const canvas=mask.querySelector("canvas"),context=canvas.getContext("2d"),use=mask.querySelector("[data-use]"),status=mask.querySelector("[role=status]");
    canvas.width=image.width;canvas.height=image.height;let box=initial,start=null;
    const valid=()=>box&&box[2]>box[0]&&box[3]>box[1]&&(box[2]-box[0])*image.width>=64&&(box[3]-box[1])*image.height>=64;
    const draw=()=>{context.drawImage(image,0,0);if(box){context.strokeStyle="#59e7ae";context.lineWidth=Math.max(3,image.width/150);context.strokeRect(box[0]*image.width,box[1]*image.height,(box[2]-box[0])*image.width,(box[3]-box[1])*image.height);}use.disabled=!valid();status.textContent=valid()?"已选中区域，请确认框内只有说话角色的脸。":"请框选完整且清晰的人脸。";};
    const point=event=>{const r=canvas.getBoundingClientRect();return [Math.max(0,Math.min(1,(event.clientX-r.left)/r.width)),Math.max(0,Math.min(1,(event.clientY-r.top)/r.height))];};
    canvas.onpointerdown=e=>{start=point(e);canvas.setPointerCapture(e.pointerId);};
    canvas.onpointermove=e=>{if(!start)return;const end=point(e);box=[Math.min(start[0],end[0]),Math.min(start[1],end[1]),Math.max(start[0],end[0]),Math.max(start[1],end[1])];draw();};
    canvas.onpointerup=()=>{start=null;};canvas.onpointercancel=()=>{start=null;};
    const close=value=>{active.delete(close);mask.remove();URL.revokeObjectURL(url);if(previous?.isConnected)previous.focus();resolve(value);};active.add(close);
    use.onclick=()=>{if(valid())close(box);};mask.querySelector("[data-cancel]").onclick=()=>close(null);
    mask.onclick=e=>{if(e.target===mask)close(null);};mask.onkeydown=e=>{if(e.key==="Escape")close(null);};
    document.body.appendChild(mask);draw();mask.querySelector("[data-cancel]").focus();
  });
}
