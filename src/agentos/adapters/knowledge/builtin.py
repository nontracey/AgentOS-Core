"""内置知识后端适配器：把同步 KnowledgeService 提升为异步 typed KnowledgePort。

- 调用者在事件循环里 await；SQLite 操作用 to_thread 隔离（不阻塞循环）。
- 后端身份与版本随每个 run 固定：`backend_id` / `backend_version` 由这里给出，
  写入 run 的证据里，切换后端后旧 run 仍指向原后端语义。
- 不做任何授权放宽：ACL 过滤在服务层（服务端强制），这里只做协议适配。
"""

from __future__ import annotations

import asyncio
from typing import Any

from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import AuthContext, KnowledgeHit, Ref
from agentos.services.knowledge import KnowledgeService

BACKEND_ID = "builtin-sqlite"
BACKEND_VERSION = "1"
BACKEND_CAPABILITIES = {
    "ingest": True,
    "search": True,
    "resolve_ref": True,
    "delete": True,
    "hybrid": "optional (FTS + vec0，取决于 embedder)",
    "acl": "source/chunk 级，服务端强制",
}


class BuiltinKnowledgeAdapter:
    """内置后端：本地 SQLite（FTS + 可选向量），能力完整。"""

    provider = BACKEND_ID

    def __init__(self, service: KnowledgeService) -> None:
        self._service = service

    # ------------------------------------------------------------ 元信息
    def capabilities(self) -> dict[str, Any]:
        return {"backend": BACKEND_ID, "version": BACKEND_VERSION, **BACKEND_CAPABILITIES}

    @property
    def backend_id(self) -> str:
        return BACKEND_ID

    @property
    def backend_version(self) -> str:
        return BACKEND_VERSION

    @property
    def sync_service(self) -> KnowledgeService:
        """本地同步服务句柄（演示投影的引用验证等同进程场景用）。

        仅内置后端提供；外部后端（WeKnora）没有此语义，getattr 缺省为 None。
        """
        return self._service

    # ------------------------------------------------------------ KnowledgePort
    async def ingest(self, actor: AuthContext, source_id: str, text: str,
                     acl: list[str] | None = None) -> str:
        """返回 source_version（内置后端为内容无关的常量 1，切换后端后可比对）。"""
        result = await asyncio.to_thread(self._service.ingest, actor, source_id, text, acl)
        return str(result.get("source_id") and BACKEND_VERSION or BACKEND_VERSION)

    async def search(self, actor: AuthContext, query: str, limit: int = 5) -> list[KnowledgeHit]:
        rows = await asyncio.to_thread(self._service.search, actor, query, limit)
        return [
            KnowledgeHit(
                chunk_id=str(r["chunk_id"]),
                text=str(r["text"]),
                source_version=str(r.get("source_version") or BACKEND_VERSION),
                refs=[Ref.model_validate(r["ref"])],
            )
            for r in rows
        ]

    async def resolve_ref(self, actor: AuthContext, ref: Ref) -> dict[str, Any]:
        row = await asyncio.to_thread(self._service.resolve_ref, actor, ref)
        return {**row, "backend": BACKEND_ID, "backend_version": BACKEND_VERSION,
                "source_id": ref.source_id}

    async def delete(self, actor: AuthContext, source_id: str) -> None:
        await asyncio.to_thread(self._service.delete_source, actor, source_id)


def backend_from_settings(settings: Any, store: Any, embedder: Any = None) -> Any:
    """组合根用：按配置返回知识后端。

    只在这里做后端选择；kernel/runtime/业务调用者不知道具体实现。
    外部后端未配置或不可用时**不会静默退回内置**（fail closed，避免引用被换源）。
    """
    name = str(getattr(settings, "knowledge_backend", "builtin") or "builtin")
    if name in {"builtin", "builtin-sqlite"}:
        return BuiltinKnowledgeAdapter(KnowledgeService(store, embedder))
    if name in {"weknora", "weknora-http"}:
        from agentos.adapters.knowledge.weknora import WeKnoraConfig, WeKnoraKnowledgeAdapter

        config = WeKnoraConfig.from_settings(settings)
        return WeKnoraKnowledgeAdapter(config, store=store)
    raise AgentOSError(ErrorCode.INVALID, f"unknown knowledge backend: {name}")
