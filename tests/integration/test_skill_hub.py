"""SkillHub 测试（FR-SKL-01/02/03）：安装/渐进加载/版本/回滚/安全边界。"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from agentos.adapters.store.sqlite import SqliteStore
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import AuthContext, utc_now
from agentos.services.skills import SkillService

SKILL_MD = """---
name: report-writer
description: 生成结构化报告的技能
version: 1.2.0
allowed_tools: math.add, fs.write_text
entrypoints: report.md
---

# 报告写作

按以下结构输出：背景 / 数据 / 结论。
"""


def actor() -> AuthContext:
    return AuthContext(
        subject="owner", tenant_id="default", scopes=["read", "write", "approve"],
        policy_version="t", expires_at=utc_now() + timedelta(minutes=10),
    )


@pytest.fixture
def skill_pkg(tmp_path: Path) -> Path:
    pkg = tmp_path / "report-writer-src"
    pkg.mkdir()
    (pkg / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    (pkg / "templates").mkdir()
    (pkg / "templates" / "daily.md").write_text("# 日报模板", encoding="utf-8")
    return pkg


@pytest.fixture
def svc(tmp_path: Path) -> SkillService:
    store = SqliteStore(tmp_path / "s.db")
    return SkillService(store, tmp_path / "skills")


def test_install_parses_core_fields_and_digest(skill_pkg: Path, svc: SkillService) -> None:
    meta = svc.install(actor(), str(skill_pkg))
    assert meta.id == "report-writer"
    assert meta.version == "1.2.0"
    assert meta.digest
    assert meta.allowed_tools == ["math.add", "fs.write_text"]
    # 阶段 1：列表只有元数据
    metas = svc.list(actor())
    assert len(metas) == 1 and metas[0]["id"] == "report-writer"
    assert "正文" not in json_dumps_safe(metas)


def json_dumps_safe(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def test_progressive_loading(skill_pkg: Path, svc: SkillService) -> None:
    svc.install(actor(), str(skill_pkg))
    body = svc.load_body(actor(), "report-writer")
    assert "# 报告写作" in body
    assert "allowed_tools" not in body  # frontmatter 不混入正文
    asset = svc.read_asset(actor(), "report-writer", "templates/daily.md")
    assert asset == "# 日报模板".encode()


def test_asset_traversal_rejected(skill_pkg: Path, svc: SkillService) -> None:
    svc.install(actor(), str(skill_pkg))
    for bad in ("../../etc/passwd", "/etc/passwd", "templates/../../x"):
        with pytest.raises(AgentOSError) as exc_info:
            svc.read_asset(actor(), "report-writer", bad)
        assert exc_info.value.code == ErrorCode.INVALID


def test_install_without_skill_md_rejected(tmp_path: Path, svc: SkillService) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(AgentOSError) as exc_info:
        svc.install(actor(), str(empty))
    assert exc_info.value.code == ErrorCode.INVALID


def test_remove_with_rollback_content(skill_pkg: Path, svc: SkillService) -> None:
    meta = svc.install(actor(), str(skill_pkg))
    assert svc.remove(actor(), meta.id) == 1
    assert svc.list(actor()) == []
    with pytest.raises(AgentOSError):
        svc.load_body(actor(), meta.id)


def test_same_content_reinstall_keeps_version(skill_pkg: Path, svc: SkillService) -> None:
    svc.install(actor(), str(skill_pkg))
    meta2 = svc.install(actor(), str(skill_pkg))
    assert meta2.version == "1.2.0"  # 同内容同版本（digest 幂等）
    assert len(svc.list(actor())) == 1
