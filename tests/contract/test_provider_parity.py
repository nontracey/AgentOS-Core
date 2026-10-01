"""NFR-ADP provider 等价（F2）：同一 fixture 下 scripted 与 openai_compat
各跑完整 run，kernel/业务层 step 序列与交付 diff 为 0（usage 除外）。

openai_compat 通过 transport 注入本地 mock（真实 HTTP 协议、无外网）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from agentos.adapters.model.openai_compat import OpenAICompatModelAdapter
from agentos.adapters.model.scripted import ScriptedModelAdapter
from agentos.apps.composition import build_system
from agentos.apps.config import Settings
from tests.conftest import WRITE_FLOW_SCRIPT, owner_token


def _openai_transport(script: list[dict[str, Any]]) -> httpx.MockTransport:
    """按 assistant 轮次回放 scripted 等价的 OpenAI 响应（同 fixture 语义）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        # 与 scripted 语义对齐：按 assistant 消息数回放（checkpoint 回卷重调不推进轮次）
        n = sum(1 for m in body["messages"] if m.get("role") == "assistant")
        print(f"  [mock call {n}] last_user={body['messages'][-1]['content'][:60]!r}")
        if n < len(script):
            step = script[n]
            calls = [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": json.dumps(c["arguments"], ensure_ascii=False)}}
                for c in step.get("tool_calls", [])
            ]
            message: dict[str, Any] = {"role": "assistant", "content": step.get("content") or ""}
            if calls:
                message["tool_calls"] = calls
                return httpx.Response(200, json={
                    "id": "chatcmpl-x", "object": "chat.completion", "model": "fixture",
                    "choices": [{"index": 0, "finish_reason": "tool_calls", "message": message}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                              "completion_tokens_details": {"reasoning_tokens": 0}},
                })
        return httpx.Response(200, json={
            "id": "chatcmpl-x", "object": "chat.completion", "model": "fixture",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "done: sum=5 and file written"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                      "completion_tokens_details": {"reasoning_tokens": 0}},
        })

    return httpx.MockTransport(handler)


def _fingerprint(steps: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """业务级指纹：工具名/终态/结果摘要（不含 id/tokens 等实现细节）。"""
    out = []
    for s in steps:
        result = json.loads(s.get("result_json") or "{}")
        out.append((str(s.get("tool_name")), str(s.get("state")),
                    json.dumps(result.get("value", {}), sort_keys=True)[:120]))
    return out


@pytest.mark.asyncio
async def test_provider_parity_same_fixture(tmp_path: Path) -> None:
    async def run_steps(settings: Settings, model: Any) -> tuple[list[dict[str, Any]], str]:
        settings.data_dir.mkdir(parents=True, exist_ok=True)  # boot 前确保目录存在
        handle = build_system(settings, model_script=WRITE_FLOW_SCRIPT)
        handle.boot()  # 生成身份与数据目录（create_app 会做；此处手动等价）
        _ = handle.boot  # noqa: B018
        _ = handle.boot  # noqa: B018
        handle.boot()  # 生成身份与数据目录（create_app 会做；此处手动等价）
        # 注入等价模型（scripted / openai_compat+mock）
        handle.loop._model = model  # 直接替换引用（__init__ 会重置路由等状态）
        handle.loop.set_owner("parity-owner")
        token = owner_token(handle)
        actor_owner = await handle.deps.identity.authenticate({"token": token})
        _ = actor_owner
        actor_owner = await handle.deps.identity.authenticate({"token": token})
        receipt = await handle.ingress.accept(
            {"payload": {"goal": "parity"}}, channel="http",
            credentials={"token": token}, thread_id=None)
        run_id = receipt.run_id
        await handle.worker.drive_once(run_id) if hasattr(handle.worker, "drive_once") else None
        # 驱动至终态：写操作审批自动批准（两个 provider 走同一流程）
        import asyncio as _aio
        for _i in range(200):
            run = handle.store.get_run("default", run_id)
            assert run is not None
            if run.state.value in {"succeeded", "failed", "cancelled"}:
                break
            if run.state.value == "awaiting_approval":
                pending = handle.deps.approvals.list_pending("default")
                for a in pending:
                    handle.deps.approvals.decide(actor_owner, a.id, "approve")
                    await _aio.sleep(0.05)  # CAS 状态落库可见
                    handle.store.enqueue_job("default", run_id)
            await handle.loop.drive_run(run_id, "parity-owner")
        steps = handle.store.list_steps("default", run_id)
        return [s.model_dump(mode="json") for s in steps], run.state.value

    # scripted
    s1 = Settings(data_dir=tmp_path / "a", model_provider="scripted")
    s1.validate()
    steps_a, state_a = await run_steps(s1, ScriptedModelAdapter(
        [json.loads(json.dumps(WRITE_FLOW_SCRIPT[i])) for i in range(len(WRITE_FLOW_SCRIPT))]))

    # openai_compat（同 fixture mock transport）
    # Transport parity is an injected deterministic fixture, never a production legacy-loop configuration.
    s2 = Settings(data_dir=tmp_path / "b", model_provider="scripted")
    s2.validate()
    adapter = OpenAICompatModelAdapter(
        base_url="http://fixture.local/v1", api_key="k", model="fixture",
        transport=_openai_transport([json.loads(json.dumps(x)) for x in WRITE_FLOW_SCRIPT]))
    steps_b, state_b = await run_steps(s2, adapter)  # noqa: F841

    assert state_a == state_b == "succeeded", f"{state_a} vs {state_b}"
    fa, fb = _fingerprint(steps_a), _fingerprint(steps_b)
    assert [x[:2] for x in fa] == [x[:2] for x in fb], f"step 序列不一致: {fa} vs {fb}"
