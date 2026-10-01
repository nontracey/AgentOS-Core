"""公开契约 JSON Schema 导出（NFR-AID：schema 漂移由 CI 阻断）。

用法：
    uv run python scripts/export_contracts.py          # 重新导出
    uv run python scripts/export_contracts.py --check  # 校验无漂移（CI）

只导出公开 DTO 白名单；内部 AuthContext/Envelope.actor 等权限字段不进入客户端可填写面。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentos.kernel.decision import DecisionResult  # noqa: E402
from agentos.kernel.dto import (  # noqa: E402
    AppManifest,
    DispatchBinding,
    GoalSpec,
    PlannedTask,
    SkillManifest,
    TaskPlan,
)
from agentos.kernel.types import (
    ChatMessage,
    ModelRequest,
    Receipt,
    ToolResult,
    ToolSpec,
    Usage,
)  # noqa: E402
from agentos.kernel.workflow_graph import FlowGraph, FlowNode  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = REPO_ROOT / "contracts" / "schemas"

PUBLIC_MODELS = {
    "Receipt": Receipt,
    "FlowGraph": FlowGraph,
    "FlowNode": FlowNode,
    "ToolSpec": ToolSpec,
    "ToolResult": ToolResult,
    "ChatMessage": ChatMessage,
    "ModelRequest": ModelRequest,
    "Usage": Usage,
    "DecisionResult": DecisionResult,
    "GoalSpec": GoalSpec,
    "PlannedTask": PlannedTask,
    "TaskPlan": TaskPlan,
    "DispatchBinding": DispatchBinding,
    "SkillManifest": SkillManifest,
    "AppManifest": AppManifest,
}


def export_schemas() -> dict[str, str]:
    out: dict[str, str] = {}
    for name, model in sorted(PUBLIC_MODELS.items()):
        schema = model.model_json_schema()
        out[f"{name}.json"] = json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="校验已导出 schema 与当前模型一致")
    args = parser.parse_args()

    schemas = export_schemas()
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)

    drift: list[str] = []
    for filename, content in schemas.items():
        target = SCHEMA_DIR / filename
        if args.check:
            if not target.exists():
                drift.append(f"missing: {filename}")
            elif target.read_text(encoding="utf-8") != content:
                drift.append(f"drift: {filename}")
        else:
            target.write_text(content, encoding="utf-8")

    if args.check:
        if drift:
            print("schema drift detected; run scripts/export_contracts.py and commit:")
            for line in drift:
                print(f"  - {line}")
            return 1
        print(f"contract schemas up to date ({len(schemas)} files)")
        return 0

    print(f"exported {len(schemas)} schemas to {SCHEMA_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
