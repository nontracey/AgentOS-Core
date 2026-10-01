"""M3 测试：知识检索（FR-KNW-01/03/07）、记忆（FR-MEM-01/03/04）、规则决策（FR-DEC-01/06/07）。"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from agentos.adapters.store.sqlite import SqliteStore
from agentos.kernel.decision import ChoiceQuestion, NoulQuestion, ScoreQuestion
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import AuthContext, Ref, utc_now
from agentos.services.knowledge import KnowledgeService, MemoryService, RuleDecisionAdapter


def actor(subject: str = "owner") -> AuthContext:
    return AuthContext(
        subject=subject, tenant_id="default", scopes=["read", "write"],
        policy_version="t", expires_at=utc_now() + timedelta(minutes=10),
    )


@pytest.fixture
def store(tmp_path: Path) -> SqliteStore:
    return SqliteStore(tmp_path / "m3.db")


@pytest.fixture
def knowledge(store: SqliteStore) -> KnowledgeService:
    return KnowledgeService(store)


@pytest.fixture
def memory(store: SqliteStore) -> MemoryService:
    return MemoryService(store)


# ------------------------------------------------------------- FR-KNW
def test_ingest_and_search_with_cjk(knowledge: KnowledgeService) -> None:
    knowledge.ingest(actor(), "doc1", "AgentOS 是可插拔的 AI 基础设施。\n\n它支持 FTS5 检索与 ACL 过滤。")
    hits = knowledge.search(actor(), "可插拔")
    assert len(hits) >= 1
    assert "ref" in hits[0] and hits[0]["ref"]["source_id"] == "doc1"


def test_zero_hit_returns_retrieval_no_source(knowledge: KnowledgeService) -> None:
    knowledge.ingest(actor(), "doc1", "关于部署的文档。")
    with pytest.raises(AgentOSError) as exc_info:
        knowledge.search(actor(), "完全不存在的词汇xyzq")
    assert "RETRIEVAL_NO_SOURCE" in exc_info.value.message
    assert exc_info.value.detail.get("code") == "RETRIEVAL_NO_SOURCE"


def test_acl_isolation_between_subjects(knowledge: KnowledgeService) -> None:
    knowledge.ingest(actor(subject="owner"), "private-doc", "机密内容：薪酬表。", acl=["owner"])
    with pytest.raises(AgentOSError):
        knowledge.search(actor(subject="helper"), "薪酬表")
    # owner 可检索
    assert len(knowledge.search(actor(subject="owner"), "薪酬表")) == 1


def test_resolve_ref_reauthorizes(knowledge: KnowledgeService) -> None:
    hits = knowledge.ingest(actor(), "d1", "引用回查目标内容。")
    _ = hits
    ref = Ref(source_id="d1", source_version="1", locator="d1:0", quote="引用回查目标内容")
    assert "引用回查" in knowledge.resolve_ref(actor(), ref)["text"]
    with pytest.raises(AgentOSError) as exc_info:
        knowledge.resolve_ref(actor(subject="helper"), ref)
    assert exc_info.value.code == ErrorCode.DENIED


def test_dedup_no_double_index(knowledge: KnowledgeService) -> None:
    r1 = knowledge.ingest(actor(), "d1", "相同内容去重测试段落。")
    r2 = knowledge.ingest(actor(), "d1", "相同内容去重测试段落。")
    assert r1["new_chunks"] == 1
    assert r2["new_chunks"] == 0  # 重复摄取零新增（FR-KNW-03）


def test_delete_source_removes_hits(knowledge: KnowledgeService) -> None:
    knowledge.ingest(actor(), "d1", "将被删除的文档内容。")
    knowledge.delete_source(actor(), "d1")
    with pytest.raises(AgentOSError):
        knowledge.search(actor(), "将被删除")


# ------------------------------------------------------------- FR-MEM
def test_memory_persists_across_sessions(memory: MemoryService, store: SqliteStore) -> None:
    a = actor()
    entry_id = memory.put(a, "prefs", "用户偏好简洁回复", source="user-stated")
    # 新会话 = 新 service 实例（同一存储）
    memory2 = MemoryService(store)
    entries = memory2.query(a, scope="prefs")
    assert len(entries) == 1
    assert entries[0]["id"] == entry_id
    assert entries[0]["source"] == "user-stated"  # 来源可追溯


def test_memory_owner_isolation(memory: MemoryService) -> None:
    memory.put(actor(subject="owner"), "scope1", "owner 的记忆")
    assert memory.query(actor(subject="helper"), scope="scope1") == []


def test_forget_removes_and_tombstones(memory: MemoryService, store: SqliteStore) -> None:
    entry_id = memory.put(actor(), "s", "将被遗忘的内容")
    memory.forget(actor(), entry_id)
    assert memory.query(actor(), scope="s") == []
    conn = store._conn
    tomb = conn.execute(
        "SELECT resource_id FROM deletion_tombstones WHERE resource_id=?", (entry_id,)
    ).fetchone()
    assert tomb is not None
    # 审计事件不含原文
    events = store.audit_query("default", kind_prefix="memory.forgotten")
    assert all("将被遗忘" not in json_dumps(e.detail) for e in events)


def json_dumps(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def test_backup_restore_replays_tombstones(memory: MemoryService) -> None:
    """FR-MEM-04：恢复前重放 tombstone，已遗忘记忆不可复活。"""
    keep = memory.put(actor(), "s", "保留的记忆")
    gone = memory.put(actor(), "s", "已遗忘的记忆")
    backup = memory.private_backup(actor())
    memory.forget(actor(), gone)
    restored = memory.restore_from_backup(actor(), backup)
    assert restored == 1  # tombstone 命中，被遗忘的不复活
    texts = [e["content"] for e in memory.query(actor(), scope="s")]
    assert "保留的记忆" in texts and "已遗忘的记忆" not in texts
    _ = keep


# ------------------------------------------------------------- FR-DEC
def test_rule_adapter_answers_and_abstains() -> None:
    adapter = RuleDecisionAdapter(rules={"q1": {"answer": "yes"}, "q2": {"answer": "b"}})
    results = asyncio_run(adapter.decide({}, [
        NoulQuestion(id="q1", instructions="i"),
        ChoiceQuestion(id="q2", instructions="i", criteria={"a": "A", "b": "B"}),
        ScoreQuestion(id="q3", instructions="i", criteria=["low", "high"]),
        NoulQuestion(id="unknown", instructions="i"),
    ]))
    by_id = {r.question_id: r for r in results}
    assert by_id["q1"].value == 1.0 and by_id["q1"].status == "answered"
    assert by_id["q2"].value == "b" and by_id["q2"].probabilities == {"a": 0.0, "b": 1.0}
    assert by_id["q3"].status == "abstained"  # 无规则不伪造
    assert by_id["unknown"].status == "abstained" and by_id["unknown"].reason


def test_shadow_only_without_thresholds() -> None:
    """FR-DEC-06/07：未配置校准阈值 → 影子模式，决策不进关键路径。"""
    adapter = RuleDecisionAdapter(rules={"q1": {"answer": "yes"}})
    profile = asyncio_run(adapter.calibration_profile())
    assert profile["shadow_only"] is True
    configured = RuleDecisionAdapter(rules={"q1": {"answer": "yes"}},
                                     confidence_thresholds={"q1": 0.8})
    assert asyncio_run(configured.calibration_profile())["shadow_only"] is False


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)
