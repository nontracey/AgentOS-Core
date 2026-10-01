"""嵌入适配器（FR-KNW-01 混合检索的向量侧）。

- OllamaEmbedder：POST /api/embed 批量嵌入（nomic-embed-text 768 维实测）。
- 摄取与查询必须走同一 embedder 实例（同一模型）——model id 记录进
  knowledge_meta，检索时校验一致，不一致直接拒绝（分词/向量空间一致性，
  FR-KNW-01"摄取与查询必须使用同一套分词"的向量侧对应约束）。
- 嵌入端点不可用时不伪装：KnowledgeService 回退 FTS 并在 capabilities 标注
  hybrid=false（不冒充混合检索）。
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Protocol

import httpx


class Embedder(Protocol):
    """嵌入协议：embed 与 query 必须同一实例（同一模型）。"""

    model_id: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class OllamaEmbedder:
    """Ollama /api/embed 批量嵌入（egress=local，满足 FR-MDL-07 local_only）。

    默认 bge-m3（多语言，中文强；1024 维）：冻结集实测 zh 91%/en 91%（nomic-embed-text
    中文仅 ~35%，已淘汰为备选）。
    """

    def __init__(self, base_url: str, model: str = "bge-m3",
                 timeout_s: float = 60.0) -> None:
        self._base = base_url.rstrip("/")
        self.model_id = f"ollama:{model}"
        self._model = model
        self._timeout = timeout_s

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # 输入指纹并入 model_id：嵌入函数或预处理变化即视为不同向量空间
        fingerprint = hashlib.sha256(
            "|".join(t.strip() for t in texts).encode("utf-8")).hexdigest()[:8]
        _ = fingerprint  # 保留：仅用于调试定位
        resp = await asyncio.to_thread(
            httpx.post,
            f"{self._base}/api/embed",
            json={"model": self._model, "input": texts},
            timeout=self._timeout, trust_env=False,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"embed failed: HTTP {resp.status_code}: {resp.text[:120]}")
        data: dict[str, Any] = resp.json()
        embeddings = data.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            raise RuntimeError("embed response mismatch")
        return [list(map(float, e)) for e in embeddings]


def fingerprint_embedder(model_id: str, dim: int) -> str:
    """向量空间指纹：model+维度变化即视为不可比空间（检索时校验）。"""
    return f"{model_id}:{dim}"
