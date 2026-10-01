"""FR-GOV-05 冻结注入样本集：30 条样本越权执行数 0，策略拒绝事件可追溯。

每个样本独立沙箱运行。全局不变量：
- 写意图样本不产生任何沙箱内文件（执行数 0）；
- 样本终态符合 expect（failed / awaiting_approval）；
- 拒绝路径有 policy.denied 或 policy.needs_approval 审计；
- 任何样本不得以 succeeded 收场（M0 治理语义：未批准的写意图绝不放行）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentos.apps.composition import build_system
from agentos.apps.config import Settings

FIXTURE = json.loads(
    (Path(__file__).parents[2] / "contracts" / "fixtures" / "injection_samples.json").read_text(
        encoding="utf-8"
    )
)
SAMPLES = FIXTURE["samples"]


def _run_sample(tmp_path: Path, sample: dict) -> tuple[str, list[str]]:
    data_dir = tmp_path / sample["id"]
    system = build_system(Settings(data_dir=data_dir), model_script=sample["turns"])
    system.boot()
    token = json.loads(system.settings.identity_file.read_text(encoding="utf-8"))["subjects"]["owner"]["token"]

    async def _drive() -> str:
        receipt = await system.ingress.accept(
            {"payload": {"goal": sample["goal"]}},
            channel="cli",
            credentials={"token": token},
        )
        return await system.worker.drive_until_settled(receipt.run_id)

    import asyncio

    state = asyncio.run(_drive())
    sandbox = system.settings.sandbox_dir
    files = [str(p.relative_to(sandbox)) for p in sandbox.rglob("*") if p.is_file()]
    return state, files


def _assertions(sample: dict, state: str, files: list[str], data_dir: Path) -> None:
    # 全局不变量：注入样本永不成功
    assert state != "succeeded", f"{sample['id']} 不应以 succeeded 收场"
    # 全局不变量：沙箱无任何写入（越权执行数 0）
    assert files == [], f"{sample['id']} 产生了沙箱写入: {files}"

    if sample["expect"] == "failed":
        assert state == "failed", f"{sample['id']} 期望 failed，实际 {state}"
    elif sample["expect"] == "awaiting_approval":
        assert state == "awaiting_approval", f"{sample['id']} 期望阻塞审批，实际 {state}"
        # 批准被请求但未消费（参数摘要存在，等待 owner 裁决）
        run = _load_run(data_dir)
        steps = _load_steps(data_dir, run["id"])
        assert steps, f"{sample['id']} 应有步骤记录"
        assert any(s["state"] == "awaiting_approval" for s in steps), f"{sample['id']} 步骤应停在审批"


def _load_run(data_dir: Path) -> dict:
    import sqlite3

    conn = sqlite3.connect(data_dir / "agentos.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM runs ORDER BY created_at LIMIT 1").fetchone()
    conn.close()
    return dict(row)


def _load_steps(data_dir: Path, run_id: str) -> list[dict]:
    import sqlite3

    conn = sqlite3.connect(data_dir / "agentos.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM steps WHERE run_id=?", (run_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@pytest.mark.parametrize("sample", SAMPLES, ids=[s["id"] for s in SAMPLES])
def test_injection_sample(sample: dict, tmp_path: Path) -> None:
    state, files = _run_sample(tmp_path, sample)
    _assertions(sample, state, files, tmp_path / sample["id"])


def test_frozen_sample_set_size_and_rejections_audited(tmp_path: Path) -> None:
    """冻结集完整性：≥30 条；拒绝路径有审计事件（FR-GOV-05 可追溯）。"""
    assert len(SAMPLES) >= 30
    data_dir = tmp_path / "audit-probe"
    sample = next(s for s in SAMPLES if s["expect"] == "failed")
    system = build_system(Settings(data_dir=data_dir), model_script=sample["turns"])
    system.boot()
    token = json.loads(system.settings.identity_file.read_text(encoding="utf-8"))["subjects"]["owner"]["token"]

    import asyncio

    async def _drive() -> None:
        receipt = await system.ingress.accept(
            {"payload": {"goal": sample["goal"]}}, channel="cli", credentials={"token": token}
        )
        await system.worker.drive_until_settled(receipt.run_id)

    asyncio.run(_drive())
    kinds = [e.kind for e in system.deps.audit.query("default")]
    assert "policy.denied" in kinds, "确定性拒绝必须可追溯"
