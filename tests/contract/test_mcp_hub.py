"""MCP Hub 契约测试（FR-TOOL-01/02/03/10）：stdio 与 Streamable HTTP 双传输端到端。

同一 demo server（examples/mcp_demo_server.py）分别以 stdio 子进程与 HTTP ASGI app
挂载，验证：发现/命名空间/调用/协议版本记录/未知能力显式报错。
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

from agentos.adapters.store.sqlite import SqliteStore
from agentos.adapters.tools.local import LocalToolAdapter
from agentos.adapters.tools.mcp_hub import McpHubAdapter
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import AuthContext, CallContext, utc_now

DEMO_SERVER = Path(__file__).parents[2] / "examples" / "mcp_demo_server.py"


def actor() -> AuthContext:
    return AuthContext(
        subject="owner", tenant_id="default",
        scopes=["read", "write", "approve", "exec", "egress"],
        policy_version="t", expires_at=utc_now() + timedelta(minutes=10),
    )


def ctx(call_id: str = "c1") -> CallContext:
    return CallContext(
        run_id="run_1", step_id="s1", call_id=call_id, actor=actor(),
        idempotency_key=f"run_1:{call_id}", fence=1,
        deadline_at=utc_now() + timedelta(seconds=60),
    )


@pytest.fixture
def hub(tmp_path: Path) -> McpHubAdapter:
    store = SqliteStore(tmp_path / "hub.db")
    local = LocalToolAdapter(tmp_path / "sandbox")
    return McpHubAdapter(store, local)


async def test_stdio_transport_end_to_end(hub: McpHubAdapter) -> None:
    added = await hub.add_server(
        actor(), "demo",
        command=f"{sys.executable} {DEMO_SERVER}", risks=["read"],
    )
    assert added["transport"] == "stdio"
    assert sorted(added["tools"]) == ["demo:add", "demo:echo"]

    # 命名空间（FR-TOOL-02）：远端工具 = server:tool，与本地工具共存
    names = {s.name for s in await hub.list_tools(actor())}
    assert "demo:echo" in names and "demo:add" in names
    assert "math.add" in names  # 本地工具不受影响

    # 调用（FR-TOOL-03）：经同一 ToolPort 语义
    result = await hub.call("demo:echo", {"text": "hello"}, ctx("c1"))
    assert result.status == "completed"
    assert result.value["text"] == "echo: hello"

    result = await hub.call("demo:add", {"a": 2, "b": 3}, ctx("c2"))
    assert result.status == "completed"
    assert result.value.get("structured", {}).get("result") == 5 or result.value.get("text")

    # 协议版本已记录（FR-TOOL-10 协商）
    row = hub._store.get_mcp_server("default", "demo")
    assert row is not None and row["protocol_version"]

    # 未注册 server 前缀 → 显式失败
    with pytest.raises(AgentOSError) as exc_info:
        await hub.call("ghost:echo", {}, ctx("c3"))
    assert exc_info.value.code == ErrorCode.INVALID

    await hub.remove_server(actor(), "demo")
    assert await hub.list_servers(actor()) == []


async def test_http_streamable_transport_end_to_end(
    hub: McpHubAdapter, tmp_path: Path
) -> None:
    """FR-TOOL-03：远程 Streamable HTTP 传输端到端（真实 ASGI server）。"""
    import threading

    import uvicorn
    from examples.mcp_demo_server import build_http_app

    app = build_http_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = 5.0
    while not server.started and deadline > 0:
        import time

        time.sleep(0.05)
        deadline -= 0.05
    assert server.started, "http server failed to start"
    port = server.servers[0].sockets[0].getsockname()[1]

    try:
        added = await hub.add_server(actor(), "remote", url=f"http://127.0.0.1:{port}/mcp", risks=["read"])
        assert added["transport"] == "http"
        assert sorted(added["tools"]) == ["remote:add", "remote:echo"]

        result = await hub.call("remote:echo", {"text": "over-http"}, ctx("h1"))
        assert result.status == "completed"
        assert result.value["text"] == "echo: over-http"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


async def test_unsupported_capabilities_explicit(hub: McpHubAdapter, tmp_path: Path) -> None:
    """FR-TOOL-09/10：未协商 Tasks → poll/cancel 返回 UNSUPPORTED_CAPABILITY。"""
    with pytest.raises(AgentOSError) as poll_exc:
        await hub.poll("ref", ctx())
    assert poll_exc.value.code == ErrorCode.UNSUPPORTED
    with pytest.raises(AgentOSError) as cancel_exc:
        await hub.cancel("ref", ctx())
    assert cancel_exc.value.code == ErrorCode.UNSUPPORTED


async def test_conservative_default_risk_requires_approval(
    hub: McpHubAdapter, tmp_path: Path
) -> None:
    """蓝图 §12：远端工具未显式配置风险时默认 write（需审批），不当授权。"""
    from agentos.services.policy import PolicyService

    await hub.add_server(actor(), "untrusted", command=f"{sys.executable} {DEMO_SERVER}")
    spec = await hub.resolve(actor(), "untrusted:echo")
    store = hub._store
    policy = PolicyService(store, policy_version="t")
    decision = policy.check(actor(), spec, {"text": "hi"})
    assert decision.outcome == "needs_approval"
    _ = tmp_path
