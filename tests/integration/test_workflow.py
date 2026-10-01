"""工作流服务测试（FR-WFL-01/02）：版本化发布、hash 幂等、start 映射内部 run。"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from agentos.apps.composition import build_system
from agentos.apps.config import Settings
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import AuthContext, utc_now


def actor() -> AuthContext:
    return AuthContext(
        subject="owner", tenant_id="default", scopes=["read", "write", "approve"],
        policy_version="t", expires_at=utc_now() + timedelta(minutes=10),
    )


def definition() -> dict:
    return {
        "id": "wf-demo",
        "name": "日报工作流",
        "tasks": [
            {"task_id": "t1", "role_id": "worker", "depends_on": [],
             "acceptance_checks": ["contains:done"]},
        ],
    }


@pytest.fixture
def system(tmp_path: Path):
    s = Settings(data_dir=tmp_path / "data")
    handle = build_system(s, model_script=[
        {"when_user_contains": "t1", "script": [{"content": "done"}]},
    ])
    handle.boot()
    return handle, actor()


def test_publish_versioned_and_hash_idempotent(system) -> None:
    handle, a = system
    r1 = handle.workflows.publish(a, definition())
    assert r1["version"] == 1 and r1["reused"] is False
    # 同 hash 重发幂等
    r2 = handle.workflows.publish(a, definition())
    assert r2 == {**r1, "reused": True}
    # 定义变更 → 新版本（不可变：旧版本仍在）
    d2 = definition()
    d2["tasks"][0]["acceptance_checks"] = ["contains:v2"]
    r3 = handle.workflows.publish(a, d2)
    assert r3["version"] == 2 and r3["reused"] is False
    old = handle.workflows.export(a, "日报工作流" if False else "wf-demo" if False else r1["workflow_id"], 1)
    assert old["definition"]["tasks"][0]["acceptance_checks"] == ["contains:done"]


def test_start_maps_internal_run_and_status(system) -> None:
    handle, a = system
    r = handle.workflows.publish(a, definition())
    started = handle.workflows.start(a, r["workflow_id"], r["version"], {}, "wf-key-1")
    assert started["replayed"] is False
    status = handle.workflows.status(a, started["wf_run_id"])
    assert status["definition_version"] == r["version"]
    # 幂等键重放 → 同一内部 run
    again = handle.workflows.start(a, r["workflow_id"], r["version"], {}, "wf-key-1")
    assert again["internal_run_id"] == started["internal_run_id"] and again["replayed"] is True


def test_start_unpublished_version_rejected(system) -> None:
    handle, a = system
    with pytest.raises(AgentOSError) as exc_info:
        handle.workflows.start(a, "ghost", 9, {}, "k")
    assert exc_info.value.code == ErrorCode.INVALID


def test_import_hash_mismatch_rejected(system) -> None:
    handle, a = system
    r = handle.workflows.publish(a, definition())
    with pytest.raises(AgentOSError) as exc_info:
        handle.workflows.import_definition(a, {
            "definition": definition(), "hash": "deadbeef",
        })
    assert exc_info.value.code == ErrorCode.CONFLICT
    _ = r


def test_workflow_end_to_end_execution(system) -> None:
    """发布 → 启动 → 内部 run 由后台 worker 执行至成功（FR-WFL-01 链路）。"""
    handle, a = system
    r = handle.workflows.publish(a, definition())
    started = handle.workflows.start(a, r["workflow_id"], r["version"], {}, "e2e-key")
    state = asyncio.run(handle.worker.drive_until_settled(started["internal_run_id"]))
    assert state == "succeeded"
    status = handle.workflows.status(a, started["wf_run_id"])
    assert status["state"] == "succeeded"
    assert status["delivery"]["completed"] == 1
