"""Install the built wheel in an isolated environment and verify packaged UI/Skill."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--offline", action="store_true", help="Use uv's existing dependency cache")
    args = parser.parse_args()
    wheel = args.wheel.resolve()
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        required = {
            "agentos/web/index.html", "agentos/web/sw.js", "agentos/web/js/main.js",
            "agentos/web/workflow-dist/editor.js", "agentos/web/workflow-dist/editor.css",
            "agentos/resources/material-report/SKILL.md",
        }
        if not required.issubset(names):
            raise SystemExit("Wheel is missing required public resources")
        if any(any(part in {"node_modules", ".git", "evidence", "history", ".env"}
                   for part in Path(name).parts) or name.endswith(("runtime.json", "node_token.txt"))
               for name in names):
            raise SystemExit("Wheel contains a prohibited deployment/development file")
    with tempfile.TemporaryDirectory(prefix="agentos-wheel-check-") as raw:
        work = Path(raw)
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment = {k: v for k, v in environment.items()
                       if not k.startswith(("AGENTOS_", "UV_PROJECT_", "ENTERPRISE_"))}
        subprocess.run(["uv", "venv", "--python", sys.executable, str(work / "venv")],
                       check=True, capture_output=True, env=environment)
        python = work / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        requirements = work / "requirements.txt"
        subprocess.run(["uv", "export", "--frozen", "--no-dev", "--no-emit-project",
                        "--format", "requirements-txt", "--output-file", str(requirements)],
                       cwd=ROOT, check=True, capture_output=True, env=environment)
        command = ["uv", "pip", "install", "--python", str(python)]
        if args.offline:
            command.append("--offline")
        install = subprocess.run(command + ["--requirement", str(requirements), str(wheel)],
                                 capture_output=True, env=environment)
        if install.returncode:
            print("Isolated wheel dependency installation failed; diagnostics withheld")
            return 1
        probe = '''
from pathlib import Path
from tempfile import TemporaryDirectory
from fastapi.testclient import TestClient
import agentos
from agentos.apps.composition import build_system
from agentos.apps.config import Settings
from agentos.apps.gateway import create_app
assert Path(agentos.__file__).resolve().is_relative_to(Path("venv").resolve())
with TemporaryDirectory() as data:
    system = build_system(Settings(data_dir=Path(data)))
    with TestClient(create_app(system=system)) as client:
        for route in ("/healthz", "/ui/", "/ui/js/main.js", "/ui/sw.js",
                      "/ui/workflow-dist/editor.js", "/ui/workflow-dist/editor.css"):
            response = client.get(route)
            assert response.status_code == 200, route
        response = client.get("/ui/js/main.js")
        assert "javascript" in response.headers["content-type"]
    assert (Path(agentos.__file__).parent / "resources/material-report/SKILL.md").is_file()
print("Isolated installed wheel: UI, editor, health and builtin Skill passed (synthetic)")
'''
        result = subprocess.run([str(python), "-c", probe], cwd=work, env=environment,
                                capture_output=True, text=True)
        if result.returncode:
            print("Isolated installed-wheel probe failed; private diagnostics withheld")
            return 1
        print(result.stdout.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
