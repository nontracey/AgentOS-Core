"""Exercise real frontend SSE reader with chunk splits, EOF reconnect and cursor replay."""

import json
import shutil
import subprocess
from pathlib import Path


def test_sse_reconnect_keeps_cursor_and_reducer_deduplicates():
    root = Path(__file__).resolve().parents[2] / "frontend/js"
    script = """
const memory = new Map([['agentos_token','synthetic']]);
globalThis.sessionStorage = {getItem:k=>memory.get(k),removeItem:k=>memory.delete(k)};
globalThis.localStorage = {getItem:()=>null,removeItem:()=>{}};
const api = (await import(API)).createApi();
const {mergeEvents,answerMessages} = await import(LIVE);
const calls=[], delivered=[];
let fetchCount=0;
const encode=x=>new TextEncoder().encode(x);
globalThis.fetch=async (url)=>{
  calls.push(url);fetchCount++;
  const frame=(seq,kind,payload)=>'data: '+JSON.stringify({seq,kind,payload})+'\\r\\n\\r\\n';
  const first=frame(1,'answer.start',{call_id:'a'})+frame(2,'answer.delta',{call_id:'a',delta:'甲'});
  const second=frame(2,'answer.delta',{call_id:'a',delta:'甲'})+
    frame(3,'answer.delta',{call_id:'a',delta:'乙'})+frame(4,'answer.end',{call_id:'a'})+
    frame(4,'stream.end',{});
  const frames=fetchCount===1 ? [first.slice(0,first.indexOf('\\r')+1),
    first.slice(first.indexOf('\\r')+1)] : [second];
  return new Response(new ReadableStream({start(c){for(const f of frames)c.enqueue(encode(f));c.close();}}));
};
let resolve;const complete=new Promise(r=>resolve=r);
const stop=api.streamTask('run',{onEvent:e=>{delivered.push(e);if(e.kind==='stream.end')resolve();}});
await Promise.race([complete,new Promise((_,reject)=>setTimeout(()=>reject(Error('timeout')),4000))]);
stop();
const merged=mergeEvents([],delivered);
console.log(JSON.stringify({calls,seq:merged.map(e=>e.seq),text:answerMessages(merged)[0].text}));
""".replace("import(API)", "import(" + json.dumps((root / "api.js").as_uri()) + ")").replace(
        "import(LIVE)", "import(" + json.dumps((root / "live.js").as_uri()) + ")"
    )
    result = subprocess.run(
        [shutil.which("node"), "--input-type=module", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    data = json.loads(result.stdout)
    assert len(data["calls"]) == 2 and "cursor=2" in data["calls"][1]
    assert data["seq"] == [1, 2, 3, 4] and data["text"] == "甲乙"


def test_guest_tab_preserves_admin_remember_token_without_using_it():
    module = Path(__file__).resolve().parents[2] / "frontend/js/api.js"
    script = """
const admin = new Map([['agentos_refresh','admin-refresh']]), guest = new Map();
const storage=m=>({getItem:k=>m.get(k)||null,setItem:(k,v)=>m.set(k,v),removeItem:k=>m.delete(k)});
globalThis.localStorage=storage(admin);globalThis.sessionStorage=storage(guest);
const {createApi,getRefreshToken,clearAuth}=await import(MODULE);
const seen=[];globalThis.fetch=async(url,opts)=>{
  seen.push({url,body:opts.body});
  if(url.endsWith('/guest/bootstrap'))return Response.json({access_token:'guest-access',refresh_token:'guest-refresh'});
  return Response.json({detail:{message:'revoked'}},{status:401});
};
const api=createApi();await api.guestBootstrap();
if(getRefreshToken()!=='guest-refresh')throw Error('wrong refresh');
try{await api.listRuns();}catch(e){}
clearAuth();try{await api.listRuns();}catch(e){}
console.log(JSON.stringify({remembered:admin.get('agentos_refresh'),refreshes:seen.filter(x=>x.url.endsWith('/refresh')).map(x=>JSON.parse(x.body).refresh_token),remaining:getRefreshToken()}));
""".replace("import(MODULE)", "import(" + json.dumps(module.as_uri()) + ")")
    result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", script],
                            check=True, capture_output=True, text=True, timeout=10)
    data = json.loads(result.stdout)
    assert data == {"remembered": "admin-refresh", "refreshes": ["guest-refresh"], "remaining": ""}
