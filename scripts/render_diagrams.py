"""Render Markdown Mermaid sources with the pinned official CLI; check SVG drift offline."""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {
    "01-总体架构.md": "01",
    "modules/execution.md": "execution",
    "modules/capabilities.md": "capabilities",
}
CLI_VERSION = "12.0.0"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    count = 0
    for name, prefix in SOURCES.items():
        text = (ROOT / "docs" / name).read_text()
        for number, block in enumerate(re.findall(r"```mermaid\n(.*?)\n```", text, re.S), 1):
            target = ROOT / "docs" / "diagrams" / f"{prefix}-current-{number}.svg"
            marker = f"<!-- source-sha256: {hashlib.sha256(block.encode()).hexdigest()} -->"
            if args.check:
                if not target.is_file() or marker not in target.read_text():
                    print(f"Diagram missing/stale: {target.relative_to(ROOT)}")
                    return 1
            else:
                command = shlex.split(os.environ.get("MERMAID_CLI", "")) or [
                    "npx", "--yes", f"--package=@mermaid-js/mermaid-cli@{CLI_VERSION}", "mmdc"]
                with tempfile.TemporaryDirectory(prefix="agentos-mermaid-") as directory:
                    source = Path(directory) / "source.mmd"
                    source.write_text(block)
                    options = ["-i", str(source), "-o", str(target), "-b", "white", "--no-font-embed"]
                    config = os.environ.get("MERMAID_PUPPETEER_CONFIG")
                    if config:
                        options += ["-p", config]
                    subprocess.run(command + options, check=True, cwd=ROOT)
                target.write_text(target.read_text().replace("</svg>", marker + "</svg>"))
            count += 1
    print(f"{count} diagrams {'current' if args.check else 'rendered'} (Mermaid CLI {CLI_VERSION})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
