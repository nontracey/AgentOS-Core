"""确定性 Scripted Model Adapter（契约 fixture 基准）。

无网络依赖：按对话状态确定性地重放脚本（tool_calls 或最终文本），用于
单元测试、离线演示与 OpenAI-compatible adapter 的同 fixture 契约对比（FR-MDL-01）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import AuthContext, ModelRequest, ModelToolCall, Usage


class ScriptedModelAdapter:
    provider = "scripted"

    def __init__(self, script: list[dict[str, Any]], *, model_name: str = "scripted-1") -> None:
        self._script = script
        self._model = model_name

    async def capabilities(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self._model,
            "streaming": True,
            "tools": True,
            "json_mode": False,
            "vision": False,
            "max_context_tokens": 8192,
        }

    def _select_script(self, request: ModelRequest) -> tuple[list[dict[str, Any]], int]:
        """选择本轮脚本：支持按 user 内容分组的子脚本（供计划多任务复用）。

        - 列表项若含 "when_user_contains" + "script"，取最后一个匹配组，组内按
          assistant 轮次计数（每组独立）；
        - 匹配 user 内容中的 "[[task:key]]" 标记（PlanRunner 按任务 ID、模板执行器
          按目标文本包装）；否则整体顺序脚本，按全局 assistant 计数。
        """
        last_user = next(
            (m.content for m in reversed(request.messages) if m.role == "user"), ""
        )
        for entry in self._script:
            if isinstance(entry, dict) and entry.get("when_user_contains") is not None \
                    and f"[[task:{entry['when_user_contains']}]]" in last_user:
                group_script = entry.get("script") or []
                assistants = sum(
                    1 for m in request.messages if m.role == "assistant"
                )
                return group_script, assistants
        return self._script, sum(1 for m in request.messages if m.role == "assistant")

    async def stream(
        self, request: ModelRequest, actor: AuthContext
    ) -> AsyncIterator[dict[str, Any]]:
        script, turn = self._select_script(request)
        if turn >= len(script):
            raise AgentOSError(
                ErrorCode.INVALID, f"scripted provider exhausted at turn {turn}"
            )
        step = script[turn]
        input_tokens = sum(len(m.content) // 4 + 1 for m in request.messages)

        if "tool_calls" in step:
            calls = [
                ModelToolCall(
                    id=c["id"],
                    name=c["name"],
                    arguments=c.get("arguments", {}),
                )
                for c in step["tool_calls"]
            ]
            assistant_content = step.get("content", "")
            yield {
                "type": "assistant_message",
                "content": assistant_content,
                "tool_calls": calls,
            }
            output_tokens = (len(json.dumps(step)) // 4) + 1
        else:
            content = str(step.get("content", ""))
            yield {"type": "text_delta", "delta": content}
            output_tokens = (len(content) // 4) + 1

        yield {
            "type": "usage",
            "usage": Usage(input_tokens=input_tokens, output_tokens=output_tokens),
        }
        yield {"type": "done", "model": self._model}
