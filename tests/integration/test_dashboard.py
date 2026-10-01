"""全局控制台（Dashboard）端点测试：静态 SPA + 聚合 API（FR-EDG-04/07 M1 界面基础）。"""

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient

from agentos.apps.composition import SystemHandle
from agentos.apps.gateway import create_app
from tests.conftest import WRITE_FLOW_SCRIPT, owner_token


def test_ui_served(make_system: Callable[..., SystemHandle]) -> None:
    handle = make_system(model_script=WRITE_FLOW_SCRIPT)
    app = create_app(system=handle)
    with TestClient(app) as client:
        r = client.get("/ui/")
        assert r.status_code == 200
        assert "AgentOS 工作台" in r.text
        # 旧控制台过渡期保留可运行
        r2 = client.get("/ui/legacy.html")
        assert r2.status_code == 200
        assert "AgentOS 控制台" in r2.text


def test_runs_list_and_mcp_servers(make_system: Callable[..., SystemHandle]) -> None:
    handle = make_system(model_script=WRITE_FLOW_SCRIPT)
    app = create_app(system=handle)
    with TestClient(app) as client:
        token = owner_token(handle)
        headers = {"Authorization": f"Bearer {token}"}
        r = client.post("/v1/tasks", json={"payload": {"goal": "demo"}}, headers=headers)
        assert r.status_code == 202
        runs = client.get("/v1/runs", headers=headers).json()
        assert any(x["goal"].startswith("demo") for x in runs)
        servers = client.get("/v1/mcp/servers", headers=headers).json()
        assert servers == []
