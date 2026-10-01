"""共享 fixtures：临时数据目录 + 组合根 + 常用脚本化模型流程。"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from agentos.apps.composition import SystemHandle, build_system
from agentos.apps.config import Settings

# 测试进程内禁用本机代理（本机 Clash/SOCKS 会让 httpx 契约用例失败）：
# 这是环境差异不是代码行为，统一在此隔离，任何开发机/CI 结果一致。
for _proxy_var in (
    "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy",
    "HTTPS_PROXY", "https_proxy",
):
    os.environ.pop(_proxy_var, None)
os.environ.setdefault("NO_PROXY", "*")


# 标准三步流程：只读工具 → 写工具（触发审批）→ 最终答复
WRITE_FLOW_SCRIPT: list[dict[str, object]] = [
    {"tool_calls": [{"id": "c1", "name": "math.add", "arguments": {"a": 2, "b": 3}}]},
    {
        "tool_calls": [
            {
                "id": "c2",
                "name": "fs.write_text",
                "arguments": {"path": "out/hello.txt", "content": "hello agentos"},
            }
        ]
    },
    {"content": "done: sum=5 and file written"},
]

IDEMPOTENT_APPEND_SCRIPT: list[dict[str, object]] = [
    {
        "tool_calls": [
            {"id": "c1", "name": "log.append", "arguments": {"message": "only-once"}}
        ]
    },
    {"content": "appended"},
]


def make_settings(data_dir: Path, **overrides: object) -> Settings:
    settings = Settings(data_dir=data_dir)
    for key, value in overrides.items():
        if key.startswith("runtime_"):
            setattr(settings.runtime, key.removeprefix("runtime_"), value)
        else:
            setattr(settings, key, value)
    settings.validate()
    return settings


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "agentos-data"


@pytest.fixture
def settings(data_dir: Path) -> Settings:
    return make_settings(data_dir)


@pytest.fixture
def make_system(data_dir: Path) -> Callable[..., SystemHandle]:
    def _make(**overrides: object) -> SystemHandle:
        script = overrides.pop("model_script", None)
        s = make_settings(data_dir, **overrides)
        return build_system(s, model_script=script)  # type: ignore[arg-type]

    return _make


@pytest.fixture
def system(make_system: Callable[..., SystemHandle]) -> SystemHandle:
    handle = make_system(model_script=WRITE_FLOW_SCRIPT)
    handle.boot()
    return handle


def owner_token(handle: SystemHandle) -> str:
    return json.loads(handle.settings.identity_file.read_text(encoding="utf-8"))["subjects"]["owner"][
        "token"
    ]


def add_subject(handle: SystemHandle, subject: str, scopes: list[str]) -> str:
    """在身份文件中追加测试主体（模拟受限身份/撤权演练）。"""
    path = handle.settings.identity_file
    data = json.loads(path.read_text(encoding="utf-8"))
    import secrets

    token = secrets.token_urlsafe(16)
    data["subjects"][subject] = {"token": token, "scopes": scopes, "tenant_id": "default"}
    path.write_text(json.dumps(data), encoding="utf-8")
    return token
