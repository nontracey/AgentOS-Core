"""知识（RAG）、记忆与快决策（FR-KNW-01/03/07、FR-MEM-01/03/04、FR-DEC-01/02/06/07，M3）。

知识：SQLite FTS5 全文检索（trigram 分词可用时用于中文双字词查询）+ sqlite-vec 向量
检索（可选增强）+ RRF 融合。ACL 服务端过滤（查询前后均过滤），内容哈希去重（重复摄取
不重复索引、不重复嵌入调用，FR-KNW-03），引用可回查（resolve_ref 再授权），零命中显式
RETRIEVAL_NO_SOURCE。嵌入端点不可用时以 FTS 为准并在 capabilities 显式标注 hybrid=false
（不伪装混合检索）。

分词/向量空间一致性（FR-KNW-01）：摄取与查询必须同一 embedder（model+维度指纹记录在
knowledge_meta，检索时校验，不一致直接拒绝）；FTS tokenizer 摄取与查询天然同源（FTS5）。

记忆：owner/scope 隔离、来源+置信度溯源、遗忘=删除在线内容+tombstone（审计仅留元数据）、
私有全量导出（开放格式 JSON）。

快决策：规则适配器实现 DecisionPort 契约（noul/choice/score + abstain）；校准未配置时
强制影子模式（shadow_only），规则不得给未知问题伪造概率。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import TYPE_CHECKING, Any

from agentos.adapters.store.sqlite import SqliteStore
from agentos.kernel.decision import DecisionQuestion, DecisionResult
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import AuthContext, Ref

if TYPE_CHECKING:
    from agentos.adapters.embed.ollama import Embedder

_KNOWN_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_sources (
  tenant_id TEXT NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
  owner TEXT NOT NULL, acl_json TEXT NOT NULL DEFAULT '[]', content_hash TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  PRIMARY KEY(tenant_id, id));

CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
  chunk_id UNINDEXED, source_id UNINDEXED, text, tokenize='trigram');

CREATE TABLE IF NOT EXISTS knowledge_chunks (
  tenant_id TEXT NOT NULL, source_id TEXT NOT NULL, chunk_id TEXT NOT NULL,
  content_hash TEXT NOT NULL, text TEXT NOT NULL, acl_json TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY(tenant_id, source_id, chunk_id));

CREATE TABLE IF NOT EXISTS knowledge_meta (
  key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS memory_entries (
  tenant_id TEXT NOT NULL, id TEXT NOT NULL, owner TEXT NOT NULL, scope TEXT NOT NULL,
  content TEXT NOT NULL, content_hash TEXT NOT NULL, source TEXT NOT NULL DEFAULT '',
  confidence REAL NOT NULL DEFAULT 0.5, version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  PRIMARY KEY(tenant_id, id));

CREATE INDEX IF NOT EXISTS memory_owner ON memory_entries(tenant_id, owner, scope);
"""


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def asyncio_run_embed(embedder: Embedder, texts: list[str]) -> list[list[float]]:
    """同步入口内驱动异步嵌入：无运行循环直接 run；已在循环内（gateway 线程）
    则放线程池，避免嵌套 asyncio.run。"""
    import asyncio
    import concurrent.futures

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(embedder.embed(texts))
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, embedder.embed(texts)).result(timeout=120)


class KnowledgeService:
    """内置轻量 RAG：个人默认、离线兜底、KnowledgePort 契约基准。

    hybrid 模式：注入 embedder 后启用 sqlite-vec 向量检索 + RRF 融合；
    无 embedder 或扩展不可用时纯 FTS（capabilities 显式标注，不伪装）。
    """

    def __init__(self, store: SqliteStore, embedder: Embedder | None = None) -> None:
        self._store = store
        self._embedder = embedder
        self._vec_ready: bool | None = None  # None=未探测
        self._conn: sqlite3.Connection = store.raw_connection()
        with store.locked():
            self._conn.executescript(_KNOWN_SCHEMA)
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(knowledge_sources)")}
            if "content_hash" not in cols:
                self._conn.execute("ALTER TABLE knowledge_sources ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''")

    # ------------------------------------------------------------- hybrid
    def capabilities(self) -> dict[str, Any]:
        """显式声明检索能力（FR-KNW-01：不伪装混合检索）。"""
        return {
            "fts": True,
            "fts_tokenizer": "trigram",
            "hybrid": self._vec_ready is True,
            "embedder": self._embedder.model_id if self._embedder else None,
            "vector_space": self._meta_get("vector_space"),
        }

    def _ensure_vec(self) -> bool:
        """惰性加载 sqlite-vec 并建向量表；失败则回退 FTS。

        embedder 为 None 时不缓存 False（embedder 可能稍后注入）；探测结果
        （成功/失败）才缓存，失败原因记入 knowledge_meta 便于诊断。
        """
        if self._embedder is None:
            return False
        if self._vec_ready is not None:
            return self._vec_ready
        try:
            import sqlite_vec  # type: ignore[import-untyped]

            self._conn.enable_load_extension(True)
            sqlite_vec.load(self._conn)
            self._conn.enable_load_extension(False)
            probe = self._conn.execute("SELECT vec_version()").fetchone()[0]
            self._meta_set("vec_version", str(probe))
            self._vec_ready = True
        except Exception as exc:  # noqa: BLE001 - 扩展缺失/加载失败一律回退 FTS
            self._meta_set("vec_disabled_reason", str(exc)[:200])
            self._vec_ready = False
        return self._vec_ready

    def set_embedder(self, embedder: Embedder) -> None:
        """注入/更换嵌入器（更换即不同向量空间，检索时由一致性校验拒绝混用）。"""
        self._embedder = embedder
        self._vec_ready = None  # 允许重新探测

    def _meta_get(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM knowledge_meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _meta_set(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO knowledge_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._conn.commit()

    def _vec_space(self, dim: int) -> str:
        """向量空间指纹（model+dim）：不一致 = 不同空间，必须拒绝混用。"""
        from agentos.adapters.embed.ollama import fingerprint_embedder

        return fingerprint_embedder(self._embedder.model_id if self._embedder else "?", dim)

    def _check_vec_space(self, dim: int) -> None:
        """FR-KNW-01 一致性：库内已锁定的向量空间与本 embedder 不一致 → 拒绝。"""
        locked = self._meta_get("vector_space")
        current = self._vec_space(dim)
        if locked is None:
            self._meta_set("vector_space", current)
        elif locked != current:
            raise AgentOSError(
                ErrorCode.INVALID,
                f"vector space mismatch: index locked to {locked}, embedder is {current} "
                "（摄取与查询必须同一嵌入模型，FR-KNW-01 一致性约束）",
            )

    # ---------------------------------------------------------------- ingest
    def _check_source(self, actor: AuthContext, source_id: str, text: str, acl: list[str]) -> None:
        row = self._conn.execute(
            "SELECT * FROM knowledge_sources WHERE tenant_id=? AND id=?", (actor.tenant_id, source_id)
        ).fetchone()
        if row is None:
            return
        if row["owner"] != actor.subject:
            raise AgentOSError(ErrorCode.DENIED, "knowledge source belongs to another subject")
        digest = _content_hash(text)
        if row["content_hash"] and row["content_hash"] != digest:
            raise AgentOSError(ErrorCode.CONFLICT, "knowledge source version is immutable; use a new source ID")
        if set(json.loads(row["acl_json"])) != set(acl):
            raise AgentOSError(ErrorCode.CONFLICT, "knowledge source ACL cannot change through ingestion")
        if not row["content_hash"]:
            old = [
                r["content_hash"]
                for r in self._conn.execute(
                    "SELECT content_hash FROM knowledge_chunks WHERE tenant_id=? AND source_id=? ORDER BY chunk_id",
                    (actor.tenant_id, source_id),
                )
            ]
            if sorted(old) != sorted(_content_hash(c) for c in self._chunk(text)):
                raise AgentOSError(ErrorCode.CONFLICT, "legacy source content cannot be replaced")
            self._conn.execute(
                "UPDATE knowledge_sources SET content_hash=? WHERE tenant_id=? AND id=?",
                (digest, actor.tenant_id, source_id),
            )

    def ingest(self, actor: AuthContext, source_id: str, text: str, acl: list[str] | None = None) -> dict[str, Any]:
        """摄取文本：切块→内容哈希去重→FTS 索引（+可选向量索引）。同 hash 二次摄取
        零新增且**不重复嵌入调用**（FR-KNW-03）。"""
        if not text.strip():
            raise AgentOSError(ErrorCode.INVALID, "empty document")
        acl = acl or [actor.subject]
        chunks = self._chunk(text)
        use_vec = self._ensure_vec()
        # 去重预检：只对新增 chunk 发起嵌入调用（FR-KNW-03：不重复计费）
        pending: list[tuple[str, str]] = []  # (chunk_id, text)
        with self._conn:
            self._check_source(actor, source_id, text, acl)
            self._conn.execute(
                "INSERT INTO knowledge_sources(tenant_id,id,owner,acl_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,datetime('now')) ON CONFLICT(tenant_id,id) DO NOTHING",
                (actor.tenant_id, source_id, actor.subject, json.dumps(acl), _content_hash(text)),
            )
            new_chunks = 0
            for i, chunk in enumerate(chunks):
                ch = _content_hash(chunk)
                cid = f"{source_id}:{i}"
                exists = self._conn.execute(
                    "SELECT 1 FROM knowledge_chunks WHERE tenant_id=? AND source_id=? AND chunk_id=?",
                    (actor.tenant_id, source_id, cid),
                ).fetchone()
                if exists is not None:
                    continue
                dup = self._conn.execute(
                    "SELECT 1 FROM knowledge_chunks WHERE tenant_id=? AND content_hash=? AND acl_json=?",
                    (actor.tenant_id, ch, json.dumps(acl)),
                ).fetchone()
                if dup is not None:
                    continue  # 内容哈希去重：重复摄取不重复索引（FR-KNW-03）
                self._conn.execute(
                    "INSERT INTO knowledge_chunks(tenant_id,source_id,chunk_id,content_hash,text,acl_json)"
                    " VALUES(?,?,?,?,?,?)",
                    (actor.tenant_id, source_id, cid, ch, chunk, json.dumps(acl)),
                )
                self._conn.execute(
                    "INSERT INTO knowledge_fts(chunk_id, source_id, text) VALUES(?,?,?)",
                    (cid, source_id, chunk),
                )
                pending.append((cid, chunk))
                new_chunks += 1

        if use_vec and pending:
            assert self._embedder is not None
            vectors = asyncio_run_embed(self._embedder, [t for _, t in pending])
            dim = len(vectors[0])
            self._check_vec_space(dim)
            if (
                self._conn.execute(
                    "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='knowledge_vec'"
                ).fetchone()[0]
                == 0
            ):
                self._conn.execute(
                    f"CREATE VIRTUAL TABLE knowledge_vec USING vec0(chunk_id TEXT PRIMARY KEY, embedding float[{dim}])"
                )
            with self._conn:
                for (cid, _), vec in zip(pending, vectors, strict=False):
                    self._conn.execute(
                        "INSERT OR REPLACE INTO knowledge_vec(chunk_id, embedding) VALUES(?,?)",
                        (cid, json.dumps(vec)),
                    )
        self._store.audit_append(
            actor.tenant_id,
            "knowledge.ingested",
            subject=actor.subject,
            run_id=None,
            detail={
                "source_id": source_id,
                "chunks": len(chunks),
                "new": new_chunks,
                "embedded": len(pending) if use_vec else 0,
            },
        )
        return {"source_id": source_id, "chunks": len(chunks), "new_chunks": new_chunks, "hybrid": use_vec}

    def ingest_many(
        self, actor: AuthContext, docs: list[tuple[str, str]], acl: list[str] | None = None, embed_batch: int = 128
    ) -> dict[str, Any]:
        """批量摄取：新增 chunk 的嵌入**合并为批量调用**（每 embed_batch 条一次 HTTP）。

        逐条 ingest 时每个 source 各发一次嵌入请求，万级语料会退化成上万次
        HTTP 往返（10k 实测 >4h）；生产摄取必须走批量。去重语义与 ingest 相同
        （FR-KNW-03：同 hash 不重复索引、不重复嵌入）。
        docs: [(source_id, text), ...]
        """
        if self._embedder is None and docs:
            # 无嵌入器：退化为逐条 FTS 摄取（无嵌入成本，逐条可接受）
            results = [self.ingest(actor, sid, text, acl) for sid, text in docs]
            return {
                "sources": len(results),
                "chunks": sum(r["chunks"] for r in results),
                "new_chunks": sum(r["new_chunks"] for r in results),
                "hybrid": False,
            }
        use_vec = self._ensure_vec()
        acl = acl or [actor.subject]
        pending: list[tuple[str, str, str]] = []  # (source_id, chunk_id, text)
        total_chunks = 0
        with self._conn:
            for sid, text in docs:
                if not text.strip():
                    continue
                self._check_source(actor, sid, text, acl)
                self._conn.execute(
                    "INSERT INTO knowledge_sources(tenant_id,id,owner,acl_json,content_hash,created_at) "
                    "VALUES(?,?,?,?,?,datetime('now')) ON CONFLICT(tenant_id,id) DO NOTHING",
                    (actor.tenant_id, sid, actor.subject, json.dumps(acl), _content_hash(text)),
                )
                for i, chunk in enumerate(self._chunk(text)):
                    cid = f"{sid}:{i}"
                    ch = _content_hash(chunk)
                    total_chunks += 1
                    exists = self._conn.execute(
                        "SELECT 1 FROM knowledge_chunks WHERE tenant_id=? AND chunk_id=?", (actor.tenant_id, cid)
                    ).fetchone()
                    if exists is not None:
                        continue
                    dup = self._conn.execute(
                        "SELECT 1 FROM knowledge_chunks WHERE tenant_id=? AND content_hash=? AND acl_json=?",
                        (actor.tenant_id, ch, json.dumps(acl))
                    ).fetchone()
                    if dup is not None:
                        continue
                    self._conn.execute(
                        "INSERT INTO knowledge_chunks(tenant_id,source_id,chunk_id,"
                        "content_hash,text,acl_json) VALUES(?,?,?,?,?,?)",
                        (actor.tenant_id, sid, cid, ch, chunk, json.dumps(acl)),
                    )
                    self._conn.execute(
                        "INSERT INTO knowledge_fts(chunk_id, source_id, text) VALUES(?,?,?)", (cid, sid, chunk)
                    )
                    pending.append((sid, cid, chunk))

        embedded = 0
        if use_vec and pending:
            assert self._embedder is not None
            for i in range(0, len(pending), embed_batch):
                batch = pending[i : i + embed_batch]
                vectors = asyncio_run_embed(self._embedder, [t for _, _, t in batch])
                self._check_vec_space(len(vectors[0]))
                if (
                    self._conn.execute(
                        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='knowledge_vec'"
                    ).fetchone()[0]
                    == 0
                ):
                    dim = len(vectors[0])
                    self._conn.execute(
                        f"CREATE VIRTUAL TABLE knowledge_vec USING vec0("
                        f"chunk_id TEXT PRIMARY KEY, embedding float[{dim}])"
                    )
                with self._conn:
                    for (cid, _, _), vec in zip(batch, vectors, strict=False):
                        self._conn.execute(
                            "INSERT OR REPLACE INTO knowledge_vec(chunk_id, embedding) VALUES(?,?)",
                            (cid, json.dumps(vec)),
                        )
                embedded += len(batch)
        self._store.audit_append(
            actor.tenant_id,
            "knowledge.ingested",
            subject=actor.subject,
            run_id=None,
            detail={"batch_sources": len(docs), "chunks": total_chunks, "new": len(pending), "embedded": embedded},
        )
        return {
            "sources": len(docs),
            "chunks": total_chunks,
            "new_chunks": len(pending),
            "hybrid": use_vec,
            "embedded": embedded,
        }

    @staticmethod
    def _chunk(text: str, size: int = 400) -> list[str]:
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        chunks: list[str] = []
        for para in paragraphs:
            if len(para) <= size:
                chunks.append(para)
            else:
                chunks.extend(para[i : i + size] for i in range(0, len(para), size))
        return chunks or [text]

    # ---------------------------------------------------------------- search
    @staticmethod
    def _rrf_fuse(*ranked_lists: list[str], k: int = 60) -> dict[str, float]:
        """Reciprocal Rank Fusion：多路排名融合（FTS 与向量各有偏好，RRF 稳健）。"""
        scores: dict[str, float] = {}
        for ranked in ranked_lists:
            for rank, cid in enumerate(ranked):
                scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
        return scores

    def _vec_candidates(self, query: str, k: int) -> list[tuple[str, float]]:
        """向量 KNN 候选（sqlite-vec）；扩展/表缺失返回空（回退 FTS）。"""
        if not self._ensure_vec() or self._embedder is None:
            return []
        if (
            self._conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='knowledge_vec'"
            ).fetchone()[0]
            == 0
        ):
            return []
        try:
            [qvec] = asyncio_run_embed(self._embedder, [query])
            self._check_vec_space(len(qvec))
            rows = self._conn.execute(
                "SELECT chunk_id, distance FROM knowledge_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                (json.dumps(qvec), k),
            ).fetchall()
            return [(r["chunk_id"], float(r["distance"])) for r in rows]
        except AgentOSError:
            raise
        except Exception:  # noqa: BLE001 - 向量通道故障回退 FTS（显式标注在 capabilities）
            return []

    @staticmethod
    def _fts_query(query: str) -> str:
        """FTS5 MATCH 语法安全化：拆 token 后双引号短语、AND 连接。

        用户查询可能携带 FTS5 语法字符（连字符/冒号/引号等），直接 MATCH 会被
        解析成列过滤或 NOT（实测 "tamper-evident" → "no such column: evident"）。
        双引号短语与 trigram tokenizer 兼容（≥3 字短语照常命中）。
        """
        import re

        tokens = re.findall(r"[\w\u4e00-\u9fff]+", query)
        return " ".join(f'"{t}"' for t in tokens if t)

    def search(self, actor: AuthContext, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """混合检索（FTS + 向量 RRF 融合）+ ACL 过滤（服务端强制）；
        零命中 → RETRIEVAL_NO_SOURCE。"""
        if not query.strip():
            raise AgentOSError(ErrorCode.INVALID, "empty query")
        fts_query = self._fts_query(query)
        fts_rows = (
            self._conn.execute(
                "SELECT chunk_id, source_id, text FROM knowledge_fts WHERE knowledge_fts MATCH ? "
                "ORDER BY bm25(knowledge_fts) LIMIT ?",
                (fts_query, limit * 4),
            ).fetchall()
            if fts_query
            else []
        )
        fts_ranked = [r["chunk_id"] for r in fts_rows]
        vec_ranked = [cid for cid, _ in self._vec_candidates(query, limit * 4)]
        fused = self._rrf_fuse(fts_ranked, vec_ranked)
        ordered = sorted(fused, key=lambda c: -fused[c])
        text_of = {r["chunk_id"]: r["text"] for r in fts_rows}
        # 向量命中但 FTS 未覆盖的 chunk 补文本
        missing = [c for c in ordered if c not in text_of]
        for cid in missing:
            row = self._conn.execute("SELECT text FROM knowledge_chunks WHERE chunk_id=?", (cid,)).fetchone()
            if row:
                text_of[cid] = row["text"]

        hits: list[dict[str, Any]] = []
        seen: set[str] = set()
        for cid in ordered:
            if cid in seen:
                continue
            source_id = cid.rsplit(":", 1)[0]
            if self.is_tombstoned(actor.tenant_id, source_id):
                continue  # 已删除来源即使残留索引也不返回
            acl = self._acl_of(actor.tenant_id, source_id)
            if actor.subject not in acl and "*" not in acl:
                continue  # 服务端权限过滤，不依赖调用方（FR-KNW-01）
            seen.add(cid)
            text = text_of.get(cid, "")
            hits.append(
                {
                    "chunk_id": cid,
                    "source_id": source_id,
                    "text": text,
                    "ref": Ref(source_id=source_id, source_version="1", locator=cid, quote=text[:80]).model_dump(
                        mode="json"
                    ),
                }
            )
            if len(hits) >= limit:
                break
        if not hits:
            raise AgentOSError(
                ErrorCode.INVALID,
                "RETRIEVAL_NO_SOURCE",
                detail={"code": "RETRIEVAL_NO_SOURCE", "query": query[:100]},
            )
        return hits

    def resolve_ref(self, actor: AuthContext, ref: Ref) -> dict[str, Any]:
        """引用回查：重新授权 + 固定来源版本。"""
        if self.is_tombstoned(actor.tenant_id, ref.source_id):
            raise AgentOSError(ErrorCode.DENIED, f"source deleted (tombstoned): {ref.source_id}")
        row = self._conn.execute(
            "SELECT source_id, text, acl_json FROM knowledge_chunks WHERE tenant_id=? AND chunk_id=?",
            (actor.tenant_id, ref.locator),
        ).fetchone()
        if row is None:
            raise AgentOSError(ErrorCode.INVALID, f"chunk not found: {ref.locator}")
        if row["source_id"] != ref.source_id or ref.source_version != "1":
            raise AgentOSError(ErrorCode.DENIED, "reference source/version does not match the stored chunk")
        if ref.quote and ref.quote not in row["text"]:
            raise AgentOSError(ErrorCode.INVALID, "reference quote does not occur in the stored chunk")
        acl = json.loads(row["acl_json"])
        if actor.subject not in acl and "*" not in acl:
            raise AgentOSError(ErrorCode.DENIED, f"chunk not authorized: {ref.locator}")
        return {"chunk_id": ref.locator, "text": row["text"]}

    def delete_source(self, actor: AuthContext, source_id: str) -> int:
        with self._conn:
            self._conn.execute("DELETE FROM knowledge_fts WHERE source_id=?", (source_id,))
            cur = self._conn.execute(
                "DELETE FROM knowledge_chunks WHERE tenant_id=? AND source_id=?", (actor.tenant_id, source_id)
            )
            self._conn.execute("DELETE FROM knowledge_sources WHERE tenant_id=? AND id=?", (actor.tenant_id, source_id))
            if self._vec_ready:
                for cid in [
                    r[0]
                    for r in self._conn.execute(
                        "SELECT chunk_id FROM knowledge_vec WHERE chunk_id LIKE ?",
                        (f"{source_id}:%",),
                    ).fetchall()
                ]:
                    self._conn.execute("DELETE FROM knowledge_vec WHERE chunk_id=?", (cid,))
        # 删除 tombstone：删除是事实，后续 search/resolve/导出都不得再返回（FR-KNW-08）
        self._store.add_tombstone(actor.tenant_id, f"knowledge:{source_id}")
        self._store.audit_append(
            actor.tenant_id,
            "knowledge.deleted",
            subject=actor.subject,
            run_id=None,
            detail={"source_id": source_id, "chunks": cur.rowcount, "tombstoned": True},
        )
        return cur.rowcount

    def is_tombstoned(self, tenant_id: str, source_id: str) -> bool:
        return f"knowledge:{source_id}" in set(self._store.list_tombstones(tenant_id))

    def chunk_meta(self, tenant_id: str, chunk_id: str) -> dict[str, Any] | None:
        """引用发布/失效验证用：片段存在性、归属、正文与 chunk 级 ACL（不对外下发正文）。"""
        row = self._conn.execute(
            "SELECT source_id, text, acl_json FROM knowledge_chunks WHERE tenant_id=? AND chunk_id=?",
            (tenant_id, chunk_id),
        ).fetchone()
        if row is None:
            return None
        return {
            "source_id": row["source_id"],
            "text": row["text"],
            "acl": json.loads(row["acl_json"]) if row["acl_json"] else [],
        }

    def source_acl(self, tenant_id: str, source_id: str) -> list[str]:
        """来源当前 ACL（演示发布快照比对 ACL 收紧用）。"""
        return self._acl_of(tenant_id, source_id)

    def _acl_of(self, tenant_id: str, source_id: str) -> list[str]:
        row = self._conn.execute(
            "SELECT acl_json FROM knowledge_sources WHERE tenant_id=? AND id=?",
            (tenant_id, source_id),
        ).fetchone()
        acl: list[str] = json.loads(row["acl_json"]) if row else []
        return acl


class MemoryService:
    """长期记忆（FR-MEM-01/03/04）：scope 隔离、溯源、遗忘 tombstone、私有导出。"""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store
        self._conn: sqlite3.Connection = store.raw_connection()
        with self._conn:
            self._conn.executescript(_KNOWN_SCHEMA)
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(knowledge_sources)")}
            if "content_hash" not in cols:
                self._conn.execute("ALTER TABLE knowledge_sources ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''")

    def put(self, actor: AuthContext, scope: str, content: str, source: str = "", confidence: float = 0.5) -> str:
        if not content.strip():
            raise AgentOSError(ErrorCode.INVALID, "empty memory content")
        entry_id = f"mem_{_content_hash(scope + content)[:12]}"
        with self._conn:
            existing = self._conn.execute(
                "SELECT content_hash FROM memory_entries WHERE tenant_id=? AND id=?",
                (actor.tenant_id, entry_id),
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    "INSERT INTO memory_entries(tenant_id,id,owner,scope,content,content_hash,"
                    "source,confidence,created_at) VALUES(?,?,?,?,?,?,?,?,datetime('now'))",
                    (
                        actor.tenant_id,
                        entry_id,
                        actor.subject,
                        scope,
                        content,
                        _content_hash(content),
                        source,
                        confidence,
                    ),
                )
        self._store.audit_append(
            actor.tenant_id,
            "memory.written",
            subject=actor.subject,
            run_id=None,
            detail={"entry_id": entry_id, "scope": scope},
        )
        return entry_id

    def query(self, actor: AuthContext, scope: str | None = None, keyword: str | None = None) -> list[dict[str, Any]]:
        """读取按 owner 隔离（服务端过滤）；关键词可选。"""
        sql = "SELECT * FROM memory_entries WHERE tenant_id=? AND owner=?"
        params: list[Any] = [actor.tenant_id, actor.subject]
        if scope is not None:
            sql += " AND scope=?"
            params.append(scope)
        if keyword:
            sql += " AND content LIKE ?"
            params.append(f"%{keyword}%")
        rows = self._conn.execute(sql + " ORDER BY created_at DESC", params).fetchall()
        return [dict(r) for r in rows]

    def forget(self, actor: AuthContext, entry_id: str) -> None:
        """遗忘：删除在线内容，tombstone 留痕，审计仅元数据（FR-MEM-03）。"""
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM memory_entries WHERE tenant_id=? AND id=? AND owner=?",
                (actor.tenant_id, entry_id, actor.subject),
            )
            if cur.rowcount:
                self._conn.execute(
                    "INSERT OR IGNORE INTO deletion_tombstones(tenant_id,resource_id,deleted_at)"
                    " VALUES(?,?,datetime('now'))",
                    (actor.tenant_id, entry_id),
                )
        if cur.rowcount == 0:
            raise AgentOSError(ErrorCode.INVALID, f"memory entry not found: {entry_id}")
        self._store.audit_append(
            actor.tenant_id,
            "memory.forgotten",
            subject=actor.subject,
            run_id=None,
            detail={"entry_id": entry_id},  # 不含原文（FR-MEM-03）
        )

    def private_backup(self, actor: AuthContext) -> dict[str, Any]:
        """私有全量备份（授权导出；开放格式 JSON，FR-MEM-04）。"""
        rows = self._conn.execute(
            "SELECT * FROM memory_entries WHERE tenant_id=? AND owner=?",
            (actor.tenant_id, actor.subject),
        ).fetchall()
        return {
            "kind": "memory_private_backup",
            "owner": actor.subject,
            "entries": [dict(r) for r in rows],
        }

    def restore_from_backup(self, actor: AuthContext, backup: dict[str, Any]) -> int:
        """备份恢复前重放 tombstone（FR-MEM-04：已遗忘记忆不可复活）。"""
        restored = 0
        for entry in backup.get("entries", []):
            tomb = self._conn.execute(
                "SELECT 1 FROM deletion_tombstones WHERE tenant_id=? AND resource_id=?",
                (actor.tenant_id, entry["id"]),
            ).fetchone()
            if tomb is not None:
                continue
            with self._conn:
                self._conn.execute(
                    "INSERT OR IGNORE INTO memory_entries(tenant_id,id,owner,scope,content,"
                    "content_hash,source,confidence,version,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,1,datetime('now'))",
                    (
                        actor.tenant_id,
                        entry["id"],
                        entry["owner"],
                        entry["scope"],
                        entry["content"],
                        entry["content_hash"],
                        entry["source"],
                        entry["confidence"],
                    ),
                )
            restored += 1
        return restored


class RuleDecisionAdapter:
    """规则快决策（FR-DEC-01 rule_fallback 默认后端）。

    - 只覆盖声明规则（rules: question_id → 答案），未知问题弃权（FR-DEC-01）。
    - calibration_profile 无阈值 → shadow_only=True（FR-DEC-06/07：默认影子模式）。
    - 规则不得伪造概率：choice 由规则给出标签时分布为 {label:1.0}，confidence=1.0
      （确定性规则，非模型估计）；noul 给 0/1 确定值。
    """

    def __init__(
        self, rules: dict[str, Any] | None = None, confidence_thresholds: dict[str, float] | None = None
    ) -> None:
        self._rules = rules or {}
        self._thresholds = confidence_thresholds

    async def decide(self, state: dict[str, Any], questions: list[DecisionQuestion]) -> list[DecisionResult]:
        results: list[DecisionResult] = []
        for q in questions:
            rule = self._rules.get(q.id)
            if rule is None:
                results.append(
                    DecisionResult(
                        question_id=q.id,
                        status="abstained",
                        kind=q.kind,
                        reason="no rule configured",
                    )
                )
                continue
            if q.kind == "noul":
                value = 1.0 if rule.get("answer") in (True, "true", "yes") else 0.0
                results.append(
                    DecisionResult(
                        question_id=q.id,
                        status="answered",
                        kind="noul",
                        value=value,
                        reason=rule.get("reason", "rule"),
                    )
                )
            elif q.kind == "choice":
                label = str(rule.get("answer", ""))
                if label not in q.criteria:
                    results.append(
                        DecisionResult(
                            question_id=q.id,
                            status="abstained",
                            kind="choice",
                            reason="rule label not in criteria",
                        )
                    )
                    continue
                results.append(
                    DecisionResult(
                        question_id=q.id,
                        status="answered",
                        kind="choice",
                        value=label,
                        probabilities={c: (1.0 if c == label else 0.0) for c in q.criteria},
                        confidence=1.0,
                        reason=rule.get("reason", "rule"),
                    )
                )
            elif q.kind == "score":
                results.append(
                    DecisionResult(
                        question_id=q.id,
                        status="answered",
                        kind="score",
                        value=float(rule.get("answer", 0)),
                        legend=list(q.criteria),
                        reason=rule.get("reason", "rule"),
                    )
                )
        return results

    async def calibration_profile(self) -> dict[str, Any]:
        """FR-DEC-07：未配置阈值 → shadow_only，决策不得进关键路径。"""
        return {
            "backend": "rule",
            "shadow_only": not self._thresholds,
            "confidence_thresholds": self._thresholds or {},
        }
