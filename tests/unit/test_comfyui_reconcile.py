"""ComfyUI 适配器回归：SaveVideo 的 animated 布尔项不再崩溃（S05 根因）；
_find_history 按 extra_data.agentos_idem 回查（0.37 history 结构）。

S05 实证（2026-09-28）：0.37 SaveVideo 的 history outputs 同时含
images:[文件dict] 与 animated:[True]，旧代码对布尔值调 .get() → AttributeError
→ 节点 /execute 500 → 步骤 uncertain。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentos.adapters.tools import comfyui as comfyui_mod
from agentos.adapters.tools.comfyui import ComfyUIToolAdapter


def _adapter(tmp_path: Path) -> ComfyUIToolAdapter:
    return ComfyUIToolAdapter(
        base_url="http://127.0.0.1:1",  # 不触网；仅测纯函数
        workflows_dir=tmp_path / "wf", sandbox_dir=tmp_path / "sb",
    )


def test_collect_images_skips_animated_boolean_flags(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    outputs: dict[str, Any] = {
        "21": {
            "images": [{"filename": "wan22_video_00003_.mp4", "subfolder": "",
                        "type": "output"}],
            "animated": [True],  # 0.37 SaveVideo 的布尔标志，不是文件项
        }
    }
    found = adapter._collect_images(outputs)
    assert found == [{"filename": "wan22_video_00003_.mp4", "subfolder": "",
                      "type": "output"}]


def test_collect_images_handles_non_dict_and_static_png(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    outputs = {"9": {"images": [{"filename": "z.png", "subfolder": "", "type": "output"},
                                "garbage", None]}}
    assert adapter._collect_images(outputs) == [
        {"filename": "z.png", "subfolder": "", "type": "output"}]


def test_find_history_matches_agentos_idem_in_extra_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    history = {
        "pid-1": {"prompt": [1, {}, {"client_id": "x", "agentos_idem": "agentos-run1:s1"}],
                  "status": {"status_str": "success", "completed": True}},
        "pid-2": {"prompt": [2, {}, {"client_id": "x"}]},
    }
    captured: dict[str, Any] = {}

    def fake_http(method: str, url: str, body: Any = None, timeout: float = 10) -> Any:
        captured["url"] = url
        return history

    monkeypatch.setattr(comfyui_mod, "_http", fake_http)
    assert adapter._find_history("agentos-run1:s1") == "pid-1"
    assert adapter._find_history("agentos-missing") is None
    # 0.37 的 /history 不带 client_id 过滤参数（回查在本地做）
    assert captured["url"].endswith("/history")


async def _call(name: str, tmp_path: Path) -> dict[str, Any]:
    """异步小助手：驱动适配器 call() 取回执信封。"""

    adapter = _adapter(tmp_path)
    from datetime import timedelta

    from agentos.kernel.types import AuthContext, CallContext, utc_now

    ctx = CallContext(
        run_id="r", step_id="s", call_id="c",
        actor=AuthContext(subject="t", tenant_id="default", scopes=["read", "write"],
                          policy_version="t", expires_at=utc_now() + timedelta(hours=1)),
        idempotency_key="k", fence=0,
        deadline_at=utc_now() + timedelta(seconds=30),
    )
    return await adapter.call(name, {}, ctx)


def test_stop_and_status_return_standard_envelope(tmp_path: Path) -> None:
    """stop/status 必须返回 {status: completed, value: {...}} 标准信封。

    D 盲跑第3轮根因：stop 返回裸 dict（无 status 键），节点 agent 以
    ToolResult(call_id, **value) 包装时 ValidationError → /execute 500 →
    网关侧 UNAVAILABLE → 步骤误转 uncertain（comfyui.stop 从未成功过）。
    status 同理：其裸 dict 的 status 值 running/stopped 不在 ToolResult 枚举内。
    """
    import asyncio

    out = asyncio.run(_call("comfyui.stop", tmp_path))
    assert out["status"] == "completed"
    assert isinstance(out["value"], dict) and "stopped" in out["value"]

    out = asyncio.run(_call("comfyui.status", tmp_path))
    assert out["status"] == "completed"
    # base_url 不可达 → value.status 是适配器原始状态 "stopped"，信封 status 恒 completed
    assert out["value"]["status"] == "stopped"

    # 信封必须能被节点 agent 的包装路径接受（回归验证到 ToolResult 构造）
    from agentos.kernel.types import ToolResult

    for out in (asyncio.run(_call("comfyui.stop", tmp_path)),
                asyncio.run(_call("comfyui.status", tmp_path))):
        ToolResult(call_id="x", **out)


async def _call(name: str, tmp_path: Path) -> dict[str, Any]:
    """异步小助手：驱动适配器 call() 取回执信封。"""

    adapter = _adapter(tmp_path)
    from datetime import timedelta

    from agentos.kernel.types import AuthContext, CallContext, utc_now

    ctx = CallContext(
        run_id="r", step_id="s", call_id="c",
        actor=AuthContext(subject="t", tenant_id="default", scopes=["read", "write"],
                          policy_version="t", expires_at=utc_now() + timedelta(hours=1)),
        idempotency_key="k", fence=0,
        deadline_at=utc_now() + timedelta(seconds=30),
    )
    return await adapter.call(name, {}, ctx)
