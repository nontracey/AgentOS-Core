"""U6 actual management, pinned execution, and negative contracts (synthetic backends)."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

import pytest

from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import AuthContext, ModelRequest, utc_now
from agentos.services.model_router import ModelRouter, ProviderSpec
from tests.integration.test_hub_admin import auth, client, submit, wait_state

SKILL = "---\nname: concise\nversion: 1\ndescription: 输出简洁\n---\n必须保留真实来源。"


def test_skill_web_install_immutable_version_pinned_call_and_disable(make_system):
    script = [
        {"tool_calls": [{"id": "s1", "name": "skill.read", "arguments": {"skill_id": "concise"}}]},
        {"content": "已读取固定技能并完成"},
    ]
    with client(make_system, script=script) as (c, handle, token):
        result = c.post("/v1/skills", headers=auth(token), json={"content_text": SKILL})
        assert result.status_code == 201
        assert c.post("/v1/skills", headers=auth(token), json={"content_text": SKILL}).status_code == 201
        assert c.post("/v1/skills", headers=auth(token),
                      json={"content_text": SKILL + "篡改正文"}).status_code == 409
        response = c.post("/v1/tasks", headers=auth(token), json={
            "payload": {"goal": "使用 concise 技能", "skill_ids": ["concise"]}, "idempotency_key": "skill"})
        rid = response.json()["run_id"]
        assert wait_state(c, token, rid, {"succeeded", "failed"}) == "succeeded"
        events = handle.store.event_list("default", rid, limit=1000)
        loaded = next(e["payload"] for e in events if e["kind"] == "skill.loaded")
        assert loaded["id"] == "concise" and loaded["version"] == "1" and loaded["digest"]
        c.post("/v1/skills/concise/enabled", headers=auth(token), json={"version": "1", "enabled": False})
        assert c.get("/v1/skills", headers=auth(token)).json() == []
        assert c.get("/v1/skills?include_disabled=true", headers=auth(token)).json()[0]["enabled"] is False
        denied = c.post("/v1/tasks", headers=auth(token), json={
            "payload": {"goal": "新任务", "skill_ids": ["concise"]}})
        assert denied.status_code in {401, 403}


def test_backend_switch_pins_old_runs_replay_and_survives_restart(make_system):
    with client(make_system, script=[{"content": "完成"}]) as (c, handle, token):
        from agentos.adapters.knowledge.builtin import BuiltinKnowledgeAdapter

        class SecondBackend(BuiltinKnowledgeAdapter):
            backend_id = "fixture-only"
            backend_version = "test-2"

        original = handle.knowledge.current()
        second = SecondBackend(original.sync_service)
        handle.knowledge._backends[second.backend_id] = second
        rid = submit(c, token, "切换前任务", "before-switch")
        changed = c.post("/v1/knowledge/backend", headers=auth(token), json={"backend": "fixture-only"})
        assert changed.status_code == 200
        actor = asyncio.run(handle.deps.identity.authenticate({"token": token}))
        assert handle.knowledge.for_run(actor, rid) is original
        again = c.post("/v1/tasks", headers=auth(token), json={
            "payload": {"goal": "切换前任务"}, "idempotency_key": "before-switch"})
        assert again.json()["run_id"] == rid and again.json()["replayed"]
        after = submit(c, token, "切换后任务", "after-switch")
        payload = json.loads(handle.store.get_run("default", after).envelope_json)["payload"]
        assert payload["knowledge_binding"] == {"backend": "fixture-only", "version": "test-2"}
        from agentos.services.knowledge_backends import KnowledgeBackends

        restored = KnowledgeBackends(handle.store, handle.knowledge._backends, "builtin-sqlite",
                                     handle.settings.data_dir / "knowledge-selection.json")
        assert restored.backend_id == "fixture-only"
        assert c.post("/v1/knowledge/backend", headers=auth(token),
                      json={"backend": "not-configured"}).status_code == 422
        assert handle.knowledge.backend_id == "fixture-only"


def test_reference_rejects_forged_quote_and_source_version(make_system):
    with client(make_system, script=[{"content": "完成"}]) as (c, handle, token):
        c.post("/v1/knowledge/ingest", headers=auth(token), json={"source_id": "report", "text": "合成活动真实来源"})
        hit = c.get("/v1/knowledge/search?q=合成活动", headers=auth(token)).json()["hits"][0]
        ref = hit["refs"][0]
        assert c.post("/v1/knowledge/resolve", headers=auth(token), json=ref).status_code == 200
        for wrong in ({"source_id": "other"}, {"source_version": "weknora-1"}, {"quote": "伪造金额"}):
            result = c.post("/v1/knowledge/resolve", headers=auth(token), json={**ref, **wrong})
            assert result.status_code in {401, 403, 422}


class DeltaModel:
    provider = "scripted"

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = 0

    async def capabilities(self) -> dict[str, Any]:
        return {"streaming": True}

    async def stream(self, request, actor):
        self.calls += 1
        yield {"type": "text_delta", "delta": "真实第一片"}
        if self.fail:
            raise AgentOSError(ErrorCode.UNAVAILABLE, "connection lost after output")
        yield {"type": "text_delta", "delta": "第二片"}


def test_router_yields_before_completion_and_does_not_mix_partial_fallback():
    async def exercise():
        primary, fallback = DeltaModel(fail=True), DeltaModel()
        router = ModelRouter([ProviderSpec("scripted-primary", primary),
                              ProviderSpec("scripted-fallback", fallback)], "scripted-primary")
        actor = AuthContext(subject="owner", tenant_id="default", scopes=["read"], policy_version="test",
                            expires_at=utc_now() + timedelta(hours=1))
        stream = router.stream_events(ModelRequest(messages=[], tools=[], max_output_tokens=64), actor,
                                      data_class="local_only")
        assert (await anext(stream))["delta"] == "真实第一片"
        with pytest.raises(AgentOSError, match="connection lost"):
            await anext(stream)
        assert primary.calls == 1 and fallback.calls == 0

    asyncio.run(exercise())


def test_knowledge_same_source_cannot_change_content_owner_or_acl(make_system):
    with client(make_system, script=[]) as (c, handle, token):
        actor = asyncio.run(handle.deps.identity.authenticate({"token": token}))
        service = handle.knowledge.current().sync_service
        service.ingest(actor, "immutable", "合成内容原版", [actor.subject])
        for text, acl in [("合成内容修改版", [actor.subject]), ("合成内容原版", ["peer"])]:
            with pytest.raises(AgentOSError) as error:
                service.ingest(actor, "immutable", text, acl)
            assert error.value.code == ErrorCode.CONFLICT
        peer = actor.model_copy(update={"subject": "peer"})
        with pytest.raises(AgentOSError) as error:
            service.ingest(peer, "immutable", "合成内容原版", ["peer"])
        assert error.value.code == ErrorCode.DENIED
        # Batch dedup must not erase the same text when the readership differs.
        service.ingest_many(peer, [("peer-source", "合成内容原版")], acl=["peer"])
        assert service.search(peer, "合成内容原版", 5)
