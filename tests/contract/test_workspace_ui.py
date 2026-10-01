"""U5 工作区 UI 契约：游客为默认角色、对话投影不编造证据、原始 JSON 只在管理员高级详情。

行为测试用 Node 直接执行 conversation.js 的纯投影（无 DOM 依赖）；
静态断言守住入口与渲染边界，不据样式判断通过。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
MAIN_JS = (FRONTEND / "js" / "main.js").read_text(encoding="utf-8")
API_JS = (FRONTEND / "js" / "api.js").read_text(encoding="utf-8")
GUEST_JS = (FRONTEND / "js" / "views" / "guest.js").read_text(encoding="utf-8")
THREAD_JS = (FRONTEND / "js" / "views" / "thread.js").read_text(encoding="utf-8")
DEMO_JS = (FRONTEND / "js" / "views" / "demo-admin.js").read_text(encoding="utf-8")
CONV_JS = (FRONTEND / "js" / "conversation.js").read_text(encoding="utf-8")

MODULE = (FRONTEND / "js" / "conversation.js").as_uri()


def run_node(code: str) -> object:
    node = shutil.which("node")
    assert node, "需要 node 执行投影行为测试"
    script = (
        "import {projectConversation, renderConversation} from "
        + json.dumps(MODULE) + ";\n" + code
    )
    out = subprocess.run([node, "--input-type=module", "-e", script],
                         check=True, capture_output=True, text=True, timeout=20)
    return json.loads(out.stdout.strip())


# ---------------------------------------------------------------- 默认角色入口
def test_guest_entry_is_code_free_bootstrap() -> None:
    """登录入口默认游客：一次免登换取只读会话，不再把访问码当必经步骤。"""
    assert "/v1/auth/guest/bootstrap" in API_JS
    assert "guestBootstrap" in API_JS and "guestBootstrap()" in MAIN_JS
    assert "以游客身份进入演示空间" in MAIN_JS
    # 兑换码只在演示访问页作为可选定向邀请（管理员生成），进入页不再要求输入
    assert "guestRedeem" not in MAIN_JS
    assert "定向邀请" in DEMO_JS and "createGrant" in DEMO_JS and "invite_code" in DEMO_JS
    assert "访问码" not in MAIN_JS or "不再要求访问码" in MAIN_JS
    # 免登会话只进 sessionStorage，不进 localStorage 的长期凭据
    bootstrap_src = API_JS.split("async guestBootstrap()")[1].split("},")[0]
    assert 'storeTokens(body.access_token, body.refresh_token, false, "guest")' in bootstrap_src
    assert "localStorage.removeItem" not in bootstrap_src


def test_role_failure_does_not_assume_admin() -> None:
    """角色查询失败不猜 admin：显示错误态并可重试/返回进入页。"""
    assert "无法确认访问角色" in MAIN_JS
    assert 'roleInfo = { role: "admin"' not in MAIN_JS
    assert "roleError" in MAIN_JS


# ---------------------------------------------------------------- 对话投影
def test_conversation_uses_only_recorded_evidence() -> None:
    """缺证据的字段写「未记录」，不生成百分比、不编造角色或节点。"""
    result = run_node("""
      const detail = {run:{goal:'整理周报',state:'succeeded'},
        steps:[{id:'s1',tool_name:'fs.write_text',state:'completed',attempt:1}],
        artifacts:[],approvals:[],refs:[]};
      const items = projectConversation(detail, [], {guest:false});
      const adminHtml = renderConversation(items);
      const guestItems = projectConversation(
        {run:{goal:'整理周报',state:'awaiting_approval'},steps:[],artifacts:[],
         approvals:[{id:'a1',state:'pending',summary:'tool=fs.write_text'}],refs:[]},
        [], {guest:true});
      console.log(JSON.stringify({
        adminHtml, guestHtml: renderConversation(guestItems),
        kinds: items.map(i=>i.type),
      }));
    """)
    assert isinstance(result, dict)
    kinds = result["kinds"]
    assert kinds[0] == "user" and "tool" in kinds
    # 没有事件证据时不出现编造的用量/节点行
    assert "metric" not in kinds
    assert "未记录" in result["adminHtml"] or "未记录" in result["adminHtml"]
    # 游客的审批渲染为只读状态，不含批准/拒绝按钮
    assert "data-decide" not in result["guestHtml"]
    assert "这一步由管理员裁定" in result["guestHtml"]


def test_guest_projection_never_renders_raw_json() -> None:
    """游客视图不出现 JSON.stringify(payload)；只渲染白名单标量字段。"""
    assert "JSON.stringify" not in GUEST_JS
    assert "JSON.stringify" not in CONV_JS
    # 管理员高级详情才有事件 JSON
    assert "JSON.stringify(events" in THREAD_JS
    assert "高级：已脱敏事件" in THREAD_JS


def test_event_mapping_covers_recorded_kinds() -> None:
    """真实事件种类映射到可读条目：工具行、交接、检查、成果、失败补传。"""
    result = run_node("""
      const events = [
        {seq:1,kind:'tool.prepared',payload:{step_id:'s1',tool:'image.generate',policy:'allowed'}},
        {seq:2,kind:'node.dispatched',payload:{step_id:'s1',node_id:'win',os:'Windows',tool:'image.generate',basis:'capability'}},
        {seq:3,kind:'model.usage',payload:{model:'m-1',input_tokens:10,output_tokens:5}},
        {seq:4,kind:'report.citation_check',payload:{passed:true,detail:'ok'}},
      ];
      const detail = {run:{goal:'g',state:'succeeded'},steps:[
        {id:'s1',tool_name:'image.generate',state:'completed',attempt:2},
        {id:'s2',tool_name:'node.push',state:'completed',
         result_json:JSON.stringify({value:{delivery:'pending',asset_ref:'a.png',sha256:'x'}})}],
        artifacts:[{id:'a1',name:'visual.png',mime:'image/png',size:10,version:1}],
        approvals:[],refs:[{source_id:'src-1',chunk_id:'src-1:0',quote:'片段'}]};
      const items = projectConversation(detail, events, {guest:true});
      console.log(JSON.stringify({kinds:items.map(i=>i.type),
        html: renderConversation(items)}));
    """)
    kinds = result["kinds"]
    for expected in ("tool", "handoff", "metric", "check", "artifact", "sources", "failure"):
        assert expected in kinds, f"缺少对话条目类型: {expected}"
    html = result["html"]
    assert "第 2 次尝试" in html            # 重试次数如实显示
    assert "Windows" in html                 # 设备来自事件
    assert "补传已有成果" in html             # 回传失败只补传，不重跑生成
    assert "片段" in html


def test_guest_has_read_only_navigation_and_no_write_entry() -> None:
    """游客导航只有演示任务/成果/说明；无新任务、无能力管理、无写动作入口。"""
    assert "演示成果" in MAIN_JS and "演示任务" in MAIN_JS and "访问说明" in MAIN_JS
    for forbidden in ("submitTask(", "cancelTask(", "decideApproval(", "uploadMaterial("):
        assert forbidden not in GUEST_JS, f"游客视图不应调用写入口: {forbidden}"
    # 服务端裁定：游客面全部走 /v1/demo/*
    assert "api.demoHome" in GUEST_JS and "api.demoRun" in GUEST_JS
    assert "/v1/tasks/" not in GUEST_JS
