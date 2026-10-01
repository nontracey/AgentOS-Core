"""Node Agent HTTP 契约（步骤 1 修复回归）。

覆盖文档 §4 验收：/tools 授权成功、无授权拒绝；/execute 执行与坏 token 拒绝；
关键回归：GET /tools 在事件循环内直接 await（旧实现 asyncio.run 必抛
RuntimeError，最小 HTTP 请求复现）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentos.apps.node_agent import create_node_app

TOKEN = "node-secret-token"


@pytest.fixture
def node_client(tmp_path: Path) -> TestClient:
    app = create_node_app(token=TOKEN, sandbox_dir=tmp_path / "sandbox")
    return TestClient(app)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_health_open(node_client: TestClient) -> None:
    r = node_client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "node": "agent"}


def test_tools_requires_token(node_client: TestClient) -> None:
    assert node_client.get("/tools").status_code == 401
    assert node_client.get("/tools", headers=auth("forged")).status_code == 401


def test_tools_in_event_loop(node_client: TestClient) -> None:
    """回归：旧实现在运行中的事件循环调 asyncio.run → 500 RuntimeError。"""
    r = node_client.get("/tools", headers=auth(TOKEN))
    assert r.status_code == 200, r.text
    names = {t["name"] for t in r.json()}
    assert {"math.add", "fs.write_text", "log.append"} <= names


def test_execute_roundtrip_and_bad_token(
    node_client: TestClient, tmp_path: Path
) -> None:
    body = {
        "tool_name": "fs.write_text",
        "args": {"path": "out/a.txt", "content": "hello node"},
        "call_id": "call_1",
        "step_id": "step_1",
        "run_id": "run_1",
        "idempotency_key": "idem_1",
        "fence": 3,
    }
    assert node_client.post("/execute", json=body).status_code == 401
    assert node_client.post("/execute", json=body, headers=auth("bad")).status_code == 401

    r = node_client.post("/execute", json=body, headers=auth(TOKEN))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed"
    written = (tmp_path / "sandbox" / "out" / "a.txt").read_text(encoding="utf-8")
    assert written == "hello node"


def test_execute_rejects_unknown_tool(node_client: TestClient) -> None:
    body = {
        "tool_name": "evil.exec",
        "args": {},
        "call_id": "call_2",
        "step_id": "step_2",
        "run_id": "run_2",
        "idempotency_key": "idem_2",
    }
    r = node_client.post("/execute", json=body, headers=auth(TOKEN))
    assert r.status_code == 200
    payload = r.json()
    # 工具未注册：节点侧明确 failed 回执而非伪成功
    assert payload["status"] == "failed"
    assert "unknown tool" in payload["value"]["message"]


def test_auth_survives_beyond_one_hour(node_client: TestClient) -> None:
    """回归：旧实现 AuthContext 启动时固定 1 小时，长任务必失效。

    这里通过公开行为验证：连续两轮请求之间无失效；并且身份 TTL=24h
    由实现常量保证（AUTH_TTL），避免再引入启动时冻结的短 TTL。
    """
    from agentos.apps.node_agent import AUTH_TTL

    assert AUTH_TTL.total_seconds() >= 24 * 3600
    for i in range(2):
        r = node_client.get("/tools", headers=auth(TOKEN))
        assert r.status_code == 200, f"round {i}: {r.text}"
