"""CLI e2e：boot → submit（审批流）→ approvals decide → task get/cancel。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentos.apps.cli import app as cli_app
from tests.conftest import IDEMPOTENT_APPEND_SCRIPT, WRITE_FLOW_SCRIPT

runner = CliRunner()


@pytest.fixture
def cli_env(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AGENTOS_DATA_DIR", str(data_dir))
    return data_dir


def _write_script(data_dir: Path, script: list[dict[str, object]]) -> Path:
    path = data_dir / "model-script.json"
    path.write_text(json.dumps(script), encoding="utf-8")
    return path


def test_boot_initializes(cli_env: Path) -> None:
    result = runner.invoke(cli_app, ["boot"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "ok"
    assert payload["governance"] == "deny"
    assert (cli_env / "agentos.db").exists()
    assert (cli_env / "identity.json").exists()


def test_submit_approval_flow_e2e(cli_env: Path) -> None:
    runner.invoke(cli_app, ["boot"])
    script = _write_script(cli_env, WRITE_FLOW_SCRIPT)
    r = runner.invoke(cli_app, ["task", "submit", "写一个文件", "--script", str(script)])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["state"] == "awaiting_approval"

    # 审批列表可见
    r = runner.invoke(cli_app, ["approvals", "list"])
    assert r.exit_code == 0, r.output
    approvals = json.loads(r.output)
    assert len(approvals) == 1
    approval_id = approvals[0]["id"]

    # 批准 → run 续跑至成功
    r = runner.invoke(cli_app, ["approvals", "decide", approval_id, "--approve"])
    assert r.exit_code == 0, r.output
    decided = json.loads(r.output)
    assert decided["run_state"] == "succeeded"

    # 产物存在（写操作在批准后恰好一次）
    sandbox_file = cli_env / "sandbox" / "out" / "hello.txt"
    assert sandbox_file.read_text(encoding="utf-8") == "hello agentos"

    # task get 可回查全链路
    run_id = json.loads(
        runner.invoke(cli_app, ["task", "submit", "写一个文件", "--script", str(script), "--no-wait"]).output
    )["receipt"]["run_id"]
    r = runner.invoke(cli_app, ["task", "get", run_id])
    assert r.exit_code == 0, r.output
    task_payload = json.loads(r.output)
    assert task_payload["run"]["id"] == run_id
    assert isinstance(task_payload["events"], list)


def test_task_get_missing_exits_nonzero(cli_env: Path) -> None:
    runner.invoke(cli_app, ["boot"])
    result = runner.invoke(cli_app, ["task", "get", "run_missing"])
    assert result.exit_code == 2


def test_cancel_before_execution(cli_env: Path) -> None:
    runner.invoke(cli_app, ["boot"])
    script = _write_script(cli_env, IDEMPOTENT_APPEND_SCRIPT)
    r = runner.invoke(cli_app, ["task", "submit", "取消目标", "--script", str(script), "--no-wait"])
    assert r.exit_code == 0, r.output
    run_id = json.loads(r.output)["receipt"]["run_id"]
    r = runner.invoke(cli_app, ["task", "cancel", run_id])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["state"] == "cancelled"
    assert not (cli_env / "sandbox" / "task.log").exists()


def test_unbooted_submit_rejected(cli_env: Path) -> None:
    script = _write_script(cli_env, WRITE_FLOW_SCRIPT)
    # 不先 boot：身份文件缺失 → 拒绝
    result = runner.invoke(cli_app, ["task", "submit", "x", "--script", str(script)])
    assert result.exit_code == 1
    assert "not booted" in str(result.exception)
