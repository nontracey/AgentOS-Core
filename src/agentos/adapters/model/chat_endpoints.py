"""Model endpoints for direct chat: Ollama native API and OpenAI-compatible servers.

The chat path talks to the model server directly. It does not go through the agent
runtime, so a normal question is one HTTP stream instead of an agent loop. Endpoints
are tried in configured order; LAN addresses bypass system proxies by default. Each
endpoint declares its egress so the service can enforce the deployment's boundary.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

EndpointKind = Literal["ollama", "openai"]


@dataclass
class ChatEndpoint:
    id: str
    base_url: str
    kind: EndpointKind = "ollama"
    label: str = ""
    api_key: str = ""
    # True when this server shares one card with a generation node (see AcceleratorLease).
    gpu_shared: bool = False
    # Where prompts go: "local" (this machine or LAN) or "cloud" (a third-party service).
    egress: Literal["local", "cloud"] = "local"
    use_proxy: bool = False
    timeout_s: float = 900.0
    enabled: bool = True

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ChatEndpoint:
        kind = str(raw.get("kind") or "ollama")
        if kind not in ("ollama", "openai"):
            raise ValueError(f"unknown chat endpoint kind: {kind}")
        endpoint_id = str(raw.get("id") or "").strip()
        base_url = str(raw.get("base_url") or "").strip().rstrip("/")
        if not endpoint_id or not base_url:
            raise ValueError("chat endpoint requires id and base_url")
        if "/" in endpoint_id:
            raise ValueError("chat endpoint id cannot contain '/'")
        egress = str(raw.get("egress") or ("local" if kind == "ollama" else "cloud"))
        if egress not in ("local", "cloud"):
            raise ValueError(f"chat endpoint egress must be local or cloud: {egress}")
        return cls(
            id=endpoint_id, base_url=base_url, kind=kind,  # type: ignore[arg-type]
            label=str(raw.get("label") or endpoint_id), api_key=str(raw.get("api_key") or ""),
            gpu_shared=bool(raw.get("gpu_shared", False)),
            egress=egress,  # type: ignore[arg-type]
            use_proxy=bool(raw.get("use_proxy", False)),
            timeout_s=float(raw.get("timeout_s", 900.0)),
            enabled=bool(raw.get("enabled", True)),
        )

    def public(self) -> dict[str, Any]:
        """Endpoint description for the UI; never includes credentials."""
        host = self.base_url.split("://", 1)[-1].split("/", 1)[0]
        return {"id": self.id, "label": self.label or self.id, "kind": self.kind, "host": host,
                "gpu_shared": self.gpu_shared, "egress": self.egress, "enabled": self.enabled}


@dataclass
class ModelInfo:
    endpoint: str
    name: str
    family: str = ""
    parameter_size: str = ""
    quantization: str = ""
    size_bytes: int = 0
    context_length: int = 0
    capabilities: list[str] = field(default_factory=list)
    loaded: bool = False
    vram_bytes: int = 0

    @property
    def ref(self) -> str:
        return f"{self.endpoint}/{self.name}"

    def public(self) -> dict[str, Any]:
        return {"ref": self.ref, "endpoint": self.endpoint, "name": self.name, "family": self.family,
                "parameter_size": self.parameter_size, "quantization": self.quantization,
                "size_bytes": self.size_bytes, "context_length": self.context_length,
                "capabilities": list(self.capabilities), "loaded": self.loaded,
                "vram_bytes": self.vram_bytes}


class EndpointError(Exception):
    """A model server failed in a way the user should see (unreachable, bad model, HTTP error)."""

    def __init__(self, message: str, *, retryable: bool = False, status: int = 0) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


@dataclass
class StreamEvent:
    kind: Literal["thinking", "content", "done"]
    text: str = ""
    stats: dict[str, Any] = field(default_factory=dict)


def _client(endpoint: ChatEndpoint, timeout: float | None = None) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {endpoint.api_key}"} if endpoint.api_key else {}
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout or endpoint.timeout_s, connect=6.0),
        trust_env=endpoint.use_proxy, headers=headers)


def _openai_root(base_url: str) -> str:
    return base_url if base_url.endswith("/v1") else base_url + "/v1"


def _ollama_root(base_url: str) -> str:
    return base_url[:-3] if base_url.endswith("/v1") else base_url


async def list_models(endpoint: ChatEndpoint, *, timeout: float = 4.0) -> list[ModelInfo]:
    """Discover models with their capabilities; raises EndpointError when unreachable."""
    try:
        async with _client(endpoint, timeout) as client:
            if endpoint.kind == "ollama":
                return await _ollama_models(endpoint, client)
            response = await client.get(_openai_root(endpoint.base_url) + "/models")
            if response.status_code >= 400:
                raise EndpointError(f"模型列表请求失败（HTTP {response.status_code}）", status=response.status_code)
            data = response.json().get("data") or []
            return [ModelInfo(endpoint=endpoint.id, name=str(item.get("id")), capabilities=["completion"])
                    for item in data if isinstance(item, dict) and item.get("id")]
    except httpx.HTTPError as exc:
        raise EndpointError(f"无法连接 {endpoint.label or endpoint.id}", retryable=True) from exc


async def _ollama_models(endpoint: ChatEndpoint, client: httpx.AsyncClient) -> list[ModelInfo]:
    root = _ollama_root(endpoint.base_url)
    tags = await client.get(root + "/api/tags")
    if tags.status_code >= 400:
        raise EndpointError(f"Ollama 模型列表失败（HTTP {tags.status_code}）", status=tags.status_code)
    loaded: dict[str, int] = {}
    try:
        running = await client.get(root + "/api/ps")
        if running.status_code < 400:
            for item in running.json().get("models") or []:
                loaded[str(item.get("name") or item.get("model"))] = int(item.get("size_vram") or 0)
    except httpx.HTTPError:
        pass
    models: list[ModelInfo] = []
    for item in tags.json().get("models") or []:
        name = str(item.get("name") or item.get("model") or "")
        if not name:
            continue
        details = item.get("details") or {}
        models.append(ModelInfo(
            endpoint=endpoint.id, name=name, family=str(details.get("family") or ""),
            parameter_size=str(details.get("parameter_size") or ""),
            quantization=str(details.get("quantization_level") or ""),
            size_bytes=int(item.get("size") or 0), loaded=name in loaded, vram_bytes=loaded.get(name, 0)))

    async def describe(info: ModelInfo) -> None:
        try:
            shown = await client.post(root + "/api/show", json={"model": info.name})
        except httpx.HTTPError:
            return
        if shown.status_code >= 400:
            return
        body = shown.json()
        info.capabilities = [str(c) for c in body.get("capabilities") or ["completion"]]
        for key, value in (body.get("model_info") or {}).items():
            if str(key).endswith(".context_length") and isinstance(value, int):
                info.context_length = value
                break

    await asyncio.gather(*(describe(m) for m in models))
    for info in models:
        if not info.capabilities:
            info.capabilities = ["completion"]
    return models


async def unload_all(endpoint: ChatEndpoint) -> list[str]:
    """Ask Ollama to release every loaded model so a production job can use the GPU."""
    if endpoint.kind != "ollama":
        return []
    root = _ollama_root(endpoint.base_url)
    released: list[str] = []
    try:
        async with _client(endpoint, 30.0) as client:
            running = await client.get(root + "/api/ps")
            for item in running.json().get("models") or []:
                name = str(item.get("name") or item.get("model") or "")
                if name:
                    await client.post(root + "/api/generate", json={"model": name, "keep_alive": 0})
                    released.append(name)
    except (httpx.HTTPError, ValueError):
        return released
    return released


def _ollama_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for message in messages:
        item: dict[str, Any] = {"role": message["role"], "content": message.get("content") or ""}
        if message.get("images"):
            item["images"] = list(message["images"])
        result.append(item)
    return result


def _openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for message in messages:
        images = message.get("images") or []
        if images:
            parts: list[dict[str, Any]] = [{"type": "text", "text": message.get("content") or ""}]
            parts += [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + str(b64)}}
                      for b64 in images]
            result.append({"role": message["role"], "content": parts})
        else:
            result.append({"role": message["role"], "content": message.get("content") or ""})
    return result


async def stream_chat(
    endpoint: ChatEndpoint, model: str, messages: list[dict[str, Any]], *,
    think: bool | None = None, options: dict[str, Any] | None = None,
    keep_alive: str | int | None = None, json_schema: dict[str, Any] | None = None,
) -> AsyncIterator[StreamEvent]:
    """Stream thinking and answer text separately; the last event carries timing stats."""
    if endpoint.kind == "ollama":
        async for event in _stream_ollama(endpoint, model, messages, think=think, options=options or {},
                                          keep_alive=keep_alive, json_schema=json_schema):
            yield event
    else:
        async for event in _stream_openai(endpoint, model, messages, options=options or {},
                                          json_schema=json_schema):
            yield event


async def _stream_ollama(
    endpoint: ChatEndpoint, model: str, messages: list[dict[str, Any]], *, think: bool | None,
    options: dict[str, Any], keep_alive: str | int | None, json_schema: dict[str, Any] | None,
) -> AsyncIterator[StreamEvent]:
    body: dict[str, Any] = {"model": model, "messages": _ollama_messages(messages), "stream": True}
    clean_options = {k: v for k, v in options.items() if v is not None}
    if clean_options:
        body["options"] = clean_options
    if keep_alive is not None:
        body["keep_alive"] = keep_alive
    if json_schema is not None:
        body["format"] = json_schema
    if think is not None:
        body["think"] = think
    started = time.monotonic()
    first_token: float | None = None
    marks = _ThinkClock()
    url = _ollama_root(endpoint.base_url) + "/api/chat"
    try:
        async with _client(endpoint) as client:
            for attempt in range(2):
                async with client.stream("POST", url, json=body) as response:
                    if response.status_code >= 400:
                        detail = (await response.aread()).decode("utf-8", "replace")
                        # Models without a reasoning mode reject think=true; retry once without it.
                        if attempt == 0 and "think" in body and "think" in detail.lower():
                            body.pop("think")
                            continue
                        raise EndpointError(_ollama_error(detail, response.status_code),
                                            status=response.status_code,
                                            retryable=response.status_code >= 500)
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        chunk = json.loads(line)
                        if chunk.get("error"):
                            raise EndpointError(str(chunk["error"]))
                        message = chunk.get("message") or {}
                        thinking = str(message.get("thinking") or "")
                        content = str(message.get("content") or "")
                        if (thinking or content) and first_token is None:
                            first_token = time.monotonic()
                        marks.saw(thinking, content)
                        if thinking:
                            yield StreamEvent("thinking", thinking)
                        if content:
                            yield StreamEvent("content", content)
                        if chunk.get("done"):
                            yield StreamEvent("done", stats=marks.apply(_ollama_stats(chunk, started, first_token)))
                            return
                    yield StreamEvent("done", stats=marks.apply(_ollama_stats({}, started, first_token)))
                    return
    except httpx.HTTPError as exc:
        raise EndpointError(f"与 {endpoint.label or endpoint.id} 的连接中断", retryable=True) from exc


def _ollama_error(detail: str, status: int) -> str:
    try:
        message = str(json.loads(detail).get("error") or detail)
    except (ValueError, AttributeError):
        message = detail
    if "not found" in message.lower():
        return "模型不存在或未下载：" + message[:200]
    return f"模型服务返回错误（HTTP {status}）：{message[:300]}"


def _ollama_stats(chunk: dict[str, Any], started: float, first_token: float | None) -> dict[str, Any]:
    eval_count = int(chunk.get("eval_count") or 0)
    eval_ns = int(chunk.get("eval_duration") or 0)
    stats: dict[str, Any] = {
        "output_tokens": eval_count,
        "input_tokens": int(chunk.get("prompt_eval_count") or 0),
        "load_seconds": round(int(chunk.get("load_duration") or 0) / 1e9, 2),
        "wall_seconds": round(time.monotonic() - started, 2),
        "done_reason": str(chunk.get("done_reason") or ""),
    }
    if eval_count and eval_ns:
        stats["tokens_per_second"] = round(eval_count / (eval_ns / 1e9), 1)
    if first_token is not None:
        stats["first_token_seconds"] = round(first_token - started, 2)
    return stats


async def _stream_openai(
    endpoint: ChatEndpoint, model: str, messages: list[dict[str, Any]], *,
    options: dict[str, Any], json_schema: dict[str, Any] | None,
) -> AsyncIterator[StreamEvent]:
    body: dict[str, Any] = {"model": model, "messages": _openai_messages(messages), "stream": True,
                            "stream_options": {"include_usage": True}}
    for key in ("temperature", "top_p"):
        if options.get(key) is not None:
            body[key] = options[key]
    if options.get("num_predict"):
        body["max_tokens"] = options["num_predict"]
    if json_schema is not None:
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": "result", "schema": json_schema}}
    started = time.monotonic()
    first_token: float | None = None
    marks = _ThinkClock()
    usage: dict[str, Any] = {}
    finish = ""
    inline = _InlineThinkSplitter()
    try:
        async with _client(endpoint) as client, client.stream(
                "POST", _openai_root(endpoint.base_url) + "/chat/completions", json=body) as response:
            if response.status_code >= 400:
                detail = (await response.aread()).decode("utf-8", "replace")
                raise EndpointError(f"模型服务返回错误（HTTP {response.status_code}）：{detail[:300]}",
                                    status=response.status_code, retryable=response.status_code >= 500)
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                chunk = json.loads(payload)
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    reasoning = str(delta.get("reasoning_content") or delta.get("reasoning") or "")
                    content = str(delta.get("content") or "")
                    if (reasoning or content) and first_token is None:
                        first_token = time.monotonic()
                    if reasoning:
                        marks.saw(reasoning, "")
                        yield StreamEvent("thinking", reasoning)
                    for kind, text in inline.feed(content):
                        marks.saw(text if kind == "thinking" else "", text if kind == "content" else "")
                        yield StreamEvent(kind, text)
                    if choice.get("finish_reason"):
                        finish = str(choice["finish_reason"])
            for kind, text in inline.flush():
                yield StreamEvent(kind, text)
    except httpx.HTTPError as exc:
        raise EndpointError(f"与 {endpoint.label or endpoint.id} 的连接中断", retryable=True) from exc
    wall = time.monotonic() - started
    output_tokens = int(usage.get("completion_tokens") or 0)
    stats: dict[str, Any] = {"output_tokens": output_tokens,
                             "input_tokens": int(usage.get("prompt_tokens") or 0),
                             "wall_seconds": round(wall, 2), "done_reason": finish}
    if first_token is not None:
        stats["first_token_seconds"] = round(first_token - started, 2)
        generating = time.monotonic() - first_token
        if output_tokens and generating > 0:
            stats["tokens_per_second"] = round(output_tokens / generating, 1)
    yield StreamEvent("done", stats=marks.apply(stats))


class _ThinkClock:
    """Measure how long the model reasoned: first thinking token until the first answer token."""

    def __init__(self) -> None:
        self.thinking_from: float | None = None
        self.answer_from: float | None = None

    def saw(self, thinking: str, content: str) -> None:
        now = time.monotonic()
        if thinking and self.thinking_from is None:
            self.thinking_from = now
        if content.strip() and self.answer_from is None:
            self.answer_from = now

    def apply(self, stats: dict[str, Any]) -> dict[str, Any]:
        if self.thinking_from is not None:
            end = self.answer_from if self.answer_from is not None else time.monotonic()
            stats["thinking_seconds"] = round(max(0.0, end - self.thinking_from), 2)
        return stats


class _InlineThinkSplitter:
    """Split `<think>…</think>` blocks that some servers inline into the answer text."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self.buffer = ""
        self.inside = False

    def feed(self, text: str) -> list[tuple[Literal["thinking", "content"], str]]:
        self.buffer += text
        out: list[tuple[Literal["thinking", "content"], str]] = []
        while self.buffer:
            marker = self.CLOSE if self.inside else self.OPEN
            index = self.buffer.find(marker)
            if index >= 0:
                if index:
                    out.append(("thinking" if self.inside else "content", self.buffer[:index]))
                self.buffer = self.buffer[index + len(marker):]
                self.inside = not self.inside
                continue
            # Hold back a possible partial marker at the end of the chunk.
            keep = max((n for n in range(1, len(marker)) if self.buffer.endswith(marker[:n])), default=0)
            emit = self.buffer[:len(self.buffer) - keep] if keep else self.buffer
            if emit:
                out.append(("thinking" if self.inside else "content", emit))
            self.buffer = self.buffer[len(emit):]
            break
        return out

    def flush(self) -> list[tuple[Literal["thinking", "content"], str]]:
        if not self.buffer:
            return []
        out: list[tuple[Literal["thinking", "content"], str]] = [
            ("thinking" if self.inside else "content", self.buffer)]
        self.buffer = ""
        return out


async def complete(
    endpoint: ChatEndpoint, model: str, messages: list[dict[str, Any]], *,
    options: dict[str, Any] | None = None, json_schema: dict[str, Any] | None = None,
    think: bool | None = False, keep_alive: str | int | None = None,
) -> tuple[str, dict[str, Any]]:
    """Collect a full answer (used by the production director); thinking text is dropped."""
    parts: list[str] = []
    stats: dict[str, Any] = {}
    async for event in stream_chat(endpoint, model, messages, think=think, options=options,
                                   keep_alive=keep_alive, json_schema=json_schema):
        if event.kind == "content":
            parts.append(event.text)
        elif event.kind == "done":
            stats = event.stats
    return "".join(parts), stats
