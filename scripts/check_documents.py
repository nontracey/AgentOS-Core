"""Check public Markdown links, document metadata and preserved identifiers."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"!?\[[^\]\n]*\]\(([^\)\n]+)\)")
FENCE = re.compile(r"^```[^\n]*\n.*?^```[^\n]*$", re.M | re.S)
REQUIREMENT = re.compile(r"\b(?:FR-[A-Z]+-\d+|NFR-[A-Z]+)\b")


def prose(path: Path) -> str:
    return FENCE.sub("", path.read_text(encoding="utf-8"))


def anchors(path: Path) -> set[str]:
    content = prose(path)
    found = set(re.findall(r"id=[\"']([^\"']+)", content))
    for heading in re.findall(r"^#{1,6}\s+(.+)$", content, re.M):
        found.add(re.sub(r"[^0-9A-Za-z一-鿿]+", "-", heading).strip("-").lower())
        plain = re.sub(r"[`*_]", "", heading).lower()
        found.add(re.sub(r"[^\w\-\s]", "", plain).replace(" ", "-"))
    return found


def check() -> dict[str, object]:
    # Inspect the public Git file set, not ignored local history/evidence. A local
    # private file must never make a broken link pass when a fresh clone lacks it.
    names = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT
    ).decode().split("\0")
    private_prefixes = ("docs/history/", "evidence/")
    errors = [f"private process files must not be tracked: {prefix} ({count})"
              for prefix in private_prefixes
              if (count := sum(name.startswith(prefix) for name in names))]
    public = {(ROOT / name).resolve() for name in names
              if name and not name.startswith(private_prefixes) and (ROOT / name).is_file()}
    files = sorted(path for path in public if path.suffix == ".md" and (
        path.parent == ROOT or path.is_relative_to(ROOT / "docs")
        or path == ROOT / "tests/fixtures/u4/README.md"))
    links = 0
    for path in files:
        for match in LINK.finditer(prose(path)):
            target = match[1]
            if re.match(r"\w+://|mailto:|data:", target):
                continue
            relative, _, fragment = target.partition("#")
            destination = (path.parent / unquote(relative)).resolve() if relative else path
            links += 1
            location = str(path.relative_to(ROOT))
            if destination not in public:
                errors.append(f"{location}: missing file {target}")
            elif (
                fragment
                and destination.is_file()
                and destination.suffix in {".md", ".html"}
                and unquote(fragment) not in anchors(destination)
            ):
                errors.append(f"{location}: missing anchor {target}")

    current = sorted(path for path in files if path.parent == ROOT / "docs")
    for path in current:
        content = path.read_text(encoding="utf-8")
        if len(re.findall(r"^# ", prose(path), re.M)) != 1:
            errors.append(f"{path.name}: expected one document title")
        for field in ("版本", "更新日期", "文档性质", "适用范围"):
            if not re.search(rf"^> {field}：", content, re.M):
                errors.append(f"{path.name}: missing metadata {field}")
        if sum(line.startswith("```") for line in content.splitlines()) % 2:
            errors.append(f"{path.name}: unclosed code fence")
        previous = 1
        for heading in re.findall(r"^(#{2,6})\s", prose(path), re.M):
            level = len(heading)
            if level > previous + 1:
                errors.append(f"{path.name}: skipped heading level")
            previous = level

    baseline = json.loads((ROOT / "contracts/document-identifiers.json").read_text())
    active = (ROOT / "docs/00-需求规格.md").read_text(encoding="utf-8")
    for label, key, pattern in (
        ("requirement", "requirement_ids", REQUIREMENT),
        ("source", "source_ids", re.compile(r"\bSRC-\d+\b")),
    ):
        missing = set(baseline[key]) - set(pattern.findall(active))
        if missing:
            errors.append(f"missing {label} IDs: {sorted(missing)}")
    active_adr = (ROOT / "docs/03-架构决策.md").read_text(encoding="utf-8")
    adr_pattern = re.compile(r"^## (ADR-\d+)", re.M)
    if not set(baseline["adr_ids"]).issubset(set(adr_pattern.findall(active_adr))):
        errors.append("historical ADR identifiers were removed")
    active_ids = adr_pattern.findall(active_adr)
    if len(active_ids) != len(set(active_ids)):
        errors.append("duplicate ADR identifiers")

    return {
        "markdown_files": len(files),
        "current_documents": len(current),
        "local_links": links,
        "requirement_ids": len(set(REQUIREMENT.findall(active))),
        "source_ids": len(set(re.findall(r"\bSRC-\d+\b", active))),
        "adr_ids": len(set(adr_pattern.findall(active_adr))),
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="Optional JSON report destination")
    args = parser.parse_args()
    result = check()
    output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.write_text(output, encoding="utf-8")
    print(output, end="")
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
