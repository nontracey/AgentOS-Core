"""M8 OSS 发布检查（NFR-OSS）：凭据扫描 + 许可证台账 + 公开物安全。"""

from __future__ import annotations

import re
from pathlib import Path

# 常见凭据模式
_SECRET_PATTERNS = [
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),           # API credential style
    re.compile(r"ghp_[a-zA-Z0-9]{36}"),            # GitHub PAT
    re.compile(r"AKIA[0-9A-Z]{16}"),               # AWS Access Key
    re.compile(r"-----BEGIN (RSA |EC )?PRIVATE KEY-----"),
    re.compile(r"password\s*=\s*['\"][^'\"]{8,}['\"]", re.IGNORECASE),
]

# 显式合成配置示例
_ALLOWLIST = {".env.example"}


def oss_credential_scan(root: Path) -> list[dict[str, str]]:
    """扫描跟踪及非忽略的新文件，只报告位置，不输出秘密。"""
    import subprocess

    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"], capture_output=True, text=True, cwd=str(root)
    )
    suffixes = (".py", ".yaml", ".yml", ".toml", ".md", ".sh", ".js", ".ts", ".tsx", ".json", ".html", ".txt")
    tracked = [f for f in result.stdout.splitlines() if f.endswith(suffixes)]

    findings: list[dict[str, str]] = []
    for filepath in tracked:
        full = root / filepath
        if not full.is_file():
            continue
        if any(filepath.startswith(a) for a in _ALLOWLIST):
            continue
        content = full.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(content.splitlines(), 1):
            for pattern in _SECRET_PATTERNS:
                if pattern.search(line):
                    findings.append({
                        "file": filepath, "line": str(i),
                        "pattern": pattern.pattern[:30], "preview": "[REDACTED]",
                    })
    return findings


def oss_license_check(root: Path) -> list[str]:
    """Review the explicit core inventory; missing/unknown data never counts as approval.

    This is a core selection gate, not an exhaustive legal audit of every wheel,
    container layer, optional enterprise feature or third-party Skill.
    """
    import json

    path = root / "contracts/oss-licenses.json"
    if not path.exists():
        return ["UNKNOWN: core license inventory missing"]
    known = {"MIT", "Apache-2.0", "BSD-3-Clause", "BSD-2-Clause", "ISC", "Python-2.0"}
    issues = []
    for component in json.loads(path.read_text()).get("components", []):
        expression = component.get("license", "")
        atoms = set(re.findall(r"[A-Za-z0-9.-]+", expression)) - {"AND", "OR"}
        if not atoms or not atoms.issubset(known):
            issues.append(component["name"] + ": " + (expression or "UNKNOWN"))
    return sorted(issues)
