"""FR-KNW-01 混合检索验收：冻结集 Recall@5、向量空间一致性、越权隔离。

- 冻结集评测（150 条：中 90/英 60）需要真实嵌入端点（Windows Ollama
  nomic-embed-text），由 AGENTOS_KNW_EVAL_EMBED 指向；不可用时 skip——
  正式验收以 evidence/ 记录的实测为准（判据不缩水：skip 不算 pass）。
- 一致性/越权/分词回归用本地确定性假嵌入即可（不依赖外部端点）。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from agentos.adapters.embed.ollama import OllamaEmbedder
from agentos.adapters.store.sqlite import SqliteStore
from agentos.apps.composition import SystemHandle
from agentos.kernel.errors import AgentOSError
from agentos.kernel.types import AuthContext, utc_now
from agentos.services.knowledge import KnowledgeService
from tests.conftest import owner_token

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "knw_eval.json"
EVAL_ENDPOINT = os.environ.get("AGENTOS_KNW_EVAL_EMBED", "")


class _HashEmbedder:
    """确定性哈希嵌入（单测用，不追求语义质量，只追求可复现与维度可控）。"""

    def __init__(self, model_id: str, dim: int = 64) -> None:
        self.model_id = model_id
        self._dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            v = [0.0] * self._dim
            for i, ch in enumerate(t):
                v[(hash((self.model_id, ch, i)) % self._dim)] += 1.0
            norm = sum(x * x for x in v) ** 0.5 or 1.0
            out.append([x / norm for x in v])
        return out


def _actor(subject: str = "owner") -> AuthContext:
    return AuthContext(subject=subject, tenant_id="default", scopes=["read", "write"],
                       policy_version="t", expires_at=utc_now())


def _corpus_actors() -> tuple[KnowledgeService, AuthContext]:
    store = SqliteStore(":memory:")
    ks = KnowledgeService(store)
    return ks, _actor()


# ----------------------------------------------------------------- 一致性/越权
def test_vector_space_mismatch_rejected() -> None:
    """摄取与查询用了不同嵌入模型（不同向量空间）→ 检索直接拒绝（FR-KNW-01 一致性）。"""
    ks, actor = _corpus_actors()
    ks._embedder = _HashEmbedder("model-A")  # noqa: SLF001 - 直接注入
    ks.ingest(actor, "src1", "写前日志保证崩溃恢复。")
    # 换成另一个模型的 embedder 查询 → 向量空间指纹不一致 → 必须拒绝而非静默混算
    ks._embedder = _HashEmbedder("model-B")  # noqa: SLF001
    with pytest.raises(AgentOSError, match="vector space mismatch"):
        ks.search(actor, "崩溃恢复")


def test_same_embedder_hybrid_search_works() -> None:
    """同 embedder 摄取+查询：混合检索命中，capabilities.hybrid=True。"""
    ks, actor = _corpus_actors()
    ks._embedder = _HashEmbedder("model-A")  # noqa: SLF001
    ks.ingest(actor, "src1", "写前日志保证崩溃恢复。内核从检查点继续。")
    hits = ks.search(actor, "检查点")
    assert hits and hits[0]["source_id"] == "src1"
    assert ks.capabilities()["hybrid"] is True
    assert ks.capabilities()["vector_space"] == "model-A:64"


def test_cross_subject_zero_leak_hybrid() -> None:
    """混合模式下越权 chunk 返回 0（FR-KNW-01 隔离判据）。"""
    ks, owner = _corpus_actors()
    ks._embedder = _HashEmbedder("model-A")  # noqa: SLF001
    ks.ingest(owner, "secret", "机密条目：审计链条的盐值与指纹算法细节。", acl=["owner"])
    ks.ingest(owner, "public", "公开条目：部署手册的目录结构。", acl=["*"])
    stranger = _actor("stranger")
    hits = ks.search(stranger, "审计链条 盐值 指纹")
    assert all(h["source_id"] != "secret" for h in hits), "越权内容泄露"
    # 直接查秘密词（trigram 命中 fts）也必须被权限过滤掉
    assert all("盐值" not in h["text"] for h in hits)


def test_tokenizer_consistency_regression() -> None:
    """分词一致性回归（FR-KNW-01：不一致的用例必须失败）+ 双字词短板如实暴露：

    - trigram tokenizer 对 <3 字查询**无匹配**（SQLite 规定）——中文双字词
      在纯 FTS 侧召回为 0，这正是判据强制"中文含双字词查询"的原因；
    - 双字词由混合检索的向量侧补齐（同 embedder 摄取+查询）；
    - 换 unicode61 建对照索引证明：tokenizer 单侧被改 → 召回行为改变 → 本用例失败。
    """
    ks, actor = _corpus_actors()
    ks.ingest(actor, "zh-doc", "审计链条按顺序串联指纹，篡改任何一条都会校验失败。")

    # ① 纯 FTS：双字词"审计"无匹配（trigram 短板，如实断言而非掩盖）
    with pytest.raises(AgentOSError, match="RETRIEVAL_NO_SOURCE"):
        ks.search(actor, "审计")
    # 长 query（≥3 字）FTS 正常
    hits = ks.search(actor, "审计链条")
    assert any(h["source_id"] == "zh-doc" for h in hits)

    # ② 混合模式：双字词由向量侧召回
    ks._embedder = _HashEmbedder("model-A")  # noqa: SLF001
    ks.ingest(actor, "zh-doc-2", "审计链条按顺序串联指纹。")  # 混合索引（含向量）
    hits = ks.search(actor, "审计")
    assert any(h["source_id"] == "zh-doc-2" for h in hits), "双字词混合召回失败"
    assert ks.capabilities()["hybrid"] is True

    # ③ 对照：unicode61 tokenizer 的同构索引（模拟"另一侧分词被改"）
    conn = ks._conn  # noqa: SLF001
    conn.execute("CREATE VIRTUAL TABLE fts_bad USING fts5("
                 "chunk_id UNINDEXED, source_id UNINDEXED, text, tokenize='unicode61')")
    conn.execute("INSERT INTO fts_bad(chunk_id, source_id, text) VALUES('zh-doc:0','zh-doc',"
                 "'审计链条按顺序串联指纹，篡改任何一条都会校验失败。')")
    conn.commit()
    bad = conn.execute(
        "SELECT count(*) FROM fts_bad WHERE fts_bad MATCH '审计链条'").fetchone()[0]
    assert bad == 0, "unicode61 下中文查询竟然命中——回归前置条件失效"


# ----------------------------------------------------------------- 冻结集评测
@pytest.mark.skipif(not EVAL_ENDPOINT,
                    reason="需要真实嵌入端点（AGENTOS_KNW_EVAL_EMBED），正式验收以 evidence 实测为准")
def test_hybrid_recall_frozen_set(
    make_system: Callable[..., SystemHandle], data_dir: Path,
) -> None:
    """冻结集 Recall@5 ≥ 80%（中英各自统计；中文含双字词）。"""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    system = make_system(model_script=[{"content": "eval"}])
    system.boot()
    ks = KnowledgeService(system.store, OllamaEmbedder(EVAL_ENDPOINT))
    actor = _actor(owner_token(system))
    for doc in data["corpus"]:
        ks.ingest(actor, doc["source_id"], doc["text"])
    stats = {"zh": [0, 0], "en": [0, 0]}
    misses: list[str] = []
    for q in data["queries"]:
        try:
            hits = ks.search(actor, q["query"], limit=5)
            got = [h["source_id"] for h in hits]
        except AgentOSError:
            got = []
        # gold 按主题前缀判定（语料 source_id 为 {topic}-zh / {topic}-en）
        ok = any(g == q["gold"] or g.startswith(q["gold"] + "-") for g in got)
        stats[q["lang"]][0] += int(ok)
        stats[q["lang"]][1] += 1
        if not ok:
            misses.append(f"{q['qid']}: {q['query']}")
    zh_r = stats["zh"][0] / stats["zh"][1]
    en_r = stats["en"][0] / stats["en"][1]
    print(f"\nRecall@5 zh={zh_r:.2%} ({stats['zh'][0]}/{stats['zh'][1]}) "
          f"en={en_r:.2%} ({stats['en'][0]}/{stats['en'][1]})")
    if misses:
        print("misses:", "\n".join(misses[:10]))
    assert zh_r >= 0.80 and en_r >= 0.80, f"Recall@5 未达标 zh={zh_r:.2%} en={en_r:.2%}"
