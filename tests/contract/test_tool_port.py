"""Tool Port 契约：风险元数据、幂等键去重、沙箱约束、能力缺失显式报错。"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from agentos.adapters.tools.local import LocalToolAdapter
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import AuthContext, CallContext, utc_now


def ctx(call_id: str = "c1", key: str | None = None) -> CallContext:
    return CallContext(
        run_id="run_1", step_id="s1", call_id=call_id,
        actor=AuthContext(
            subject="owner", tenant_id="default", scopes=["read", "write"],
            policy_version="t", expires_at=utc_now() + timedelta(minutes=5),
        ),
        idempotency_key=key or "run_1:s1", fence=1,
        deadline_at=utc_now() + timedelta(seconds=30),
    )


@pytest.fixture
def tools(tmp_path: Path) -> LocalToolAdapter:
    return LocalToolAdapter(tmp_path / "sandbox")


async def test_math_add_completed(tools: LocalToolAdapter) -> None:
    result = await tools.call("math.add", {"a": 2, "b": 3}, ctx())
    assert result.status == "completed"
    assert result.value["result"] == 5


async def test_log_append_is_idempotent_per_key(tools: LocalToolAdapter, tmp_path: Path) -> None:
    """FR-ORC-08：同一步重试（同幂等键）只有一次已确认效果。"""
    first = await tools.call("log.append", {"message": "only-once"}, ctx(key="run_1:s1"))
    assert first.value["appended"] is True
    # 崩溃重试：同一幂等键再次调用
    second = await tools.call("log.append", {"message": "only-once"}, ctx(key="run_1:s1"))
    assert second.value["appended"] is False
    # 两次合法相同操作：不同幂等键，各自执行
    third = await tools.call("log.append", {"message": "second-legit"}, ctx(call_id="c2", key="run_1:s2"))
    assert third.value["appended"] is True
    log_file = tmp_path / "sandbox" / "task.log"
    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert lines == ["only-once", "second-legit"]


async def test_fs_write_is_idempotent_for_same_content(tools: LocalToolAdapter) -> None:
    args = {"path": "out/x.txt", "content": "same"}
    r1 = await tools.call("fs.write_text", args, ctx())
    assert r1.value["written"] is True
    r2 = await tools.call("fs.write_text", args, ctx())
    assert r2.value["written"] is False  # 幂等重放


async def test_sandbox_escape_rejected(tools: LocalToolAdapter) -> None:
    for bad_path in ("/etc/passwd", "../outside.txt", "a/../../../etc/passwd"):
        result = await tools.call("fs.write_text", {"path": bad_path, "content": "x"}, ctx())
        assert result.status == "failed"
        assert result.error_code == str(ErrorCode.INVALID)


async def test_unknown_tool_invalid(tools: LocalToolAdapter) -> None:
    with pytest.raises(AgentOSError) as exc_info:
        await tools.resolve(ctx().actor, "shell.exec")
    assert exc_info.value.code == ErrorCode.INVALID


async def test_poll_cancel_unsupported_capability(tools: LocalToolAdapter) -> None:
    for coro in (tools.poll("ref", ctx()), tools.cancel("ref", ctx())):
        with pytest.raises(AgentOSError) as exc_info:
            await coro
        assert exc_info.value.code == ErrorCode.UNSUPPORTED


async def test_tool_specs_declare_risks(tools: LocalToolAdapter) -> None:
    specs = await tools.list_tools(ctx().actor)
    by_name = {s.name: s for s in specs}
    assert by_name["math.add"].risks == ["read"]
    assert by_name["fs.write_text"].risks == ["write"]
    assert by_name["fs.write_text"].recovery == "idempotency_key"
    write_spec = by_name["fs.write_text"]
    assert write_spec.input_schema["additionalProperties"] is False
