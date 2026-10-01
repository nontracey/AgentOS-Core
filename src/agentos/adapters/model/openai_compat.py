"""OpenAI-compatible Chat API Adapter（FR-MDL-01：M0 首个受控 provider 契约）。

- 只实现 Chat 能力族；Embedding/Rerank/视觉等按 FR-MDL-06 在后续阶段按能力族拆分，
  不把一个"兼容端点"当成全能力端点。
- 输出事件：text_delta / assistant_message / tool_call / usage / done / error。
- usage 缺失时标 estimated=True（FR-MDL-05），不混入实测成本。
- M2 扩展降级链与流式首字（FR-MDL-03、NFR-STR）；当前失败显式报错，不静默降级。
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import (
    AuthContext,
    ChatMessage,
    ModelRequest,
    ModelToolCall,
    ToolSpec,
    Usage,
)

_HTTP_TIMEOUT_S = float(os.environ.get("AGENTOS_MODEL_TIMEOUT_S", "300"))


class OpenAICompatModelAdapter:
    provider = "openai_compat"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        api_keys: list[str] | None = None,
        capabilities: dict[str, Any] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        # key 池：免费端点限流时轮换下一个 key（显式事件，不静默）；
        # 本地端点（Ollama）key 可为空串——必须保留至少一个条目，否则轮换循环 0 次直接抛错
        self._api_keys = [k for k in (api_keys or [api_key]) if k] or [api_key]
        self._key_index = 0
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._transport = transport  # 测试注入 mock transport（NFR-ADP 同 fixture 等价）
        self._caps = capabilities or {
            "streaming": True,  # AGENTOS_MODEL_STREAM=1 时走供应商真逐字流；否则单段返回（不伪造增量）
            "tools": True,
            "json_mode": False,
            "vision": False,
            "max_context_tokens": 8192,
        }

    @property
    def model_name(self) -> str:
        return self._model

    async def capabilities(self) -> dict[str, Any]:
        return {"provider": self.provider, "model": self._model, **self._caps}

    @staticmethod
    def _message_to_openai(message: ChatMessage) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.tool_call_id:
            payload["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)},
                }
                for c in message.tool_calls
            ]
        return payload

    @staticmethod
    def _tool_to_openai(spec: ToolSpec) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.input_schema,
            },
        }

    def _debug_dump(self, body: dict[str, Any], data: dict[str, Any]) -> None:
        """AGENTOS_MODEL_DEBUG=1 时把完整请求/响应落盘（排障用，默认关）。"""
        import os as _os

        if _os.environ.get("AGENTOS_MODEL_DEBUG") != "1":
            return
        path = _os.environ.get("AGENTOS_MODEL_DEBUG_FILE", "model_debug.jsonl")
        slim = {**body, "tools": [t["function"]["name"] for t in body.get("tools", [])]}
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"req": slim,
                                    "resp": {"finish": (data.get("choices") or [{}])[0].get("finish_reason"),
                                             "msg": (data.get("choices") or [{}])[0].get("message", {})}},
                                   ensure_ascii=False) + "\n")
        except OSError:
            pass

    async def _stream_remote(self, body: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """真流式：解析供应商 SSE 增量；usage 取最后 chunk；工具调用聚合成一次 assistant_message。"""
        import json as _json

        content_parts: list[str] = []
        tool_calls: dict[str, dict[str, Any]] = {}
        usage_raw: dict[str, Any] = {}
        finish = ""
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(_HTTP_TIMEOUT_S, read=_HTTP_TIMEOUT_S),
                trust_env=False, transport=self._transport
            ) as client, client.stream(
                "POST",
                f"{self._base_url}/chat/completions",
                json=body,
                headers=({"Authorization": f"Bearer {self._api_keys[self._key_index]}"}
                         if self._api_keys[self._key_index] else {}),
            ) as resp:
                if resp.status_code != 200:
                    await resp.aread()
                    raise AgentOSError(
                        ErrorCode.UNAVAILABLE,
                        f"model endpoint returned {resp.status_code}",
                        detail={"http_status": resp.status_code})
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    chunk = line[6:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        payload = _json.loads(chunk)
                    except _json.JSONDecodeError:
                        continue
                    if payload.get("error"):
                        raise AgentOSError(
                            ErrorCode.UNAVAILABLE,
                            "model endpoint returned an error frame",
                            detail={"http_status": 429 if "rate limit" in str(payload["error"]).lower() else 0})
                    if payload.get("usage"):
                        usage_raw = payload["usage"]
                    for choice in payload.get("choices") or []:
                        finish = choice.get("finish_reason") or finish
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            content_parts.append(delta["content"])
                            yield {"type": "text_delta", "delta": delta["content"]}
                        for tc in delta.get("tool_calls") or []:
                            idx = str(tc.get("index", tc.get("id", "")))
                            slot = tool_calls.setdefault(
                                idx, {"id": str(tc.get("id") or ""), "name": "",
                                      "arguments": ""})
                            fn = tc.get("function") or {}
                            slot["id"] = slot["id"] or str(tc.get("id") or "")
                            slot["name"] = slot["name"] or str(fn.get("name") or "")
                            slot["arguments"] += str(fn.get("arguments") or "")
        except httpx.TimeoutException as exc:
            raise AgentOSError(ErrorCode.TIMEOUT, f"model endpoint timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise AgentOSError(
                ErrorCode.UNAVAILABLE, f"model endpoint unreachable: {exc}") from exc

        if not finish:
            raise AgentOSError(ErrorCode.UNAVAILABLE, "model stream ended without a completion marker")
        if finish == "length":
            raise AgentOSError(ErrorCode.INVALID, "model output truncated by token limit")
        if tool_calls:
            calls: list[ModelToolCall] = []
            for slot in tool_calls.values():
                try:
                    arguments = _json.loads(slot["arguments"] or "{}")
                except _json.JSONDecodeError as exc:
                    raise AgentOSError(
                        ErrorCode.INVALID,
                        f"model produced malformed tool arguments: {exc}") from exc
                calls.append(ModelToolCall(id=slot["id"], name=slot["name"], arguments=arguments))
            yield {"type": "assistant_message",
                   "content": "".join(content_parts), "tool_calls": calls}
        elif content_parts:
            pass  # 增量已逐条 yield；此处不再重复发整段

        if usage_raw:
            completion_details = usage_raw.get("completion_tokens_details") or {}
            yield {
                "type": "usage",
                "usage": Usage(
                    input_tokens=int(usage_raw.get("prompt_tokens") or 0),
                    output_tokens=int(usage_raw.get("completion_tokens") or 0),
                    reasoning_tokens=int(completion_details.get("reasoning_tokens") or 0),
                ),
                "model": self._model,
            }
        else:
            estimated_text = "".join(content_parts) + _json.dumps(list(tool_calls.values()))
            yield {
                "type": "usage",
                "usage": Usage(
                    input_tokens=0, output_tokens=len(estimated_text) // 4 + 1, estimated=True
                ),
            }
        yield {"type": "done", "model": self._model}
        _ = finish

    async def _post_with_key_rotation(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST；限流/不可用时轮换 key 池（每 key 一次），全部失败抛 UNAVAILABLE。"""
        last_detail = ""
        for attempt in range(len(self._api_keys)):
            key = self._api_keys[(self._key_index + attempt) % len(self._api_keys)]
            try:
                async with httpx.AsyncClient(
                    timeout=_HTTP_TIMEOUT_S, trust_env=False, transport=self._transport
                ) as client:
                    # 本地端点（Ollama）允许空 key：空串时不得发送空 Authorization（httpx 拒绝空值头）
                    response = await client.post(
                        f"{self._base_url}/chat/completions",
                        json=body,
                        headers=({"Authorization": f"Bearer {key}"} if key else {}),
                    )
            except httpx.TimeoutException as exc:
                raise AgentOSError(ErrorCode.TIMEOUT, f"model endpoint timeout: {exc}") from exc
            except httpx.HTTPError as exc:
                raise AgentOSError(
                    ErrorCode.UNAVAILABLE, f"model endpoint unreachable: {exc}"
                ) from exc
            if response.status_code == 200:
                self._key_index = (self._key_index + attempt) % len(self._api_keys)
                data: dict[str, Any] = response.json()
                # 端点可能以 HTTP 200 返回错误体（部分兼容提供方限流使用此形态）：
                # 还有剩余 key 时先轮换，全部失败才显式报错（不静默）
                if data.get("error"):
                    last_detail = "200-with-error"
                    if attempt < len(self._api_keys) - 1:
                        continue
                    raise AgentOSError(
                        ErrorCode.UNAVAILABLE, f"model endpoint error: {last_detail}"
                    )
                return data
            last_detail = f"HTTP {response.status_code}"
            if response.status_code == 429 or "rate limit" in response.text.lower():
                continue  # 限流 → 轮换下一个 key
            raise AgentOSError(
                ErrorCode.UNAVAILABLE, f"model endpoint returned {response.status_code}"
            )
        raise AgentOSError(
            ErrorCode.UNAVAILABLE, f"model endpoint rate-limited on all keys: {last_detail}"
        )

    async def stream(
        self, request: ModelRequest, actor: AuthContext
    ) -> AsyncIterator[dict[str, Any]]:
        stream_remote = (
            os.environ.get("AGENTOS_MODEL_STREAM") == "1"
            and bool(self._caps.get("streaming", True))
        )
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [self._message_to_openai(m) for m in request.messages],
            "max_tokens": request.max_output_tokens,
            "stream": stream_remote,
        }
        if request.tools:
            body["tools"] = [self._tool_to_openai(t) for t in request.tools]

        # trust_env=False：端点直连（国内 apihub 不走本机代理），禁用系统代理
        if self._reasoning_effort is not None:
            body["reasoning_effort"] = self._reasoning_effort
        if stream_remote:
            # U4-04：供应商真逐字流（SSE）——只有端点真返回增量才有 delta，绝不伪造逐字动画
            for attempt in range(len(self._api_keys)):
                produced = False
                try:
                    async for event in self._stream_remote(body):
                        if event.get("type") in {"text_delta", "assistant_message"} or event.get("tool_calls"):
                            produced = True
                        yield event
                    return
                except AgentOSError as exc:
                    if produced or exc.detail.get("http_status") != 429 or attempt == len(self._api_keys) - 1:
                        raise
                    self._key_index = (self._key_index + 1) % len(self._api_keys)
                    yield {"type": "key_rotation", "reason": "rate_limited", "attempt": attempt + 1}
            return
        data = await self._post_with_key_rotation(body)
        self._debug_dump(body, data)

        # 端点可能以 HTTP 200 返回错误体（部分兼容提供方限流使用此形态）
        err = data.get("error")
        if err:
            raise AgentOSError(
                ErrorCode.UNAVAILABLE, f"model endpoint error: {str(err.get('message'))[:200]}"
            )
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message", {})
        content = message.get("content") or ""
        raw_calls = message.get("tool_calls") or []

        if raw_calls:
            calls: list[ModelToolCall] = []
            for c in raw_calls:
                fn = c.get("function", {})
                try:
                    arguments = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError as exc:
                    raise AgentOSError(
                        ErrorCode.INVALID, f"model produced malformed tool arguments: {exc}"
                    ) from exc
                calls.append(ModelToolCall(id=str(c.get("id")), name=str(fn.get("name")), arguments=arguments))
            yield {"type": "assistant_message", "content": content, "tool_calls": calls}
        else:
            yield {"type": "text_delta", "delta": content}

        usage_raw = data.get("usage") or {}
        if usage_raw:
            completion_details = usage_raw.get("completion_tokens_details") or {}
            reasoning = int(completion_details.get("reasoning_tokens") or 0)
            # usage 带模型名：任务账本/工作台必须能显示"这条用的什么模型"（禁静默降级）
            yield {
                "type": "usage",
                "usage": Usage(
                    input_tokens=int(usage_raw.get("prompt_tokens") or 0),
                    output_tokens=int(usage_raw.get("completion_tokens") or 0),
                    reasoning_tokens=reasoning,
                ),
                "model": self._model,
            }
        else:
            estimated_text = content + json.dumps(raw_calls)
            yield {
                "type": "usage",
                "usage": Usage(
                    input_tokens=0, output_tokens=len(estimated_text) // 4 + 1, estimated=True
                ),
            }
        yield {"type": "done", "model": self._model}
