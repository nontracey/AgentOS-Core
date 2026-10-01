"""M2 模型治理与背压测试（FR-MDL-03/04/07、NFR-THR）。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta

import pytest

from agentos.adapters.model.scripted import ScriptedModelAdapter
from agentos.apps.composition import SystemHandle
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import AuthContext, ModelRequest, utc_now
from agentos.services.model_router import ModelRouter, ProviderSpec
from tests.conftest import owner_token


class _FailingModel(ScriptedModelAdapter):
    """总是 UNAVAILABLE 的主 provider（验证降级链）。"""

    async def stream(self, request: ModelRequest, actor: AuthContext):  # type: ignore[override]
        raise AgentOSError(ErrorCode.UNAVAILABLE, "primary provider down")
        yield  # pragma: no cover


def _router() -> ModelRouter:
    good = ScriptedModelAdapter([{"content": "fallback answer"}])
    return ModelRouter(
        [
            ProviderSpec(name="primary-broken", adapter=_FailingModel([{"content": "x"}])),
            ProviderSpec(name="scripted-fallback", adapter=good),
        ],
        primary="primary-broken",
    )


def test_fallback_chain_serves_and_records() -> None:
    router = _router()

    async def main() -> tuple[str, list[dict]]:
        fired: list[tuple[str, str]] = []

        async def on_fallback(frm: str, to: str, reason: str) -> None:
            fired.append((frm, to))

        name, events = await router.stream_with_fallback(
            ModelRequest(messages=[], max_output_tokens=64),
            AuthContext(subject="o", tenant_id="t", scopes=["read"],
                        policy_version="p", expires_at=utc_now() + timedelta(minutes=5)),
            data_class="approved_private",
            on_fallback=on_fallback,
        )
        return name, fired

    name, fired = asyncio.run(main())
    assert name == "scripted-fallback"
    assert fired == [("primary-broken", "scripted-fallback")]


def test_local_only_blocks_cloud_provider() -> None:
    """FR-MDL-07：local_only 数据在仅有云端 provider 时明确阻塞（fail-closed）。"""
    router = ModelRouter(
        [ProviderSpec(name="primary-broken", adapter=_FailingModel([{"content": "x"}])),
         ProviderSpec(name="cloud-backup", adapter=ScriptedModelAdapter([{"content": "x"}]))],
        primary="primary-broken",
    )
    with pytest.raises(AgentOSError) as exc_info:
        asyncio.run(router.stream_with_fallback(
            ModelRequest(messages=[], max_output_tokens=64),
            AuthContext(subject="o", tenant_id="t", scopes=["read"],
                        policy_version="p", expires_at=utc_now() + timedelta(minutes=5)),
            data_class="local_only",
        ))
    assert exc_info.value.code == ErrorCode.DENIED
    assert "no compliant provider" in exc_info.value.message


def test_capability_negotiation_strips_tools() -> None:
    """FR-MDL-04：无 tools 能力的 provider，编排层自动不传工具定义。"""
    caps_limited = ScriptedModelAdapter([{"content": "text only"}])

    class _NoTools(ScriptedModelAdapter):
        async def capabilities(self) -> dict:
            caps = await super().capabilities()
            return {**caps, "tools": False}

    router = ModelRouter(
        [ProviderSpec(name="limited", adapter=_NoTools([{"content": "text only"}]))],
        primary="limited",
    )
    request = ModelRequest(
        messages=[],
        tools=[__import__("agentos.kernel.types", fromlist=["ToolSpec"]).ToolSpec(
            name="t", version="1", input_schema={"type": "object"}, timeout_ms=1)],
        max_output_tokens=64,
    )
    negotiated = router._negotiate(request, {"tools": False})
    assert negotiated.tools == []
    _ = caps_limited


def test_unknown_data_class_rejected() -> None:
    router = _router()
    with pytest.raises(AgentOSError) as exc_info:
        router.providers_for("public_internet")
    assert exc_info.value.code == ErrorCode.INVALID


def test_queue_full_rejects_with_retry_hint(
    make_system: Callable[..., SystemHandle], monkeypatch: pytest.MonkeyPatch
) -> None:
    """NFR-THR：队列有界，超限明确拒绝（UNAVAILABLE + 重试提示），不吞任务。"""
    system = make_system(model_script=[{"content": "ok"}])
    system.boot()
    token = owner_token(system)
    monkeypatch.setattr(system.ingress, "_queue_limit", 0)  # 0 容量：立即拒绝
    with pytest.raises(AgentOSError) as exc_info:
        asyncio.run(system.ingress.accept(
            {"payload": {"goal": "x"}}, channel="cli", credentials={"token": token}
        ))
    assert exc_info.value.code == ErrorCode.UNAVAILABLE
    assert "queue full" in exc_info.value.message
    kinds = [e.kind for e in system.deps.audit.query("default")]
    assert "ingress.queue_full" in kinds


def test_fallback_recorded_in_run_events(
    make_system: Callable[..., SystemHandle], monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-MDL-03 验收：可重试故障注入下自动降级并落事件。"""
    system = make_system(model_script=[{"content": "ok"}])
    system.boot()
    # 把 loop 的 model 换成必失败主 + scripted 备的 router
    router = ModelRouter(
        [
            ProviderSpec(name="primary-broken", adapter=_FailingModel([{"content": "x"}])),
            ProviderSpec(name="scripted", adapter=system.loop._model),
        ],
        primary="primary-broken",
    )
    system.loop.use_router(router)
    token = owner_token(system)

    async def main() -> str:
        receipt = await system.ingress.accept(
            {"payload": {"goal": "with fallback"}}, channel="cli", credentials={"token": token}
        )
        return await system.worker.drive_until_settled(receipt.run_id)

    state = asyncio.run(main())
    assert state == "succeeded"
    events = system.store.event_list("default", _last_run_id(system), limit=200)
    kinds = [e["kind"] for e in events]
    assert "model.fallback" in kinds and "model.provider" in kinds


def _last_run_id(system: SystemHandle) -> str:
    runs = system.store.list_runs("default")
    return runs[0].id
