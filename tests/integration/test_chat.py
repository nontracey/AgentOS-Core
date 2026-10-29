"""Direct chat: discovery, streaming, thinking, regenerate/edit/stop, egress, audit and card sharing.

A mock transport stands in for Ollama / OpenAI-compatible servers; no real model runs.
All model names, hosts and texts are synthetic.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from agentos.adapters.model import chat_endpoints
from agentos.adapters.model.chat_endpoints import ChatEndpoint, _InlineThinkSplitter
from agentos.apps.composition import SystemHandle
from agentos.apps.gateway import create_app
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.services.chat import estimate_tokens, parse_json_object, title_from
from tests.conftest import owner_token

MODELS = {
    "reasoner:30b": {"caps": ["completion", "tools", "thinking"], "family": "moe", "ctx": 262144},
    "vision:7b": {"caps": ["completion", "vision"], "family": "vl", "ctx": 128000},
    "plain:4b": {"caps": ["completion"], "family": "dense", "ctx": 32768},
    "embed:latest": {"caps": ["embedding"], "family": "bert", "ctx": 8192},
}


class FakeModelServer:
    """Records requests and streams deterministic NDJSON / SSE answers."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.loaded = {"reasoner:30b"}
        self.slow = 0.0

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content or b"{}") if request.content else {}
        self.requests.append({"path": path, "body": body})
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [
                {"name": n, "size": 21_000_000_000, "details": {"family": m["family"], "parameter_size": "35B",
                                                               "quantization_level": "Q4_K_M"}}
                for n, m in MODELS.items()]})
        if path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": n, "size_vram": 9_000_000_000}
                                                        for n in sorted(self.loaded)]})
        if path == "/api/show":
            meta = MODELS[body["model"]]
            return httpx.Response(200, json={"capabilities": meta["caps"],
                                             "model_info": {meta["family"] + ".context_length": meta["ctx"]}})
        if path == "/api/generate":
            self.loaded.discard(body["model"])
            return httpx.Response(200, json={"done": True})
        if path == "/api/chat":
            if body["model"] == "plain:4b" and "think" in body:
                return httpx.Response(400, json={"error": '"plain:4b" does not support thinking'})
            self.loaded.add(body["model"])
            last = body["messages"][-1]["content"]
            lines = []
            if body.get("think"):
                lines.append({"message": {"role": "assistant", "content": "", "thinking": "先想一想。"},
                              "done": False})
            pieces = ["你好，", "收到：", last[:20]] + (["慢"] * 40 if self.slow else [])
            for piece in pieces:
                lines.append({"message": {"role": "assistant", "content": piece}, "done": False})
            lines.append({"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
                          "eval_count": 30, "eval_duration": 1_500_000_000, "prompt_eval_count": 12,
                          "load_duration": 200_000_000})
            slow = self.slow

            async def stream() -> AsyncIterator[bytes]:
                for item in lines:
                    if slow:
                        await asyncio.sleep(slow)
                    yield (json.dumps(item, ensure_ascii=False) + "\n").encode()

            return httpx.Response(200, content=stream())
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "gateway-model"}]})
        if path == "/v1/chat/completions":
            chunks = [{"choices": [{"delta": {"content": "<think>内部推理</think>"}}]},
                      {"choices": [{"delta": {"content": "网关回答"}}]},
                      {"choices": [{"delta": {}, "finish_reason": "stop"}],
                       "usage": {"completion_tokens": 4, "prompt_tokens": 9}}]
            payload = "".join("data: " + json.dumps(c, ensure_ascii=False) + "\n\n" for c in chunks)
            payload += "data: [DONE]\n\n"
            return httpx.Response(200, content=payload.encode())
        return httpx.Response(404, json={"error": "not found"})


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeModelServer:
    server = FakeModelServer()

    def client(endpoint: ChatEndpoint, timeout: float | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=endpoint.base_url, transport=httpx.MockTransport(server.handler))

    monkeypatch.setattr(chat_endpoints, "_client", client)
    return server


ENDPOINTS = [{"id": "lab", "label": "GPU 服务器", "kind": "ollama", "base_url": "http://gpu.example:11434",
              "gpu_shared": True},
             {"id": "gw", "label": "局域网网关", "kind": "openai", "base_url": "http://gw.example:4000/v1",
              "api_key": "secret-key", "egress": "local"}]


@contextmanager
def chat_client(make_system: Callable[..., SystemHandle], **settings: Any
                ) -> Iterator[tuple[TestClient, SystemHandle, dict[str, str]]]:
    handle = make_system(chat_endpoints=settings.pop("endpoints", ENDPOINTS),
                         chat_default_model=settings.pop("chat_default_model", "lab/reasoner:30b"), **settings)
    with TestClient(create_app(system=handle)) as c:
        yield c, handle, {"Authorization": f"Bearer {owner_token(handle)}"}


def read_events(c: TestClient, url: str, headers: dict[str, str], body: dict[str, Any]) -> list[dict[str, Any]]:
    events = []
    with c.stream("POST", url, headers=headers, json=body) as response:
        assert response.status_code == 200, response.read()
        for line in response.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def test_models_hide_embeddings_and_credentials(make_system: Callable[..., SystemHandle],
                                                fake: FakeModelServer) -> None:
    with chat_client(make_system) as (c, _handle, headers):
        body = c.get("/v1/chat/models", headers=headers).json()
        names = {m["ref"] for m in body["models"]}
        assert "lab/reasoner:30b" in names and "gw/gateway-model" in names
        assert "lab/embed:latest" not in names
        reasoner = next(m for m in body["models"] if m["name"] == "reasoner:30b")
        assert reasoner["loaded"] and "thinking" in reasoner["capabilities"] and reasoner["context_length"] == 262144
        assert body["default"] == "lab/reasoner:30b"
        assert "secret-key" not in json.dumps(body)
        assert {e["id"] for e in body["endpoints"] if e["online"]} == {"lab", "gw"}


def test_stream_thinking_answer_and_persist(make_system: Callable[..., SystemHandle], fake: FakeModelServer) -> None:
    with chat_client(make_system) as (c, _handle, headers):
        conversation = c.post("/v1/chat/conversations", headers=headers, json={}).json()
        events = read_events(c, f"/v1/chat/conversations/{conversation['id']}/messages", headers,
                             {"content": "帮我写一个开场"})
        kinds = [e["type"] for e in events]
        assert kinds[0] == "start" and "thinking" in kinds and "delta" in kinds and kinds[-1] == "end"
        done = next(e for e in events if e["type"] == "done")["message"]
        assert done["content"] == "你好，收到：帮我写一个开场" and done["thinking"] == "先想一想。"
        assert done["stats"]["tokens_per_second"] == 20.0 and done["status"] == "complete"
        chat_request = next(r for r in fake.requests if r["path"] == "/api/chat")
        assert chat_request["body"]["think"] is True and chat_request["body"]["options"]["num_ctx"] == 8192
        assert chat_request["body"]["messages"][0]["role"] == "system"
        detail = c.get(f"/v1/chat/conversations/{conversation['id']}", headers=headers).json()
        assert detail["title"] == "帮我写一个开场" and [m["role"] for m in detail["messages"]] == ["user", "assistant"]
        listing = c.get("/v1/chat/conversations", headers=headers, params={"q": "开场"}).json()
        assert listing[0]["message_count"] == 2


def test_regenerate_and_edit_supersede_history(make_system: Callable[..., SystemHandle],
                                               fake: FakeModelServer) -> None:
    with chat_client(make_system) as (c, _handle, headers):
        cid = c.post("/v1/chat/conversations", headers=headers, json={}).json()["id"]
        url = f"/v1/chat/conversations/{cid}/messages"
        read_events(c, url, headers, {"content": "第一问"})
        read_events(c, url, headers, {"regenerate": True})
        messages = c.get(f"/v1/chat/conversations/{cid}", headers=headers).json()["messages"]
        assert [m["role"] for m in messages] == ["user", "assistant"]
        first_user = messages[0]["id"]
        read_events(c, url, headers, {"content": "改过的问题", "edit_message_id": first_user})
        messages = c.get(f"/v1/chat/conversations/{cid}", headers=headers).json()["messages"]
        assert [m["content"] for m in messages if m["role"] == "user"] == ["改过的问题"]
        assert messages[-1]["content"].endswith("改过的问题")


def test_model_without_reasoning_retries_without_think(make_system: Callable[..., SystemHandle],
                                                       fake: FakeModelServer) -> None:
    with chat_client(make_system) as (c, _handle, headers):
        cid = c.post("/v1/chat/conversations", headers=headers,
                     json={"model": "lab/plain:4b", "params": {"think": "on"}}).json()["id"]
        events = read_events(c, f"/v1/chat/conversations/{cid}/messages", headers, {"content": "hi"})
        assert next(e for e in events if e["type"] == "done")["message"]["status"] == "complete"
        attempts = [r["body"] for r in fake.requests if r["path"] == "/api/chat"]
        assert "think" in attempts[0] and "think" not in attempts[1]


def test_openai_endpoint_splits_inline_thinking(make_system: Callable[..., SystemHandle],
                                                fake: FakeModelServer) -> None:
    with chat_client(make_system) as (c, _handle, headers):
        cid = c.post("/v1/chat/conversations", headers=headers, json={"model": "gw/gateway-model"}).json()["id"]
        events = read_events(c, f"/v1/chat/conversations/{cid}/messages", headers, {"content": "hi"})
        done = next(e for e in events if e["type"] == "done")["message"]
        assert done["content"] == "网关回答" and done["thinking"] == "内部推理"
        assert done["stats"]["output_tokens"] == 4


def test_chat_waits_while_generation_holds_shared_card(make_system: Callable[..., SystemHandle],
                                                       fake: FakeModelServer) -> None:
    with chat_client(make_system) as (c, handle, headers):
        cid = c.post("/v1/chat/conversations", headers=headers, json={}).json()["id"]
        release = threading.Event()
        holding = threading.Event()

        async def hold_gpu() -> None:
            async with handle.accelerator.hold("generation", "第 3 镜画面"):
                holding.set()
                while not release.is_set():
                    await asyncio.sleep(0.02)

        portal = c.portal
        assert portal is not None
        future = portal.start_task_soon(hold_gpu)
        assert holding.wait(5)
        threading.Timer(0.4, release.set).start()
        events = read_events(c, f"/v1/chat/conversations/{cid}/messages", headers, {"content": "排队测试"})
        waiting = next(e for e in events if e["type"] == "status" and e["status"] == "waiting_gpu")
        assert waiting["gpu"]["holder"]["label"] == "第 3 镜画面"
        assert next(e for e in events if e["type"] == "done")["message"]["status"] == "complete"
        future.result(timeout=5)


def test_stop_keeps_partial_answer(make_system: Callable[..., SystemHandle], fake: FakeModelServer) -> None:
    fake.slow = 0.05
    with chat_client(make_system) as (c, _handle, headers):
        cid = c.post("/v1/chat/conversations", headers=headers, json={}).json()["id"]
        result: list[list[dict[str, Any]]] = []
        reader = threading.Thread(target=lambda: result.append(
            read_events(c, f"/v1/chat/conversations/{cid}/messages", headers, {"content": "长回答"})))
        reader.start()
        for _ in range(100):
            if c.get("/v1/chat/conversations", headers=headers).json()[0]["live"]:
                break
            time.sleep(0.02)
        time.sleep(0.3)
        assert c.post(f"/v1/chat/conversations/{cid}/stop", headers=headers).json() == {"stopped": True}
        reader.join(10)
        final = c.get(f"/v1/chat/conversations/{cid}", headers=headers).json()["messages"][-1]
        assert final["status"] == "stopped" and final["content"].startswith("你好，")
        assert "慢" * 40 not in final["content"]
        assert result and result[0][-1]["type"] == "end"


def test_text_attachment_is_inlined_and_image_needs_vision(make_system: Callable[..., SystemHandle],
                                                           fake: FakeModelServer) -> None:
    with chat_client(make_system) as (c, _handle, headers):
        upload = c.post("/v1/chat/attachments", headers={**headers, "content-type": "text/markdown",
                                                         "x-file-name": "notes.md"}, content="# 设定\n灯塔".encode())
        assert upload.status_code == 201, upload.text
        from io import BytesIO

        from PIL import Image

        buffer = BytesIO()
        Image.new("RGB", (8, 8), (200, 180, 120)).save(buffer, format="PNG")
        png = buffer.getvalue()
        image = c.post("/v1/chat/attachments", headers={**headers, "content-type": "image/png",
                                                        "x-file-name": "ref.png"}, content=png)
        assert image.status_code == 201, image.text
        cid = c.post("/v1/chat/conversations", headers=headers, json={}).json()["id"]
        read_events(c, f"/v1/chat/conversations/{cid}/messages", headers,
                    {"content": "看附件", "attachments": [upload.json(), image.json()]})
        sent = [r for r in fake.requests if r["path"] == "/api/chat"][-1]["body"]["messages"][-1]
        assert "附件《notes.md》" in sent["content"] and "灯塔" in sent["content"] and "images" not in sent
        cid2 = c.post("/v1/chat/conversations", headers=headers, json={"model": "lab/vision:7b"}).json()["id"]
        read_events(c, f"/v1/chat/conversations/{cid2}/messages", headers,
                    {"content": "看图", "attachments": [image.json()]})
        sent = [r for r in fake.requests if r["path"] == "/api/chat"][-1]["body"]["messages"][-1]
        assert len(sent["images"]) == 1
        bad = c.post("/v1/chat/attachments", headers={**headers, "content-type": "application/zip",
                                                      "x-file-name": "x.zip"}, content=b"PK\x03\x04")
        assert bad.status_code == 422
        broken = c.post("/v1/chat/attachments", headers={**headers, "content-type": "image/png",
                                                         "x-file-name": "broken.png"}, content=png[:40] + b"x" * 20)
        assert broken.status_code == 422


def test_generation_on_shared_card_unloads_chat_models_first(make_system: Callable[..., SystemHandle],
                                                              fake: FakeModelServer) -> None:
    with chat_client(make_system) as (c, handle, _headers):
        portal = c.portal
        assert portal is not None
        assert fake.loaded == {"reasoner:30b"}

        async def generate() -> bool:
            async with handle.accelerator.hold("generation", "第 1 镜画面"):
                return not fake.loaded

        assert portal.call(generate) is True


def test_cloud_endpoint_stays_listed_but_unused_under_local_egress(
        make_system: Callable[..., SystemHandle], fake: FakeModelServer) -> None:
    cloud = [*ENDPOINTS[:1], {"id": "remote", "label": "云端服务", "kind": "openai",
                              "base_url": "https://api.example.invalid/v1", "api_key": "secret-key"}]
    with chat_client(make_system, endpoints=cloud) as (c, _handle, headers):
        body = c.get("/v1/chat/models", headers=headers).json()
        remote = next(e for e in body["endpoints"] if e["id"] == "remote")
        assert remote["blocked"] and not remote["online"] and remote["egress"] == "cloud"
        assert not any(m["endpoint"] == "remote" for m in body["models"])
        cid = c.post("/v1/chat/conversations", headers=headers, json={"model": "remote/any"}).json()["id"]
        refused = c.post(f"/v1/chat/conversations/{cid}/messages", headers=headers, json={"content": "hi"})
        assert refused.status_code in (401, 403) and "出口策略" in refused.text
        assert not any(r["path"] == "/v1/chat/completions" for r in fake.requests)


CLOUD = [*ENDPOINTS[:1], {"id": "remote", "label": "云端服务", "kind": "openai",
                          "base_url": "https://api.example.invalid/v1", "api_key": "secret-key"}]


def test_cloud_purpose_opens_cloud_endpoint_for_that_purpose_only(
        make_system: Callable[..., SystemHandle], fake: FakeModelServer) -> None:
    with chat_client(make_system, endpoints=CLOUD, chat_cloud_purposes=["studio.director"]) as (c, handle, headers):
        chat = c.get("/v1/chat/models", headers=headers).json()
        remote = next(e for e in chat["endpoints"] if e["id"] == "remote")
        assert remote["blocked"] and remote["open_for"] == ["studio.director"] and "工作室导演" in remote["error"]
        assert not any(m["endpoint"] == "remote" for m in chat["models"])
        director = c.get("/v1/chat/models?purpose=studio.director", headers=headers).json()
        remote = next(e for e in director["endpoints"] if e["id"] == "remote")
        assert not remote["blocked"] and remote["online"]
        assert "remote/gateway-model" in {m["ref"] for m in director["models"]}
        assert director["default"] == "lab/reasoner:30b"   # cloud is never the implicit choice

        cid = c.post("/v1/chat/conversations", headers=headers, json={"model": "remote/gateway-model"}).json()["id"]
        refused = c.post(f"/v1/chat/conversations/{cid}/messages", headers=headers, json={"content": "hi"})
        assert refused.status_code in (401, 403) and "工作室导演" in refused.text
        assert not any(r["path"] == "/v1/chat/completions" for r in fake.requests)

        portal = c.portal
        assert portal is not None

        async def direct() -> str:
            actor = await handle.deps.identity.authenticate({"token": headers["Authorization"][7:]})
            return await handle.chat.complete_text(actor, [{"role": "user", "content": "剧本梗概"}],
                                                   purpose="studio.director", model_ref="remote/gateway-model")

        assert portal.call(direct) == "网关回答"
        rows = handle.store._conn.execute(  # type: ignore[attr-defined]
            "SELECT detail_json FROM audit_log WHERE kind='model.chat'").fetchall()
        detail = json.loads(rows[-1]["detail_json"])
        assert detail["purpose"] == "studio.director" and detail["egress"] == "cloud" and detail["endpoint"] == "remote"
        assert "剧本梗概" not in rows[-1]["detail_json"]


def test_cloud_only_deployment_never_picks_cloud_implicitly(
        make_system: Callable[..., SystemHandle], fake: FakeModelServer) -> None:
    with chat_client(make_system, endpoints=CLOUD[1:], chat_cloud_purposes=["studio.director"],
                     chat_default_model="") as (c, handle, headers):
        director = c.get("/v1/chat/models?purpose=studio.director", headers=headers).json()
        assert director["default"] == "" and director["models"]
        portal = c.portal
        assert portal is not None

        async def implicit() -> str:
            actor = await handle.deps.identity.authenticate({"token": headers["Authorization"][7:]})
            return await handle.chat.complete_text(actor, [{"role": "user", "content": "x"}],
                                                   purpose="studio.director")

        with pytest.raises(AgentOSError) as caught:
            portal.call(implicit)
        assert caught.value.code == ErrorCode.UNAVAILABLE
        assert not any(r["path"] == "/v1/chat/completions" for r in fake.requests)


def test_endpoint_model_allowlist_limits_listing_and_calls(
        make_system: Callable[..., SystemHandle], fake: FakeModelServer) -> None:
    gateway = [ENDPOINTS[0], {**ENDPOINTS[1], "models": ["local-route"]}]
    with chat_client(make_system, endpoints=gateway) as (c, _handle, headers):
        body = c.get("/v1/chat/models", headers=headers).json()
        assert not any(m["endpoint"] == "gw" for m in body["models"])
        cid = c.post("/v1/chat/conversations", headers=headers, json={"model": "gw/gateway-model"}).json()["id"]
        refused = c.post(f"/v1/chat/conversations/{cid}/messages", headers=headers, json={"content": "hi"})
        assert refused.status_code in (401, 403) and "不提供模型" in refused.text
        assert not any(r["path"] == "/v1/chat/completions" for r in fake.requests)
    with pytest.raises(ValueError):
        ChatEndpoint.from_dict({**ENDPOINTS[1], "models": "local-route"})


def test_generations_are_audited_without_content(make_system: Callable[..., SystemHandle],
                                                 fake: FakeModelServer) -> None:
    with chat_client(make_system) as (c, handle, headers):
        cid = c.post("/v1/chat/conversations", headers=headers, json={}).json()["id"]
        read_events(c, f"/v1/chat/conversations/{cid}/messages", headers, {"content": "保密的提问内容"})
        rows = handle.store._conn.execute(  # type: ignore[attr-defined]
            "SELECT detail_json FROM audit_log WHERE kind='model.chat'").fetchall()
        assert len(rows) == 1
        detail = json.loads(rows[0]["detail_json"])
        assert detail["endpoint"] == "lab" and detail["model"] == "reasoner:30b" and detail["egress"] == "local"
        assert detail["status"] == "complete" and detail["output_tokens"] == 30 and detail["purpose"] == "chat"
        assert "保密" not in rows[0]["detail_json"]


def test_helpers() -> None:
    wanted = "写一个关于灯塔看守的短剧开场，要有反转和钩子"
    assert title_from("## " + wanted) == wanted
    assert title_from("x" * 40).endswith("…")
    assert estimate_tokens("你好世界") < estimate_tokens("你好世界" * 10)
    assert parse_json_object('<think>…</think>```json\n{"a": 1}\n```') == {"a": 1}
    splitter = _InlineThinkSplitter()
    parts = splitter.feed("前<thi") + splitter.feed("nk>想</th") + splitter.feed("ink>后") + splitter.flush()
    assert [p for p in parts if p[1]] == [("content", "前"), ("thinking", "想"), ("content", "后")]
