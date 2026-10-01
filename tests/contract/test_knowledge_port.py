"""U4-06 KnowledgePort 合同：同一 fixture 驱动，内置后端全链路验收。

判据来自 tests/fixtures/u4/k_knowledge.json（K01～K06、K08、K09）。
K07（WeKnora 同 fixture）归 U4-07；本文件的共享 fixture 设计成可直接换后端。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from agentos.apps.composition import SystemHandle
from agentos.kernel.errors import AgentOSError
from agentos.kernel.types import Ref
from tests.conftest import WRITE_FLOW_SCRIPT, owner_token

FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "u4" / "k_knowledge.json").read_text())


def admin(handle: SystemHandle):
    return asyncio.run(handle.deps.identity.authenticate({"token": owner_token(handle)}))


def second_subject(handle: SystemHandle):
    import secrets

    path = handle.settings.identity_file
    data = json.loads(path.read_text())
    data["subjects"]["knw-peer"] = {
        "token": secrets.token_urlsafe(16),
        "scopes": ["read"],
        "tenant_id": "default",
    }
    path.write_text(json.dumps(data))
    return data["subjects"]["knw-peer"]["token"]


@pytest.fixture()
def backend(make_system: Callable[..., SystemHandle]):
    """共享 fixture：与 U4-07 的 WeKnora 后端共用同一批判据。"""
    handle = make_system(model_script=WRITE_FLOW_SCRIPT)
    handle.boot()
    return handle


# ------------------------------------------------------------------ K01/K02
def test_ingest_returns_version_and_deduplicates(backend: SystemHandle) -> None:
    actor = admin(backend)
    kb = backend.knowledge
    for doc in FIXTURE["corpus"]:
        acl = [actor.subject if subject == "default" else subject for subject in doc["acl"]]
        version = asyncio.run(kb.ingest(actor, doc["source_id"], doc["text"], acl))
        assert version, "K01：摄取必须返回 source_version"
    # K02：重复文档二次摄取——内容哈希去重，嵌入调用不重复
    dup = next(d for d in FIXTURE["corpus"] if d.get("duplicate_of"))
    before = asyncio.run(kb.search(actor, "春季徒步节", 5))
    again = asyncio.run(
        kb.ingest(actor, dup["source_id"], dup["text"], [actor.subject if s == "default" else s for s in dup["acl"]])
    )
    after = asyncio.run(kb.search(actor, "春季徒步节", 5))
    assert again  # 二次摄取仍返回版本
    assert len(before) == len(after), "K02：重复文档不得产生重复命中"
    assert all(h.chunk_id for h in after)


# ------------------------------------------------------------------ K03/K04
def test_search_hits_with_resolvable_refs(backend: SystemHandle) -> None:
    actor = admin(backend)
    kb = backend.knowledge
    for doc in FIXTURE["corpus"]:
        acl = [actor.subject if subject == "default" else subject for subject in doc["acl"]]
        asyncio.run(kb.ingest(actor, doc["source_id"], doc["text"], acl))
    for query in FIXTURE["queries"]:
        if query.get("expect") in {"denied-or-empty", "needs-vector-embedder"}:
            if query.get("expect") == "needs-vector-embedder":
                with pytest.raises(AgentOSError, match="RETRIEVAL_NO_SOURCE"):
                    asyncio.run(kb.search(actor, query["query"], 5))
            continue
        hits = asyncio.run(kb.search(actor, query["query"], 5))
        assert hits, f"K03：{query['query']} 应命中"
        assert all(h.refs for h in hits), "命中必须带可回查引用"
        assert query["expect_no_source"] not in {h.refs[0].source_id for h in hits}
        ref = hits[0].refs[0]
        resolved = asyncio.run(kb.resolve_ref(actor, ref))
        assert resolved["text"], "K04：引用回查必须返回原文"
        assert resolved["backend"] == "builtin-sqlite"


# ------------------------------------------------------------------ K05：私有哨兵
def test_private_sentinel_never_returned(backend: SystemHandle) -> None:
    actor = admin(backend)
    kb = backend.knowledge
    for doc in FIXTURE["corpus"]:
        acl = [actor.subject if subject == "default" else subject for subject in doc["acl"]]
        asyncio.run(kb.ingest(actor, doc["source_id"], doc["text"], acl))
    peer = asyncio.run(backend.deps.identity.authenticate({"token": second_subject(backend)}))
    for subject in (actor, peer):
        with pytest.raises(AgentOSError) as exc:
            asyncio.run(kb.search(subject, "权限哨兵", 5))
        assert "RETRIEVAL_NO_SOURCE" in str(exc.value)
    with pytest.raises(AgentOSError):
        asyncio.run(
            kb.resolve_ref(
                actor,
                Ref(source_id="src-sentinel-private", source_version="1", locator="src-sentinel-private:0", quote=""),
            )
        )
    blob = json.dumps(FIXTURE)
    assert "PRIVATE-SENTINEL-42" in blob  # fixture 自身含有哨兵文本（对照）


# ------------------------------------------------------------------ K06：删除失效
def test_delete_tombstones_source_everywhere(backend: SystemHandle) -> None:
    actor = admin(backend)
    kb = backend.knowledge
    for doc in FIXTURE["corpus"]:
        acl = [actor.subject if subject == "default" else subject for subject in doc["acl"]]
        asyncio.run(kb.ingest(actor, doc["source_id"], doc["text"], acl))
    hits = asyncio.run(kb.search(actor, "春季徒步节", 5))
    assert hits
    target = hits[0].refs[0].source_id
    asyncio.run(kb.delete(actor, target))
    with pytest.raises(AgentOSError):
        asyncio.run(kb.search(actor, "春季徒步节", 5))
    with pytest.raises(AgentOSError):
        asyncio.run(kb.resolve_ref(actor, Ref(source_id=target, source_version="1", locator=f"{target}:0", quote="")))
    assert f"knowledge:{target}" in backend.store.list_tombstones("default"), "删除必须写 tombstone"


# ------------------------------------------------------------------ K08：引用归属固定
def test_cross_backend_reference_is_rejected(backend: SystemHandle) -> None:
    """引用里混入其他后端的语义：内置后端无法证明归属 → 拒绝，不猜。"""
    actor = admin(backend)
    kb = backend.knowledge
    for doc in FIXTURE["corpus"]:
        acl = [actor.subject if subject == "default" else subject for subject in doc["acl"]]
        asyncio.run(kb.ingest(actor, doc["source_id"], doc["text"], acl))
    forged = Ref(source_id="weknora-doc", source_version="weknora-9", locator="src-brand-zh:0", quote="")
    with pytest.raises(AgentOSError, match="source/version"):
        asyncio.run(kb.resolve_ref(actor, forged))


# ------------------------------------------------------------------ K09：游客不可实时检索
def test_guest_cannot_search_or_reach_full_corpus(backend: SystemHandle) -> None:
    import time as _t

    from fastapi.testclient import TestClient

    from agentos.apps.gateway import create_app

    handle = backend
    with TestClient(create_app(system=handle)) as c:
        token = owner_token(handle)
        created = c.post(
            "/v1/demo/grants", headers={"Authorization": f"Bearer {token}"}, json={"label": "knw", "ttl_hours": 24}
        ).json()
        gtok = c.post("/v1/auth/guest/redeem", json={"code": created["invite_code"]}).json()["access_token"]
        g = {"Authorization": f"Bearer {gtok}"}
        assert c.get("/v1/knowledge/search?q=%E5%BE%92%E6%AD%A5", headers=g).status_code in (401, 403)
        assert c.get("/v1/knowledge/capabilities", headers=g).status_code in (401, 403)
        # 发布的引用片段在任务投影内可看，但不能跳整库（无全文端点给游客）
        assert c.post("/v1/knowledge/ingest", headers=g, json={"source_id": "x", "text": "y"}).status_code in (
            401,
            403,
            422,
        )
        _ = _t  # 保持导入整洁


# ------------------------------------------------------------------ 业务调用者
def test_normal_task_calls_knowledge_through_port(
    make_system: Callable[..., SystemHandle],
) -> None:
    """普通任务里的 `knw.search` 走 KnowledgePort：后端名进结果与 trace，切换后端不改上层。"""
    import time as _time

    from fastapi.testclient import TestClient

    from agentos.apps.gateway import create_app

    script = [
        {"tool_calls": [{"id": "c1", "name": "knw.search", "arguments": {"query": "春季徒步节"}}]},
        {"content": "答复：活动定于 2026-04-12（来自知识库，附引用）。"},
    ]
    handle = make_system(model_script=script)
    handle.boot()
    actor = admin(handle)
    for doc in FIXTURE["corpus"]:
        acl = [actor.subject if subject == "default" else subject for subject in doc["acl"]]
        asyncio.run(handle.knowledge.ingest(actor, doc["source_id"], doc["text"], acl))
    with TestClient(create_app(system=handle)) as c:
        token = owner_token(handle)
        r = c.post(
            "/v1/tasks",
            headers={"Authorization": f"Bearer {token}"},
            json={"payload": {"goal": "查一下活动日期"}, "idempotency_key": "knw-task"},
        )
        rid = r.json()["run_id"]
        deadline = _time.monotonic() + 15
        state = ""
        while _time.monotonic() < deadline:
            detail = c.get(f"/v1/tasks/{rid}", headers={"Authorization": f"Bearer {token}"}).json()
            state = detail["run"]["state"]
            if state in {"succeeded", "failed"}:
                break
            _time.sleep(0.05)
        assert state == "succeeded", state
        steps = detail["steps"]
        assert any(s["tool_name"] == "knw.search" and s["state"] == "completed" for s in steps)
        # 步骤结果里带后端身份（后端可切换，证据保留）
        result_blob = json.dumps([s.get("result_json") for s in steps], ensure_ascii=False)
        assert "builtin-sqlite" in result_blob
