"""Regression behavior for actionable tasks and server-supplied roles."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / 'frontend/js/workspace-data.js'


def node(code: str) -> object:
    result = subprocess.run([shutil.which('node') or 'node', '--input-type=module', '-e',
        'import {filterRuns, roleLabel, taskTitle, riskLabels} from '
        + json.dumps(MODULE.as_uri()) + ';\n' + code],
        capture_output=True, text=True, timeout=15, check=True)
    return json.loads(result.stdout)


def test_attention_includes_pending_input_review_and_failed_not_success() -> None:
    actual = node("""
      const runs = ['succeeded','failed','awaiting_approval','awaiting_input','awaiting_review',
        'paused','running','cancelled'].map(state=>({id:state,state,goal:'合成报告 '+state}));
      console.log(JSON.stringify({attention:filterRuns(runs,'attention').map(r=>r.id),
        searched:filterRuns(runs,'attention','REPORT'),
        failed:filterRuns(runs,'attention','FAILED').map(r=>r.id),all:filterRuns(runs).length}));
    """)
    assert actual == {'attention': ['failed', 'awaiting_approval', 'awaiting_input',
        'awaiting_review', 'paused'], 'searched': [], 'failed': ['failed'], 'all': 8}


def test_roles_never_promote_member_or_missing_role() -> None:
    assert node("console.log(JSON.stringify(['admin','member','guest',null,'other'].map(roleLabel)))") == [
        '管理员', '成员', '游客（只读）', '角色未确认', '角色未确认']


def test_member_settings_hide_admin_password_and_demo_management() -> None:
    settings = (ROOT / 'frontend/js/views/settings.js').as_uri()
    result = node("""
      globalThis.localStorage={getItem:()=>null};
      globalThis.matchMedia=()=>({matches:false,addEventListener:()=>{}});
      const {renderSettings}=await import(SETTINGS);
      const html=[];
      for (const role of ['member','admin',null]) {
        const el={innerHTML:'',querySelector:s=> s==='#st-pw' && role!=='admin' ? null
          : {value:'',addEventListener:()=>{}}};
        await renderSettings({el, api:{getCapabilities:async()=>({}),
          authMe:async()=>({role,subject:'synthetic'})},session:{logout:()=>{}}});
        html.push({role,password:el.innerHTML.includes('id="st-pw"'),
          demo:el.innerHTML.includes('href="#/demo"'),member:el.innerHTML.includes('成员')});
      }
      console.log(JSON.stringify(html));
    """.replace('SETTINGS', json.dumps(settings)))
    assert result == [
        {'role': 'member', 'password': False, 'demo': False, 'member': True},
        {'role': 'admin', 'password': True, 'demo': True, 'member': False},
        {'role': None, 'password': False, 'demo': False, 'member': False},
    ]
