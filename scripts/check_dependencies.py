"""依赖方向检查（FR-ORC-01：kernel/runtime 到具体 Adapter 的反向引用为 0，CI 卡死）。

基于 AST 的静态检查，覆盖别名/延迟导入；与 import-linter 互为补充。
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "agentos"

FORBIDDEN: dict[str, frozenset[str]] = {
    "kernel": frozenset(
        {"agentos.runtime", "agentos.domains", "agentos.services", "agentos.adapters", "agentos.apps"}
    ),
    "runtime": frozenset({"agentos.adapters", "agentos.apps"}),
}

# 产品 SDK 禁止进入 kernel/runtime（NFR-PRT）；仅 adapters/apps 可用
PRODUCT_SDKS = frozenset(
    {"httpx", "fastapi", "uvicorn", "typer", "sqlalchemy", "redis", "celery", "openai", "anthropic"}
)


def _iter_py_files(base: Path) -> list[Path]:
    return sorted(base.rglob("*.py"))


def _imports_of(tree: ast.AST) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append(node.module)
    return found


def check() -> list[str]:
    violations: list[str] = []
    for layer, forbidden in FORBIDDEN.items():
        base = SRC / layer
        for path in _iter_py_files(base):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for imported in _imports_of(tree):
                for bad in forbidden:
                    if imported == bad or imported.startswith(bad + "."):
                        violations.append(f"{path.relative_to(REPO_ROOT)}: {layer} imports {imported}")
                root_module = imported.split(".")[0]
                if root_module in PRODUCT_SDKS:
                    violations.append(
                        f"{path.relative_to(REPO_ROOT)}: {layer} imports product SDK '{root_module}'"
                    )
    return violations


def main() -> int:
    violations = check()
    if violations:
        print("dependency direction violations:")
        for line in violations:
            print(f"  - {line}")
        return 1
    print("dependency direction OK (kernel/runtime independence)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
