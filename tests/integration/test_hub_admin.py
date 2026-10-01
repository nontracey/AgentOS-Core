"""U4-05 能力中心管理：MCP 真实调用、Skill 按需加载、权限收窄与停用。

真实外部组件：`tests/fixtures/u4/mcp_metrics_server.py`（真实 MCP stdio server，合成数据）。
模型仍是 scripted（机制级）；真实模型调用链在 U4-08/U4-10 另记。
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from agentos.apps.composition import SystemHandle
from agentos.apps.gateway import create_app
from tests.conftest import owner_token

REPO = Path(__file__).resolve().parents[2]
MCP_SERVER = REPO / "tests" / "fixtures" / "u4" / "mcp_metrics_server.py"

# scripted 模型：先真实调用 MCP 工具，再给最终答复
MCP_SCRIPT = [
    {"tool_calls": [{"id": "c1", "name": "u4metrics:team_metrics",
                     "arguments": {"sprint": "S1"}}]},
    {"content": "已完成：S1 完成 2/3（来自 MCP 工具）。"},
]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@contextmanager
def client(make_system: Callable[..., SystemHandle], script: Any = MCP_SCRIPT,
           **kw: Any) -> Iterator[Any]:
    handle = make_system(model_script=script, **kw)
    with TestClient(create_app(system=handle)) as c:
        yield c, handle, owner_token(handle)


def register_server(c: TestClient, token: str, *, enabled_risks: list[str] | None = None) -> dict:
    r = c.post("/v1/mcp/servers", headers=auth(token), json={
        "server_id": "u4metrics",
        "command": f"{sys.executable} {MCP_SERVER}",
        "risks": enabled_risks or ["read"],
    })
    assert r.status_code == 201, r.text
    return r.json()


def wait_state(c: TestClient, token: str, run_id: str, states: set[str], timeout: float = 15) -> str:
    deadline = time.monotonic() + timeout
    state = ""
    while time.monotonic() < deadline:
        state = c.get(f"/v1/tasks/{run_id}", headers=auth(token)).json()["run"]["state"]
        if state in states:
            return state
        time.sleep(0.05)
    return state


def submit(c: TestClient, token: str, goal: str, key: str) -> str:
    r = c.post("/v1/tasks", headers=auth(token),
               json={"payload": {"goal": goal}, "idempotency_key": key})
    assert r.status_code == 202, r.text
    return r.json()["run_id"]


# ------------------------------------------------------------------ 真实 MCP 注册/探针
def test_register_and_probe_real_mcp_server(make_system: Callable[..., SystemHandle]) -> None:
    with client(make_system) as (c, handle, token):
        created = register_server(c, token)
        assert "u4metrics:team_metrics" in created["tools"], created
        probe = c.post("/v1/mcp/servers/u4metrics/probe", headers=auth(token)).json()
        assert probe["protocol_version"], "真实探针必须协商到协议版本"
        assert probe["transport"] == "stdio"
        names = [t["name"] for t in probe["tools"]]
        assert names == ["u4metrics:team_metrics", "u4metrics:now_utc"], names
        # 清单不泄露命令/环境变量等秘密
        listed = c.get("/v1/mcp/servers", headers=auth(token)).json()
        blob = str(listed)
        assert "env_json" not in blob
        assert "'command'" not in blob and "mcp_metrics_server" not in blob, "命令行原文不外泄"


# ------------------------------------------------------------------ 真实调用进入普通任务
def test_real_mcp_tool_runs_inside_normal_task(make_system: Callable[..., SystemHandle]) -> None:
    with client(make_system) as (c, handle, token):
        register_server(c, token)
        rid = submit(c, token, "用指标工具给出本冲刺完成率", "hub1")
        state = wait_state(c, token, rid, {"succeeded", "failed"})
        assert state == "succeeded", state
        detail = c.get(f"/v1/tasks/{rid}", headers=auth(token)).json()
        steps = detail["steps"]
        assert any(s["tool_name"] == "u4metrics:team_metrics"
                   and s["state"] == "completed" for s in steps), steps
        # 真实结果进了 trace（含工具返回的合成数据），不是"列了工具名"
        trace = c.get(f"/v1/tasks/{rid}/trace", headers=auth(token)).json()["events"]
        assert any(e["kind"] == "tool.prepared" and "u4metrics" in str(e["payload"]) for e in trace)
        # 工具真实返回的结构化结果进入 trace（不是只记工具名）
        assert "2/3" in json.dumps(trace, ensure_ascii=False)


# ------------------------------------------------------------------ 停用收窄
def test_disable_blocks_new_calls_and_resolve(make_system: Callable[..., SystemHandle]) -> None:
    with client(make_system) as (c, handle, token):
        register_server(c, token)
        off = c.post("/v1/mcp/servers/u4metrics/enabled", headers=auth(token),
                     json={"enabled": False})
        assert off.status_code == 200 and off.json()["enabled"] is False
        listed = c.get("/v1/mcp/servers", headers=auth(token)).json()
        assert listed[0]["enabled"] == 0
        rid = submit(c, token, "停用后仍尝试调用", "hub2")
        state = wait_state(c, token, rid, {"succeeded", "failed"})
        assert state == "failed", "停用后不得静默替换成其他工具或伪造成功"
        detail = c.get(f"/v1/tasks/{rid}", headers=auth(token)).json()
        assert all(s["tool_name"] != "u4metrics:team_metrics"
                   or s["state"] != "completed" for s in detail["steps"])
        # 重新启用后可再次解析
        on = c.post("/v1/mcp/servers/u4metrics/enabled", headers=auth(token),
                    json={"enabled": True})
        assert on.json()["enabled"] is True


# ------------------------------------------------------------------ Skill：安装≠授权、按需正文、版本固定
def test_skill_install_is_not_authorization_and_body_is_on_demand(
    make_system: Callable[..., SystemHandle], tmp_path: Path,
) -> None:
    skill_dir = tmp_path / "brand-report"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: brand-report\nversion: 3\ndescription: 合成品牌报告技能\n"
        "allowed_tools: math.add\nentrypoints: references/checklist.md\n---\n\n"
        "# 品牌报告技能\n\n本技能要求：先给结论，再给引用，不得编造日期。\n",
        encoding="utf-8",
    )
    refs = skill_dir / "references"
    refs.mkdir()
    (refs / "checklist.md").write_text("检查项：日期来源、预算来源。\n", encoding="utf-8")

    with client(make_system) as (c, handle, token):
        installed = c.post("/v1/skills", headers=auth(token),
                           json={"source": str(skill_dir)})
        assert installed.status_code == 201, installed.text
        meta = installed.json()
        assert meta["id"] == "brand-report" and meta["version"] == "3"
        # 阶段 1：列表只有元数据，不含正文
        listed = c.get("/v1/skills", headers=auth(token)).json()
        assert listed and "先给结论" not in str(listed)
        # 阶段 2：按需读取正文，带版本与资源清单
        body = c.get("/v1/skills/brand-report/body", headers=auth(token)).json()
        assert body["version"] == "3" and "先给结论" in body["body"]
        assert body["resources"] == ["references/checklist.md"]
        assert body["allowed_tools"] == ["math.add"]
        # 停用后不再出现在可用清单（安装 ≠ 授权）
        off = c.post("/v1/skills/brand-report/enabled", headers=auth(token),
                     json={"version": "3", "enabled": False})
        assert off.status_code == 200 and off.json()["enabled"] is False
        assert c.get("/v1/skills", headers=auth(token)).json() == []


# ------------------------------------------------------------------ guest 负向
def test_guest_cannot_manage_or_read_hub(make_system: Callable[..., SystemHandle]) -> None:
    with client(make_system) as (c, handle, token):
        register_server(c, token)
        created = c.post("/v1/demo/grants", headers=auth(token),
                         json={"label": "hub", "ttl_hours": 24}).json()
        gtok = c.post("/v1/auth/guest/redeem",
                      json={"code": created["invite_code"]}).json()["access_token"]
        g = auth(gtok)
        assert c.get("/v1/skills", headers=g).status_code in (401, 403)
        assert c.get("/v1/mcp/servers", headers=g).status_code in (401, 403)
        assert c.post("/v1/mcp/servers", headers=g, json={"server_id": "x", "url": "http://x"}
                      ).status_code in (401, 403, 422)
        assert c.post("/v1/mcp/servers/u4metrics/probe", headers=g).status_code in (401, 403)
        assert c.post("/v1/mcp/servers/u4metrics/enabled", headers=g,
                      json={"enabled": False}).status_code in (401, 403)
        assert c.delete("/v1/mcp/servers/u4metrics", headers=g).status_code in (401, 403)
        assert c.post("/v1/skills", headers=g, json={"source": "/tmp"}).status_code in (401, 403)
        assert c.get("/v1/skills/brand-report/body", headers=g).status_code in (401, 403)
