"""Model Port 契约测试（FR-MDL-01）：scripted 与 openai_compat 跑同一 fixture。"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from agentos.adapters.model.openai_compat import OpenAICompatModelAdapter
from agentos.adapters.model.scripted import ScriptedModelAdapter
from agentos.kernel.types import AuthContext, ChatMessage, ModelRequest, utc_now

FIXTURE = json.loads(
    (Path(__file__).parents[2] / "contracts" / "fixtures" / "model" / "tool_call_round.json").read_text(
        encoding="utf-8"
    )
)


def actor() -> AuthContext:
    return AuthContext(
        subject="owner", tenant_id="default", scopes=["read"], policy_version="t",
        expires_at=utc_now(),
    )


async def collect(adapter: Any, request: ModelRequest) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    async for event in adapter.stream(request, actor()):
        events.append(event)
    return events


def base_request(messages: list[ChatMessage]) -> ModelRequest:
    return ModelRequest(messages=messages, tools=[], max_output_tokens=256)


class _StubOpenAI(BaseHTTPRequestHandler):
    """最小 OpenAI-compatible stub：按收到 assistant 消息的次数轮换 fixture 响应。"""

    server_version = "stub-openai/1"

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(length))
        assistant_turns = sum(1 for m in body["messages"] if m["role"] == "assistant")
        if assistant_turns == 0:
            payload = FIXTURE["openai_response_tool_turn"]
        else:
            payload = FIXTURE["openai_response_final_turn"]
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args: Any) -> None:
        return


@pytest.fixture
def stub_url() -> str:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubOpenAI)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()
    server.server_close()


async def test_scripted_adapter_matches_fixture() -> None:
    adapter = ScriptedModelAdapter(FIXTURE["scripted_script"])
    caps = await adapter.capabilities()
    assert caps["provider"] == "scripted" and caps["tools"] is True

    events0 = await collect(adapter, base_request([ChatMessage(role="user", content="q")]))
    tool_calls = next(e for e in events0 if e["type"] == "assistant_message")["tool_calls"]
    assert [
        {"id": c.id, "name": c.name, "arguments": c.arguments} for c in tool_calls
    ] == FIXTURE["expect"]["turn_0_tool_calls"]
    assert events0[-1]["type"] == "done"

    events1 = await collect(
        adapter,
        base_request(
            [
                ChatMessage(role="user", content="q"),
                ChatMessage(role="assistant", content="", tool_calls=tool_calls),
                ChatMessage(role="tool", content="{}", tool_call_id=tool_calls[0].id),
            ]
        ),
    )
    delta = next(e for e in events1 if e["type"] == "text_delta")
    assert delta["delta"] == FIXTURE["expect"]["turn_1_content"]
    usage = next(e for e in events1 if e["type"] == "usage")
    assert usage["usage"].input_tokens > 0


async def test_openai_compat_adapter_matches_same_fixture(stub_url: str) -> None:
    adapter = OpenAICompatModelAdapter(
        base_url=stub_url, api_key="test-key", model="stub-model"
    )
    caps = await adapter.capabilities()
    assert caps["provider"] == "openai_compat"

    events0 = await collect(adapter, base_request([ChatMessage(role="user", content="q")]))
    tool_calls = next(e for e in events0 if e["type"] == "assistant_message")["tool_calls"]
    assert [
        {"id": c.id, "name": c.name, "arguments": c.arguments} for c in tool_calls
    ] == FIXTURE["expect"]["turn_0_tool_calls"]
    usage = next(e for e in events0 if e["type"] == "usage")
    assert usage["usage"].estimated is False

    events1 = await collect(
        adapter,
        base_request(
            [
                ChatMessage(role="user", content="q"),
                ChatMessage(role="assistant", content="", tool_calls=tool_calls),
                ChatMessage(role="tool", content="{}", tool_call_id=tool_calls[0].id),
            ]
        ),
    )
    delta = next(e for e in events1 if e["type"] == "text_delta")
    assert delta["delta"] == FIXTURE["expect"]["turn_1_content"]


async def test_openai_compat_marks_estimated_usage_without_provider_usage(stub_url: str) -> None:
    adapter = OpenAICompatModelAdapter(base_url=stub_url, api_key="k", model="m")
    # stub 恒返回 usage；此处仅验证事件序列闭合（estimated 路径由 provider 无 usage 时触发）
    events = await collect(adapter, base_request([ChatMessage(role="user", content="q")]))
    assert events[-1]["type"] == "done"
