"""Official v0.8.2 HTTP contract: source ACLs, reference rebinding and uncertain writes."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
import pytest

from agentos.adapters.knowledge.weknora import WeKnoraConfig, WeKnoraKnowledgeAdapter
from agentos.kernel.errors import AgentOSError
from agentos.kernel.types import AuthContext, utc_now


def actor(subject="owner", tenant="org-a"):
    return AuthContext(subject=subject, tenant_id=tenant, scopes=["read", "write"],
                       policy_version="test", expires_at=utc_now() + timedelta(minutes=5))


@pytest.fixture
def upstream(monkeypatch):
    state = {"posts": 0, "searches": [], "foreign_hit": False, "foreign_chunk": False,
             "delete_failure": False, "lost_receipt": False, "deleted": False}

    def handle(request):
        import json

        path = request.url.path
        assert request.headers["x-api-key"] == "synthetic-test-key"
        if request.method == "POST" and path.endswith("/knowledge/manual"):
            state["posts"] += 1
            if state["lost_receipt"]:
                raise httpx.ReadTimeout("synthetic lost receipt", request=request)
            return httpx.Response(200, json={"data": {"id": "document-1"}})
        if request.method == "POST" and path.endswith("/hybrid-search"):
            state["searches"].append(json.loads(request.content))
            return httpx.Response(200, json={"data": [{"id": "chunk-1", "content": "synthetic text",
                "knowledge_base_id": "kb-1", "knowledge_id": "foreign" if state["foreign_hit"] else "document-1"}]})
        if request.method == "DELETE":
            if state["delete_failure"]:
                return httpx.Response(503, json={})
            state["deleted"] = True
            return httpx.Response(200, json={"success": True})
        if path.endswith("/by-id/chunk-1"):
            document = "foreign" if state["foreign_chunk"] else "document-1"
            return httpx.Response(200, json={"data": {"knowledge_id": document,
                                                      "content": "synthetic text"}})
        if path.endswith("/knowledge/document-1"):
            if state["deleted"]:
                return httpx.Response(404, json={})
            return httpx.Response(200, json={"data": {"knowledge_base_id": "kb-1", "parse_status": "completed"}})
        raise AssertionError("unexpected upstream route")

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handle), **kwargs))
    return state


def adapter(tmp_path):
    return WeKnoraKnowledgeAdapter(WeKnoraConfig("https://knowledge.example", "synthetic-test-key",
                                    "v0.8.2", "kb-1", mapping_file=tmp_path / "bindings.db"))


def test_prefilter_and_reference_checks_deny_other_subject_tenant_and_remote(tmp_path, upstream):
    async def run():
        port = adapter(tmp_path)
        await port.ingest(actor(), "source", "synthetic text")
        assert await port.ingest(actor(), "source", "synthetic text")
        assert upstream["posts"] == 1
        for other in (actor("unrelated"), actor(tenant="org-b")):
            with pytest.raises(AgentOSError):
                await port.search(other, "text")
        assert upstream["searches"] == []
        hits = await port.search(actor(), "text")
        assert upstream["searches"][0]["knowledge_ids"] == ["document-1"]
        ref = hits[0].refs[0]
        assert (await port.resolve_ref(actor(), ref))["text"] == "synthetic text"
        with pytest.raises(AgentOSError):
            await port.resolve_ref(actor("unrelated"), ref)
        with pytest.raises(AgentOSError):
            await port.resolve_ref(actor(), ref.model_copy(update={"source_version": "wrong"}))
        upstream["foreign_hit"] = True
        with pytest.raises(AgentOSError, match="out-of-scope"):
            await port.search(actor(), "text")
        upstream["foreign_chunk"] = True
        with pytest.raises(AgentOSError, match="another document"):
            await port.resolve_ref(actor(), ref)
    asyncio.run(run())


def test_delete_revokes_immediately_and_persists_when_remote_unavailable(tmp_path, upstream):
    async def run():
        port = adapter(tmp_path)
        await port.ingest(actor(), "source", "synthetic text")
        ref = (await port.search(actor(), "text"))[0].refs[0]
        upstream["delete_failure"] = True
        with pytest.raises(AgentOSError):
            await port.delete(actor(), "source")
        rebuilt = adapter(tmp_path)
        with pytest.raises(AgentOSError):
            await rebuilt.resolve_ref(actor(), ref)
        with pytest.raises(AgentOSError):
            await rebuilt.search(actor(), "text")
        assert (await rebuilt.reconcile(actor(), "source"))["status"] == "revoked"
    asyncio.run(run())


def test_missing_write_receipt_is_never_blindly_replayed(tmp_path, upstream):
    async def run():
        port = adapter(tmp_path)
        upstream["lost_receipt"] = True
        with pytest.raises(AgentOSError, match="reconcile"):
            await port.ingest(actor(), "source", "synthetic text")
        rebuilt = adapter(tmp_path)
        with pytest.raises(AgentOSError, match="no blind replay"):
            await rebuilt.ingest(actor(), "source", "synthetic text")
        status = await rebuilt.reconcile(actor(), "source")
        assert status["status"] == "uncertain" and upstream["posts"] == 1
    asyncio.run(run())
