"""U4-07 WeKnora 后端 fail-closed 守卫（真实服务未接入时的行为契约）。

- 未锁定版本/未配置 URL → 配置校验直接拒绝（不用浮动 latest，不静默退回内置）。
- 服务不可达 → UNAVAILABLE，不返回空结果冒充"没有命中"。
- 版本不一致 → INVALID（锁定版本被打破）。
- 端到端语义（ingest/search/resolve/delete 同 fixture）**未验证**，U4-07 保持 blocked。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from agentos.adapters.knowledge.weknora import WeKnoraConfig, WeKnoraKnowledgeAdapter
from agentos.apps.composition import SystemHandle
from agentos.apps.config import Settings
from agentos.kernel.errors import AgentOSError, ErrorCode
from tests.conftest import WRITE_FLOW_SCRIPT


def test_weknora_config_requires_pin_and_url() -> None:
    import pathlib

    s = Settings(data_dir=pathlib.Path("/tmp/u4-kn-a"), knowledge_backend="weknora")
    with pytest.raises(AgentOSError, match="base url"):
        s.validate()


def test_weknora_config_requires_pinned_version() -> None:
    import pathlib

    s = Settings(data_dir=pathlib.Path("/tmp/u4-kn-b"), knowledge_backend="weknora",
                 knowledge_weknora_url="http://127.0.0.1:9999")
    with pytest.raises(AgentOSError, match="pinned version"):
        s.validate()


def test_backend_selection_stays_fail_closed(make_system: Callable[..., SystemHandle]) -> None:
    handle = make_system(model_script=WRITE_FLOW_SCRIPT)
    assert handle.knowledge.backend_id == "builtin-sqlite"
    # 未知后端在组合根/配置层直接拒绝（不静默退回）
    settings = Settings(data_dir=handle.settings.data_dir, knowledge_backend="mystery")
    with pytest.raises(AgentOSError, match="unknown knowledge backend"):
        settings.validate()


def test_weknora_unverified_protocol_is_denied_before_any_remote_call() -> None:
    import pathlib
    import tempfile

    s = Settings(data_dir=pathlib.Path(tempfile.mkdtemp()) / "data", knowledge_backend="weknora",
                 knowledge_weknora_url="http://127.0.0.1:9",
                 knowledge_weknora_version="v9.9.9-pinned",
                 knowledge_weknora_kb_id="kb-test")
    s.validate()
    adapter = WeKnoraKnowledgeAdapter(WeKnoraConfig.from_settings(s))
    actor = __import__("agentos.kernel.types", fromlist=["AuthContext"]).AuthContext(
        subject="owner", tenant_id="default", scopes=["read"],
        policy_version="t", expires_at=__import__("agentos.kernel.types", fromlist=["utc_now"]).utc_now(),  # type: ignore[arg-type]
    )
    from datetime import timedelta

    actor.expires_at = actor.expires_at + timedelta(minutes=5)
    with pytest.raises(AgentOSError) as exc:
        asyncio.run(adapter.search(actor, "anything", 5))
    assert exc.value.code is ErrorCode.DENIED
    assert "unverified" in exc.value.message
