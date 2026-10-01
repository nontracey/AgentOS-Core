"""Real SSE protocol through mock HTTP: rotate before output only, preserve usage and deltas."""
import json

import httpx
import pytest

from agentos.adapters.model.openai_compat import OpenAICompatModelAdapter
from agentos.kernel.errors import AgentOSError, ErrorCode
from tests.contract.test_model_port import actor, base_request


async def test_sse_rate_limit_rotates_once_and_returns_real_chunks(monkeypatch):
    monkeypatch.setenv("AGENTOS_MODEL_STREAM", "1")
    keys = []

    def respond(request):
        keys.append(request.headers["Authorization"])
        if len(keys) == 1:
            return httpx.Response(429, json={"error": {"message": "rate limit"}})
        payloads = [
            {"choices": [{"delta": {"content": "第一片"}}]},
            {"choices": [{"delta": {"content": "第二片"}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 24, "completion_tokens": 6}},
        ]
        body = "".join("data: " + json.dumps(p) + "\n\n" for p in payloads) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    adapter = OpenAICompatModelAdapter(base_url="http://model.test/v1", api_key="", api_keys=["one", "two"],
                                       model="test", transport=httpx.MockTransport(respond))
    events = [event async for event in adapter.stream(base_request([]), actor())]
    assert keys == ["Bearer one", "Bearer two"]
    assert [e["delta"] for e in events if e["type"] == "text_delta"] == ["第一片", "第二片"]
    assert len([e for e in events if e["type"] == "key_rotation"]) == 1
    usage = next(e["usage"] for e in events if e["type"] == "usage")
    assert not usage.estimated and usage.input_tokens == 24 and usage.output_tokens == 6


async def test_sse_partial_answer_does_not_rotate_or_replay(monkeypatch):
    monkeypatch.setenv("AGENTOS_MODEL_STREAM", "1")
    adapter = OpenAICompatModelAdapter(base_url="http://model.test/v1", api_key="", api_keys=["one", "two"],
                                       model="test")
    calls = []

    async def fail_after_delta(body):
        calls.append(body)
        yield {"type": "text_delta", "delta": "已交付片段"}
        raise AgentOSError(ErrorCode.UNAVAILABLE, "rate limit mid-stream", detail={"http_status": 429})

    monkeypatch.setattr(adapter, "_stream_remote", fail_after_delta)
    stream = adapter.stream(base_request([]), actor())
    assert (await anext(stream))["delta"] == "已交付片段"
    with pytest.raises(AgentOSError, match="mid-stream"):
        await anext(stream)
    assert len(calls) == 1


async def test_sse_abrupt_eof_is_not_success_or_a_key_replay(monkeypatch):
    monkeypatch.setenv("AGENTOS_MODEL_STREAM", "1")
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                             text='data: {"choices":[{"delta":{"content":"半段正文"}}]}\n\n')

    adapter = OpenAICompatModelAdapter(base_url="http://model.test/v1", api_key="", api_keys=["one", "two"],
                                       model="test", transport=httpx.MockTransport(respond))
    stream = adapter.stream(base_request([]), actor())
    assert (await anext(stream))["delta"] == "半段正文"
    with pytest.raises(AgentOSError, match="completion marker"):
        await anext(stream)
    assert len(calls) == 1
