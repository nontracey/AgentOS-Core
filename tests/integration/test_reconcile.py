"""对账端到端（FR-ORC-08 / F1）：uncertain 步骤 → 探针外部状态 → 补传或授权重放。

用真实 uvicorn Node Agent + 假 ComfyUI 适配器（可控 generate 行为与 probe 回执），
走完整 worker 驱动链路，验证：
- 无外部效果 → reconcile 授权重放 → 生成恰一次成功 → run succeeded
- 外部已完成 → reconcile 直接补传为 completed → 不重复执行副作用
- 外部仍在执行 → 409 拒绝重放，run 保持 paused
"""

from __future__ import annotations

import socket
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from agentos.apps.composition import SystemHandle
from agentos.apps.gateway import create_app
from agentos.apps.node_agent import create_node_app
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import AuthContext, ToolSpec
from tests.conftest import owner_token

NODE_TOKEN = "reconcile-test-token"


class FakeComfyAdapter:
    """节点侧假 ComfyUI：generate 首次抛 TIMEOUT（模拟效果未知），probe 按剧本回执。"""

    def __init__(self) -> None:
        self.generate_calls = 0
        self.probe_calls: list[dict[str, Any]] = []
        self.generate_complete = False
        self.probe_response: dict[str, Any] = {"found": False, "in_flight": False}

    def has_tool(self, name: str) -> bool:
        return name in {"comfyui.generate", "comfyui.probe"}

    def list_tools(self, actor: AuthContext) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="comfyui.generate", version="1.0.0", description="fake generate",
                input_schema={"type": "object", "properties": {"prompt": {"type": "string"}},
                              "additionalProperties": False},
                risks=["read"], recovery="manual", timeout_ms=60_000,
            ),
            ToolSpec(
                name="comfyui.probe", version="1.0.0", description="fake probe",
                input_schema={"type": "object", "properties": {"idem_tag": {"type": "string"}},
                              "required": ["idem_tag"], "additionalProperties": False},
                risks=["read"], recovery="pure", timeout_ms=10_000,
            ),
        ]

    async def call(self, name: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        if name == "comfyui.generate":
            self.generate_calls += 1
            if self.generate_calls == 1 and not self.generate_complete:
                # 首次执行抛效果未知错误（模拟提交后连接丢失）
                raise AgentOSError(ErrorCode.TIMEOUT, "comfyui submit lost (fake)")
            return {"status": "completed",
                    "value": {"asset_ref": f"fake_{self.generate_calls}.png",
                              "sha256": "a" * 64}}
        if name == "comfyui.probe":
            self.probe_calls.append(dict(args))
            return {"status": "completed", "value": dict(self.probe_response)}
        raise AgentOSError(ErrorCode.INVALID, f"unknown: {name}")


@pytest.fixture
def node_server(tmp_path: Path) -> Callable[[], dict[str, Any]]:
    """起一个真实 uvicorn Node Agent，返回 (url, adapter) 的惰性持有者。"""
    holder: dict[str, Any] = {}

    def _start(adapter: FakeComfyAdapter) -> dict[str, Any]:
        app = create_node_app(
            token=NODE_TOKEN, sandbox_dir=tmp_path / f"sandbox-{uuid.uuid4().hex[:6]}",
            extra_tools=[adapter],
        )
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                               log_level="error"))
        threading.Thread(target=server.run, daemon=True).start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                httpx.get(f"http://127.0.0.1:{port}/health", timeout=1)
                break
            except httpx.HTTPError:
                time.sleep(0.05)
        holder["url"] = f"http://127.0.0.1:{port}"
        holder["adapter"] = adapter
        return holder

    return _start


def _client(make_system: Callable[..., SystemHandle]) -> tuple[TestClient, SystemHandle, str]:
    handle = make_system(model_script=[
        {"tool_calls": [{"id": "c1", "name": "node_node-win_comfyui_generate",
                         "arguments": {"prompt": "city neon"}}]},
        {"content": "delivered"},
    ])
    app = create_app(system=handle)
    c = TestClient(app)
    c.__enter__()
    return c, handle, owner_token(handle)


def _wait_state(c: TestClient, token: str, run_id: str, states: set[str],
                timeout: float = 15.0) -> str:
    deadline = time.monotonic() + timeout
    state = ""
    while time.monotonic() < deadline:
        state = c.get(f"/v1/tasks/{run_id}",
                      headers={"Authorization": f"Bearer {token}"}).json()["run"]["state"]
        if state in states:
            return state
        time.sleep(0.05)
    return state


def _pair(c: TestClient, token: str, url: str) -> None:
    r = c.post("/v1/nodes/pair", headers={"Authorization": f"Bearer {token}"},
               json={"node_id": "node-win", "endpoint": url, "os": "test", "arch": "x64",
                     "token": NODE_TOKEN})
    assert r.status_code == 200, r.text


def test_reconcile_replays_when_no_external_effect(
    make_system: Callable[..., SystemHandle], node_server: Callable[..., dict[str, Any]],
) -> None:
    """生成抛效果未知错误 → run paused → 对账探针无效果 → 授权重放 → 恰一次成功。"""
    fake = FakeComfyAdapter()
    holder = node_server(fake)
    c, handle, token = _client(make_system)
    try:
        _pair(c, token, holder["url"])
        r = c.post("/v1/tasks", json={"payload": {"goal": "gen"}},
                   headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 202, r.text
        run_id = r.json()["run_id"]
        assert _wait_state(c, token, run_id, {"paused"}) == "paused"

        step = handle.store.list_steps("default", run_id)[0]
        assert step.state.value == "uncertain"

        rec = c.post(f"/v1/tasks/{run_id}/reconcile", json={"step_id": step.id},
                     headers={"Authorization": f"Bearer {token}"})
        assert rec.status_code == 202, rec.text
        assert rec.json()["reconciled"] == "replay_authorized"
        assert fake.probe_calls and fake.probe_calls[0]["idem_tag"].startswith("agentos-")

        assert _wait_state(c, token, run_id, {"succeeded", "failed"}) == "succeeded"
        assert fake.generate_calls == 2  # 首次(效果未知) + 对账后重放一次
        final = handle.store.get_step("default", run_id, step.id)
        assert final is not None and final.state.value == "completed"
        events = handle.store.event_list("default", run_id, limit=100)
        assert any(e["kind"] == "step.reconcile" for e in events)
    finally:
        c.__exit__(None, None, None)


def test_reconcile_completes_from_external_without_regeneration(
    make_system: Callable[..., SystemHandle], node_server: Callable[..., dict[str, Any]],
) -> None:
    """外部实际已完成（响应丢失场景）→ 对账补传产物 → generate 不重跑（不重复副作用）。"""
    fake = FakeComfyAdapter()
    fake.probe_response = {
        "found": True, "in_flight": False,
        "result": {"status": "completed",
                   "value": {"asset_ref": "lost_1.png", "sha256": "b" * 64}},
    }
    # generate 首次抛错后保持不完成——模拟"提交成功但响应丢失"，重放若发生会再次调用
    holder = node_server(fake)
    c, handle, token = _client(make_system)
    try:
        _pair(c, token, holder["url"])
        r = c.post("/v1/tasks", json={"payload": {"goal": "gen"}},
                   headers={"Authorization": f"Bearer {token}"})
        run_id = r.json()["run_id"]
        assert _wait_state(c, token, run_id, {"paused"}) == "paused"
        step = handle.store.list_steps("default", run_id)[0]

        rec = c.post(f"/v1/tasks/{run_id}/reconcile", json={"step_id": step.id},
                     headers={"Authorization": f"Bearer {token}"})
        assert rec.status_code == 202, rec.text
        assert rec.json()["reconciled"] == "completed_from_external"
        assert rec.json()["value"]["asset_ref"] == "lost_1.png"

        assert _wait_state(c, token, run_id, {"succeeded", "failed"}) == "succeeded"
        assert fake.generate_calls == 1  # 重放未发生：外部产物直接补传
        final = handle.store.get_step("default", run_id, step.id)
        assert final is not None and final.state.value == "completed"
        assert final.result_json is not None and "lost_1.png" in final.result_json
    finally:
        c.__exit__(None, None, None)


def test_reconcile_rejects_while_external_in_flight(
    make_system: Callable[..., SystemHandle], node_server: Callable[..., dict[str, Any]],
) -> None:
    """外部仍在执行 → 409，禁止此刻重放；run 保持 paused 等待后续对账。"""
    fake = FakeComfyAdapter()
    fake.probe_response = {"found": False, "in_flight": True,
                           "note": "generation still running"}
    holder = node_server(fake)
    c, handle, token = _client(make_system)
    try:
        _pair(c, token, holder["url"])
        r = c.post("/v1/tasks", json={"payload": {"goal": "gen"}},
                   headers={"Authorization": f"Bearer {token}"})
        run_id = r.json()["run_id"]
        assert _wait_state(c, token, run_id, {"paused"}) == "paused"
        step = handle.store.list_steps("default", run_id)[0]

        rec = c.post(f"/v1/tasks/{run_id}/reconcile", json={"step_id": step.id},
                     headers={"Authorization": f"Bearer {token}"})
        assert rec.status_code == 409
        assert fake.generate_calls == 1  # 未触发重放
        assert handle.store.get_run("default", run_id).state.value == "paused"
    finally:
        c.__exit__(None, None, None)


class FakeIdemAdapter:
    """节点侧假幂等工具：首次抛 TIMEOUT（效果未知），重放安全与否看自声明 recovery。"""

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}

    def has_tool(self, name: str) -> bool:
        return name in {"safeidem.run", "unsafemanual.run"}

    def list_tools(self, actor: AuthContext) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="safeidem.run", version="1.0.0", description="idempotent by contract",
                input_schema={"type": "object", "additionalProperties": False},
                risks=["write"], recovery="idempotency_key", timeout_ms=60_000,
            ),
            ToolSpec(
                name="unsafemanual.run", version="1.0.0", description="manual recovery only",
                input_schema={"type": "object", "additionalProperties": False},
                risks=["write"], recovery="manual", timeout_ms=60_000,
            ),
        ]

    async def call(self, name: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        self.calls[name] = self.calls.get(name, 0) + 1
        if self.calls[name] == 1:
            raise AgentOSError(ErrorCode.TIMEOUT, "first call lost (fake)")
        return {"status": "completed", "value": {"run": self.calls[name]}}


def _client_script(
    make_system: Callable[..., SystemHandle], tool_name: str,
) -> tuple[TestClient, SystemHandle, str]:
    handle = make_system(model_script=[
        {"tool_calls": [{"id": "c1", "name": tool_name, "arguments": {}}]},
        {"content": "delivered"},
    ])
    app = create_app(system=handle)
    c = TestClient(app)
    c.__enter__()
    return c, handle, owner_token(handle)


def _approve_pending(c: TestClient, token: str, run_id: str) -> None:
    """写操作走审批：把本 run 的 pending 审批全部批准（与演练等待循环同语义）。"""
    approvals = c.get("/v1/approvals", headers={"Authorization": f"Bearer {token}"}).json()
    for a in approvals if isinstance(approvals, list) else []:
        if a.get("state") == "pending" and run_id in str(a.get("call_id") or ""):
            r = c.post(f"/v1/approvals/{a['id']}/decision",
                       headers={"Authorization": f"Bearer {token}"},
                       json={"decision": "approve"})
            assert r.status_code == 200, r.text


def test_reconcile_idempotent_replay_without_probe(
    make_system: Callable[..., SystemHandle], node_server: Callable[..., dict[str, Any]],
) -> None:
    """无探针但底层工具声明幂等键 → 对账授权重放 → 同幂等键恰好一次成功（F1 路径）。"""
    fake = FakeIdemAdapter()
    holder = node_server(fake)
    c, handle, token = _client_script(make_system, "node_node-win_safeidem_run")
    try:
        _pair(c, token, holder["url"])
        r = c.post("/v1/tasks", json={"payload": {"goal": "run"}},
                   headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 202, r.text
        run_id = r.json()["run_id"]
        state = _wait_state(c, token, run_id, {"paused", "awaiting_approval"})
        if state == "awaiting_approval":
            _approve_pending(c, token, run_id)
        assert _wait_state(c, token, run_id, {"paused"}) == "paused"
        step = handle.store.list_steps("default", run_id)[0]
        assert step.state.value == "uncertain"

        rec = c.post(f"/v1/tasks/{run_id}/reconcile", json={"step_id": step.id},
                     headers={"Authorization": f"Bearer {token}"})
        assert rec.status_code == 202, rec.text
        assert rec.json()["reconciled"] == "replay_authorized"
        assert rec.json()["mode"] == "idempotent_replay"

        assert _wait_state(c, token, run_id, {"succeeded", "failed"}) == "succeeded"
        assert fake.calls["safeidem.run"] == 2  # 首次(效果未知) + 对账后重放一次
        final = handle.store.get_step("default", run_id, step.id)
        assert final is not None and final.state.value == "completed"
        events = handle.store.event_list("default", run_id, limit=100)
        assert any(e["kind"] == "step.reconcile" and
                   e["payload"].get("mode") == "idempotent_replay" for e in events)
    finally:
        c.__exit__(None, None, None)


def test_reconcile_refuses_manual_tool_without_probe(
    make_system: Callable[..., SystemHandle], node_server: Callable[..., dict[str, Any]],
) -> None:
    """无探针且底层声明 manual → 501 拒绝重放（不因无探针就放行任意重试）。"""
    fake = FakeIdemAdapter()
    holder = node_server(fake)
    c, handle, token = _client_script(make_system, "node_node-win_unsafemanual_run")
    try:
        _pair(c, token, holder["url"])
        r = c.post("/v1/tasks", json={"payload": {"goal": "run"}},
                   headers={"Authorization": f"Bearer {token}"})
        run_id = r.json()["run_id"]
        state = _wait_state(c, token, run_id, {"paused", "awaiting_approval"})
        if state == "awaiting_approval":
            _approve_pending(c, token, run_id)
        assert _wait_state(c, token, run_id, {"paused"}) == "paused"
        step = handle.store.list_steps("default", run_id)[0]

        rec = c.post(f"/v1/tasks/{run_id}/reconcile", json={"step_id": step.id},
                     headers={"Authorization": f"Bearer {token}"})
        assert rec.status_code == 501
        assert fake.calls.get("unsafemanual.run", 0) == 1  # 未触发重放
        assert handle.store.get_run("default", run_id).state.value == "paused"
    finally:
        c.__exit__(None, None, None)


def test_reconcile_rejects_non_uncertain_step(
    make_system: Callable[..., SystemHandle], node_server: Callable[..., dict[str, Any]],
) -> None:
    """只有 uncertain 步骤可对账；其余状态 409，防止误用绕过状态机。"""
    fake = FakeComfyAdapter()
    fake.generate_complete = True  # 一次成功，run 直接走完
    holder = node_server(fake)
    c, handle, token = _client(make_system)
    try:
        _pair(c, token, holder["url"])
        r = c.post("/v1/tasks", json={"payload": {"goal": "gen"}},
                   headers={"Authorization": f"Bearer {token}"})
        run_id = r.json()["run_id"]
        assert _wait_state(c, token, run_id, {"succeeded"}) == "succeeded"
        step = handle.store.list_steps("default", run_id)[0]
        rec = c.post(f"/v1/tasks/{run_id}/reconcile", json={"step_id": step.id},
                     headers={"Authorization": f"Bearer {token}"})
        assert rec.status_code == 409
    finally:
        c.__exit__(None, None, None)


def test_paused_run_with_uncertain_step_not_woken_by_job(
    make_system: Callable[..., SystemHandle], node_server: Callable[..., dict[str, Any]],
) -> None:
    """排队 job 不得唤醒待对账 run（FR-ORC-08）。

    D 盲跑第3轮根因：drive_claimed 入口把 PAUSED 无条件拉回 RUNNING，
    回卷截断绕过 uncertain 步骤重新调模型 → run succeeded 而 step 永远 uncertain。
    守卫：uncertain 只能经 reconcile 端点清除。
    """
    import asyncio

    fake = FakeComfyAdapter()
    holder = node_server(fake)
    c, handle, token = _client(make_system)
    try:
        _pair(c, token, holder["url"])
        r = c.post("/v1/tasks", json={"payload": {"goal": "gen"}},
                   headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 202, r.text
        run_id = r.json()["run_id"]
        assert _wait_state(c, token, run_id, {"paused"}) == "paused"

        # 模拟队列残留 job 直接驱动：不得拉回 RUNNING，不得盲重试
        asyncio.run(handle.worker.drive_until_settled(run_id, deadline_s=5))
        run = handle.store.get_run("default", run_id)
        assert run is not None and run.state.value == "paused"
        assert fake.generate_calls == 1  # 未重放

        # reconcile 清除 uncertain 后，驱动恢复正常（既有语义不回归）
        step = handle.store.list_steps("default", run_id)[0]
        rec = c.post(f"/v1/tasks/{run_id}/reconcile", json={"step_id": step.id},
                     headers={"Authorization": f"Bearer {token}"})
        assert rec.status_code == 202, rec.text
        assert _wait_state(c, token, run_id, {"succeeded", "failed"}) == "succeeded"
        assert fake.generate_calls == 2
    finally:
        c.__exit__(None, None, None)
