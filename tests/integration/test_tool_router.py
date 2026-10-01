"""FR-TOOL-06 工具语义路由验收：冻结集 Recall@12 + 低置信回退。

- 冻结集评测（100 工具/50 查询，合成语料如实标注）需要嵌入端点
  （AGENTOS_KNW_EVAL_EMBED），正式验收以 evidence 实测为准。
- 回退用例用本地确定性假嵌入（不依赖外部端点）。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from agentos.adapters.embed.ollama import OllamaEmbedder
from agentos.services.tool_router import SemanticToolRouter, build_frozen_set

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tool_router_eval.json"
EVAL_ENDPOINT = os.environ.get("AGENTOS_KNW_EVAL_EMBED", "")


class _NoiseEmbedder:
    """确定性噪声嵌入：SHA256 位向量——跨进程稳定、可复现，但无语义相关性，
    使跨域查询相似度低于阈值 → 触发可解释回退（Python hash 有随机化不可用）。"""

    model_id = "noise:1"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import hashlib

        out = []
        for t in texts:
            digest = hashlib.sha256(("noise:" + t).encode("utf-8")).digest()
            v = [float(b) - 127.5 for b in digest[:32]]  # 中心化：随机 cosine≈0
            norm = sum(x * x for x in v) ** 0.5 or 1.0
            out.append([x / norm for x in v])
        return out


def test_low_confidence_falls_back_to_categorical() -> None:
    """最高相似度低于阈值 → 回退可解释的分类检索（FR-TOOL-06 判据后半句）。"""
    router = SemanticToolRouter(_NoiseEmbedder(), recall_threshold=0.5)

    async def setup() -> None:
        await router.index([
            {"name": "fs_read", "description": "read file", "category": "fs"},
            {"name": "db_query", "description": "query db", "category": "db"},
        ])

    asyncio.run(setup())
    result = asyncio.run(router.route("backup database"))
    assert result["mode"] == "categorical"
    assert "db_query" in result["tools"]  # 分类关键词命中 category


def test_semantic_route_top_n_within_limit() -> None:
    """正常语义路由：Top-N ≤12、模式=semantic。"""
    router = SemanticToolRouter(_NoiseEmbedder(), recall_threshold=0.0)

    async def setup() -> None:
        await router.index([
            {"name": f"t{i}", "description": f"tool {i}", "category": "x"} for i in range(30)
        ])

    asyncio.run(setup())
    result = asyncio.run(router.route("tool"))
    assert len(result["tools"]) <= 12
    assert result["mode"] == "semantic"


@pytest.mark.skipif(not EVAL_ENDPOINT,
                    reason="需要嵌入端点（AGENTOS_KNW_EVAL_EMBED），正式验收以 evidence 为准")
def test_frozen_recall_at_12() -> None:
    """冻结集（100 工具/50 查询）Top-N 必需工具 Recall@12 ≥ 95%。"""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    router = SemanticToolRouter(OllamaEmbedder(EVAL_ENDPOINT))

    async def run() -> tuple[float, list[str]]:
        await router.index(data["tools"])
        hit, misses = 0, []
        for q in data["queries"]:
            r = await router.route(q["query"])
            if any(t.startswith(q["gold_prefix"]) for t in r["tools"]):
                hit += 1
            else:
                misses.append(q["qid"])
        return hit / len(data["queries"]), misses

    recall, misses = asyncio.run(run())
    print(f"\nRecall@12 = {recall:.2%} misses={misses}")
    assert recall >= 0.95, f"Recall@12 未达标: {recall:.2%}"


def test_frozen_set_meets_minimums() -> None:
    """冻结集规模判据：≥100 工具、≥50 查询（无需端点）。"""
    data = build_frozen_set()
    assert len(data["tools"]) >= 100
    assert len(data["queries"]) >= 50
