"""Scan tracked and new public files with pinned Gitleaks, without printing secrets."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "8.30.1"


def main() -> int:
    binary = os.environ.get("GITLEAKS_BIN") or shutil.which("gitleaks")
    if not binary:
        print(f"Gitleaks {VERSION} required; see docs/09-集成部署.md")
        return 2
    version = subprocess.run([binary, "version"], capture_output=True, text=True, check=False)
    if version.returncode or version.stdout.strip() != VERSION:
        print("Gitleaks version differs from reviewed pin")
        return 2
    names = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT
    ).decode().split("\0")
    with tempfile.TemporaryDirectory(prefix="agentos-public-scan-") as directory:
        snapshot = Path(directory) / "public"
        snapshot.mkdir()
        for name in names:
            path = ROOT / name
            if not name or not path.is_file():
                continue
            if path.is_symlink():
                print("Public symlink requires manual review: " + name)
                return 2
            target = snapshot / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        report = Path(directory) / "report.json"
        result = subprocess.run([binary, "dir", str(snapshot), "--redact=100", "--no-banner",
            "--log-level", "error", "--config", str(ROOT / ".gitleaks.toml"), "--report-format", "json", "--report-path", str(report)],
            capture_output=True, text=True, check=False)
        if result.returncode not in (0, 1) or not report.exists():
            print("Gitleaks scan failed; diagnostic output withheld")
            return 2
        findings = json.loads(report.read_text())
        safe = [{"file": item["File"].removeprefix(str(snapshot) + "/"),
                 "line": item["StartLine"], "rule": item["RuleID"]} for item in findings]
        print(json.dumps({"files": len([n for n in names if n]), "findings": safe}, ensure_ascii=False))
        return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
