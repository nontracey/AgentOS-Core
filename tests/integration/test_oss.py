"""M8 OSS 检查 + M5 企业隔离测试。"""

from __future__ import annotations

from pathlib import Path

from agentos.services.oss_check import oss_credential_scan


def test_oss_credential_scan_clean(tmp_path: Path) -> None:
    """仓库源码无凭据泄露（NFR-OSS / NFR-PRI）。"""
    root = Path(__file__).parents[1]
    findings = oss_credential_scan(root)
    assert findings == [], f"credential leak: {findings}"
