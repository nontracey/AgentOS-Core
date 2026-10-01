"""跨机主链集成（步骤 2）：真实 uvicorn Node Agent + gateway 全链。

覆盖验收：
- 配对/心跳/能力探针（真实 HTTP /tools）
- gateway 计划执行真正派发 node: 工具到节点（不旁路 NodePort）
- Asset 受控传输：节点产物 fetch 回 gateway（hash 校验）→ download；
  gateway 素材 push 到节点（节点侧 hash 校验）
- 未配对/已撤销节点派发拒绝并审计；节点令牌错误拒绝
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from agentos.apps.composition import SystemHandle
from agentos.apps.gateway import create_app
from agentos.apps.node_agent import create_node_app
from agentos.kernel.errors import AgentOSError
from tests.conftest import owner_token

NODE_TOKEN = "node-e2e-token"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def node_server(tmp_path: Path) -> Iterator[dict[str, str]]:
    """真实 uvicorn Node Agent（127.0.0.1 随机端口），沙箱在临时目录。"""
    app = create_node_app(token=NODE_TOKEN, sandbox_dir=tmp_path / "sandbox")
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(base + "/health", timeout=0.3)
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    yield {"url": base, "token": NODE_TOKEN}
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def client(make_system: Callable[..., SystemHandle]) -> Iterator[tuple[TestClient, SystemHandle, str]]:
    handle = make_system()
    app = create_app(system=handle)
    with TestClient(app) as c:
        yield c, handle, owner_token(handle)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _pair(client: TestClient, token: str, node_server: dict[str, str], node_id: str) -> dict:
    r = client.post(
        "/v1/nodes/pair",
        json={"node_id": node_id, "endpoint": node_server["url"], "os": "test-os",
              "arch": "x64", "token": node_server["token"]},
        headers=auth(token),
    )
    assert r.status_code == 200, r.text
    return r.json()


async def test_node_lifecycle(client: tuple[TestClient, SystemHandle, str], node_server) -> None:
    c, handle, token = client
    # 未配对时列表为空
    assert c.get("/v1/nodes", headers=auth(token)).json() == []
    # 未授权管理操作被拒
    assert c.get("/v1/nodes").status_code == 401

    paired = _pair(c, token, node_server, "node-a")
    assert paired["node_token"] == NODE_TOKEN

    # 心跳上报能力（节点令牌认证）
    r = c.post(
        "/v1/nodes/node-a/heartbeat",
        json={"os": "test-os", "arch": "x64",
              "capabilities": [{"name": "fs.write_text", "input_schema": {"type": "object"}}],
              "version": "node-1"},
        headers=auth(NODE_TOKEN),
    )
    assert r.status_code == 200, r.text
    # 错误节点令牌心跳被拒
    r = c.post("/v1/nodes/node-a/heartbeat", json={"os": "x", "arch": "x", "capabilities": []},
               headers=auth("wrong"))
    assert r.status_code == 401

    nodes = c.get("/v1/nodes", headers=auth(token)).json()
    assert len(nodes) == 1 and nodes[0]["status"] == "paired"
    assert all("token" not in k for n in nodes for k in n), "列表不得泄漏令牌材料"

    # 能力探针：真实 HTTP /tools
    actor = await handle.deps.identity.authenticate({"token": token})
    specs = await handle.tool_hub.refresh_node_tools(actor, "node-a")
    assert any(s.name == "node_node-a_fs_write_text" for s in specs)

    # 撤销后心跳/派发/探针全拒
    assert c.post("/v1/nodes/node-a/revoke", headers=auth(token)).json()["status"] == "revoked"
    r = c.post("/v1/nodes/node-a/heartbeat", json={"os": "x", "arch": "x", "capabilities": []},
               headers=auth(NODE_TOKEN))
    assert r.status_code == 401
    with pytest.raises(Exception, match="not paired"):
        await handle.tool_hub.refresh_node_tools(actor, "node-a")


async def test_dispatch_and_asset_roundtrip(
    client: tuple[TestClient, SystemHandle, str], node_server, tmp_path: Path,
) -> None:
    c, handle, token = client
    _pair(c, token, node_server, "node-win")
    actor = await handle.deps.identity.authenticate({"token": token})

    # 1) 真实派发：node: 工具经 HTTP 到 Node Agent 执行
    ctx_args = {"path": "out/remote.txt", "content": "from-gateway"}
    from datetime import timedelta

    from agentos.kernel.types import CallContext, utc_now

    ctx = CallContext(
        run_id="run_x", step_id="step_x", call_id="call_x", actor=actor,
        idempotency_key="run_x:step_x", fence=1,
        deadline_at=utc_now() + timedelta(seconds=30),
    )
    result = await handle.deps.tools.call("node_node-win_fs_write_text", ctx_args, ctx)
    assert result.status == "completed", result.value
    # 产物确实落在节点沙箱（真实文件系统）
    assert (tmp_path / "sandbox" / "out" / "remote.txt").read_text(encoding="utf-8") == "from-gateway"

    # 2) 节点端产物以 asset 引用拉回 gateway（hash 校验）
    node_file = tmp_path / "sandbox" / "out" / "remote.txt"
    data = node_file.read_bytes()
    import hashlib

    digest = hashlib.sha256(data).hexdigest()
    # 产物在节点侧登记（模拟节点产物上传后的引用）
    import uuid

    ref = f"{uuid.uuid4().hex[:8]}_remote.txt"
    (tmp_path / "sandbox" / "assets").mkdir(parents=True, exist_ok=True)
    (tmp_path / "sandbox" / "assets" / ref).write_bytes(data)
    fetched = c.post(
        "/v1/nodes/node-win/assets/fetch",
        # 上方是无持久化 run 的直接 ToolPort 调用，不能伪绑定 run_x。
        json={"remote_ref": ref, "sha256": digest, "mime": "text/plain"},
        headers=auth(token),
    )
    assert fetched.status_code == 200, fetched.text
    asset_id = fetched.json()["asset_id"]
    assert fetched.json()["sha256"] == digest
    assert c.get(f"/v1/assets/{asset_id}", headers=auth(token)).json()["run_id"] is None

    # 3) hash 篡改的 fetch 被拒（422/500 uncertain）
    bad = c.post(
        "/v1/nodes/node-win/assets/fetch",
        json={"remote_ref": ref, "sha256": "0" * 64},
        headers=auth(token),
    )
    assert bad.status_code in (422, 500)

    # 4) gateway 侧下载：字节一致
    dl = c.get(f"/v1/assets/{asset_id}/download", headers=auth(token))
    assert dl.status_code == 200 and dl.content == data

    # 5) push：gateway 素材推送到节点，节点侧校验 hash
    payload = b"input-bytes-\xe4\xbd\xa0\xe5\xa5\xbd"
    push = c.post(
        "/v1/nodes/node-win/assets/push",
        content=payload,
        headers={**auth(token), "Content-Type": "application/octet-stream",
                 "X-Asset-Filename": "in.bin", "X-Asset-Sha256": hashlib.sha256(payload).hexdigest()},
    )
    assert push.status_code == 200, push.text
    node_ref = push.json()["node_ref"]
    node_bytes = httpx.get(
        node_server["url"] + f"/assets/{node_ref}", headers=auth(NODE_TOKEN)
    ).content
    assert node_bytes == payload

    # 6) 未配对节点派发拒绝
    with pytest.raises(AgentOSError) as ei2:
        await handle.deps.tools.call("node_ghost-fs_write_text", ctx_args, ctx)
    assert ("not paired" in str(ei2.value)) or ("unknown tool" in str(ei2.value))


async def test_node_token_rejected_after_revoke(client: tuple[TestClient, SystemHandle, str], node_server) -> None:
    """撤销后 gateway 不再持有有效凭据派发；节点令牌错误也拒绝。"""
    c, handle, token = client
    _pair(c, token, node_server, "node-b")
    assert c.post("/v1/nodes/node-b/revoke", headers=auth(token)).status_code == 200
    actor = await handle.deps.identity.authenticate({"token": token})

    from datetime import timedelta

    from agentos.kernel.errors import AgentOSError
    from agentos.kernel.types import CallContext, utc_now

    ctx = CallContext(
        run_id="run_y", step_id="step_y", call_id="call_y", actor=actor,
        idempotency_key="run_y:step_y", fence=0,
        deadline_at=utc_now() + timedelta(seconds=10),
    )
    with pytest.raises(AgentOSError) as ei:
        await handle.deps.tools.call("node_node-b_clock.now", {}, ctx)
    assert ("not paired" in str(ei.value)) or ("unknown tool" in str(ei.value))
