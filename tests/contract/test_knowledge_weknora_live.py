"""U4-07 K07 联验：同一 K fixture 在真实 WeKnora 后端跑 ingest/search/resolve/delete。

仅在显式提供外部服务时运行（缺环境变量即 skip，不假通过）：

    AGENTOS_KNOWLEDGE_BACKEND=weknora \
    AGENTOS_WEKNORA_URL=http://weknora.example:8080 \
    AGENTOS_WEKNORA_VERSION=v0.8.2 \
    AGENTOS_WEKNORA_TOKEN=<空间级 API Key> \
    AGENTOS_WEKNORA_KB_ID=<kb id> \
    uv run pytest tests/contract/test_knowledge_weknora_live.py -q

判据与 test_knowledge_port.py 相同（K fixture），外加：切换期间 kernel/runtime/业务
调用者零改动（同一套测试代码，只换组合根配置）。
"""

from __future__ import annotations

import asyncio
import os

import pytest

from agentos.apps.composition import SystemHandle
from agentos.kernel.errors import AgentOSError, ErrorCode
from tests.conftest import WRITE_FLOW_SCRIPT, make_settings, owner_token

FIXTURE = __import__("json").loads(
    (__import__("pathlib").Path(__file__).resolve().parents[1] / "fixtures" / "u4" / "k_knowledge.json")
    .read_text()
)

WEKNORA_URL = os.environ.get("AGENTOS_WEKNORA_URL", "")
WEKNORA_TOKEN = os.environ.get("AGENTOS_WEKNORA_TOKEN", "")
WEKNORA_KB = os.environ.get("AGENTOS_WEKNORA_KB_ID", "")
WEKNORA_VERSION = os.environ.get("AGENTOS_WEKNORA_VERSION", "v0.8.2")

pytestmark = pytest.mark.skipif(
    not (WEKNORA_URL and WEKNORA_KB),
    reason="真实 WeKnora 未配置（AGENTOS_WEKNORA_URL/KB_ID 缺失）——保持 blocked，不假通过",
)


@pytest.fixture()
def weknora_backend(tmp_path: pytest.TempPathFactory) -> SystemHandle:
    data_dir = tmp_path / "weknora-data"  # type: ignore[operator]
    settings = make_settings(
        data_dir, model_script=WRITE_FLOW_SCRIPT,
        knowledge_backend="weknora",
        knowledge_weknora_url=WEKNORA_URL,
        knowledge_weknora_token=WEKNORA_TOKEN,
        knowledge_weknora_version=WEKNORA_VERSION,
        knowledge_weknora_kb_id=WEKNORA_KB,
        knowledge_weknora_auth_kind=os.environ.get("AGENTOS_WEKNORA_AUTH_KIND", "api_key"),
    )
    from agentos.apps.composition import build_system

    handle = build_system(settings, model_script=WRITE_FLOW_SCRIPT)
    handle.boot()
    return handle


def _admin(handle: SystemHandle):
    return asyncio.run(handle.deps.identity.authenticate({"token": owner_token(handle)}))



async def _ingest_ready(kb, actor, source_id, text):
    try:
        return await kb.ingest(actor, source_id, text)
    except AgentOSError as exc:
        if exc.code != ErrorCode.UNCERTAIN:
            raise
    # Official ingestion is async. Poll the saved receipt, never replay the POST.
    for _ in range(300):
        status = await kb.reconcile(actor, source_id)
        if status["status"] == "active":
            return status["source_version"]
        if status["status"] != "pending" or status.get("parse_status") in ("failed", "error"):
            raise AgentOSError(ErrorCode.UNCERTAIN, "live ingestion needs operator reconciliation")
        await asyncio.sleep(2)
    raise AgentOSError(ErrorCode.UNCERTAIN, "live ingestion did not complete within bound")

def test_k07_weknora_ingest_search_resolve_delete(weknora_backend: SystemHandle) -> None:
    handle = weknora_backend
    kb = handle.knowledge
    assert kb.backend_id == "weknora", "组合根必须真实切到 WeKnora"
    actor = _admin(handle)

    # 摄取 K fixture 正式语料（中文 + 英文）
    for doc in FIXTURE["corpus"]:
        if doc["source_id"] == "src-sentinel-private":
            continue
        version = asyncio.run(_ingest_ready(kb, actor, doc["source_id"], doc["text"]))
        assert version, "K07：摄取必须返回 source_version"

    sentinel = next(d for d in FIXTURE["corpus"] if d["source_id"] == "src-sentinel-private")
    other = actor.model_copy(update={"subject": "synthetic-private-owner"})
    asyncio.run(_ingest_ready(kb, other, sentinel["source_id"], sentinel["text"]))

    # 检索（中文长词 + 英文）
    zh = asyncio.run(kb.search(actor, "春季徒步节", 5))
    en = asyncio.run(kb.search(actor, "spring hiking", 5))
    assert zh and en, "K07：两种语言检索均应命中"
    assert all(h.refs for h in zh + en), "命中必须带引用"

    # 引用回查
    resolved = asyncio.run(kb.resolve_ref(actor, zh[0].refs[0]))
    assert resolved.get("text"), "K07：resolve 必须回查到原文"

    # 删除失效
    target = zh[0].refs[0].source_id
    asyncio.run(kb.delete(actor, target))
    with pytest.raises(AgentOSError):
        asyncio.run(kb.resolve_ref(actor, zh[0].refs[0]))
    try:
        remaining = asyncio.run(kb.search(actor, "春季徒步节", 5))
        assert all(h.refs[0].source_id != target for h in remaining)
    except AgentOSError:
        pass

    # 私有哨兵不得返回
    try:
        other_hits = asyncio.run(kb.search(actor, "权限哨兵", 5))
        assert all("权限哨兵" not in h.text and h.refs[0].source_id != "src-sentinel-private" for h in other_hits)
    except AgentOSError:
        pass
    asyncio.run(kb.delete(other, sentinel["source_id"]))
