"""Verify a built image with isolated synthetic data; preserve that data for inspection.

Requires an existing Docker daemon. Never mounts deployment credentials, touches
existing containers, deletes volumes, or calls a real model.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

PROBE = r'''
import json, time, urllib.request, urllib.error, hashlib
from pathlib import Path
p = Path('/data')
token = json.loads((p / 'identity.json').read_text())['subjects']['owner']['token']
def request(route, body=None, authenticated=True):
    headers = {'Content-Type': 'application/json'}
    if authenticated: headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request('http://127.0.0.1:8000' + route,
        data=json.dumps(body).encode() if body is not None else None, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as response:
        data = response.read()
        return json.loads(data) if 'application/json' in response.headers.get('Content-Type', '') else data
for route in ('/healthz', '/ui/', '/ui/sw.js', '/ui/workflow-dist/editor.js', '/ui/workflow-dist/editor.css'):
    assert request(route, authenticated=False)
try:
    request('/v1/runs', authenticated=False)
    raise AssertionError('Unauthenticated access allowed')
except urllib.error.HTTPError as exc:
    assert exc.code == 401
assert request('/v1/knowledge/capabilities')['enabled'] is False
catalog = request('/v1/capabilities/catalog')['tools']
assert 'knw.search' not in {s['name'] for s in catalog}
assert request('/v1/workflows/catalog')['engine_status'] == 'disabled'
payload = {'payload': {'goal': 'synthetic container persistence acceptance'},
           'idempotency_key': 'container-acceptance-v1'}
receipt = request('/v1/tasks', payload)
run_id = receipt['run_id']
deadline = time.monotonic() + 45
while time.monotonic() < deadline:
    detail = request('/v1/tasks/' + run_id)
    if detail['run']['state'] in ('awaiting_approval', 'succeeded', 'failed'): break
    time.sleep(.2)
expected = {'path': 'acceptance.txt', 'content': 'synthetic container persistence'}
expected_hash = hashlib.sha256(json.dumps(expected, sort_keys=True, separators=(',', ':'),
                                        ensure_ascii=False).encode()).hexdigest()
if not receipt['replayed']:
    assert detail['run']['state'] == 'awaiting_approval'
    step = next(s for s in detail['steps'] if s['tool_name'] == 'fs.write_text')
    assert step['args_hash'] == expected_hash
    approval = next(a for a in detail['approvals'] if a['call_id'].endswith(':' + step['id']))
    request('/v1/approvals/' + approval['id'] + '/decision', {'decision': 'approve'})
    while time.monotonic() < deadline:
        detail = request('/v1/tasks/' + run_id)
        if detail['run']['state'] in ('succeeded', 'failed'): break
        time.sleep(.2)
assert detail['run']['state'] == 'succeeded'
assert len(detail['steps']) == 1 and detail['steps'][0]['state'] == 'completed'
files = list((p / 'sandbox').rglob('acceptance.txt'))
assert len(files) == 1 and files[0].read_text() == expected['content']
marker = p / 'acceptance-receipt.json'
saved = {'run_id': run_id, 'identity_hash': hashlib.sha256(token.encode()).hexdigest()}
if receipt['replayed']:
    assert json.loads(marker.read_text()) == saved
else:
    marker.write_text(json.dumps(saved))
print(json.dumps({'ui': True, 'authentication': True, 'knowledge_disabled': True,
                  'approved_effect': True, 'identity_retained': True,
                  'replayed_after_recreate': receipt['replayed']}))
'''


def docker(*args: str, input_text: str | None = None, timeout: int = 90) -> str:
    result = subprocess.run(["docker", *args], input=input_text, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # Daemon logs and inspect output can include private host configuration.
        raise RuntimeError("Docker acceptance command failed; diagnostics withheld")
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", help="Already built image tag or digest")
    parser.add_argument("--state-dir", required=True, type=Path, help="New empty directory; retained after the check")
    args = parser.parse_args()
    state = args.state_dir.resolve()
    if state.exists() and any(state.iterdir()):
        parser.error("state directory must be empty; existing data is never used")
    repository = next((parent for parent in (state, *state.parents) if (parent / ".git").exists()), None)
    if repository:
        try:
            ignored = subprocess.run(["git", "check-ignore", "--quiet", str(state) + "/"],
                                     cwd=repository, capture_output=True, check=False)
        except OSError:
            parser.error("Git is required to verify that acceptance state is ignored")
        if ignored.returncode:
            parser.error("state within a repository must be Git ignored; no credentials were generated")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    (state / "model_script.json").write_text(json.dumps([
        {"tool_calls": [{"id": "acceptance-write", "name": "fs.write_text", "arguments": {
            "path": "acceptance.txt", "content": "synthetic container persistence"}}]},
        {"content": "synthetic acceptance complete"},
    ]))
    name = "agentos-check-" + uuid.uuid4().hex[:12]
    # Dropped capabilities must not rely on root bypassing the bind directory's
    # owner-only permissions. Docker Desktop also maps the host user's files.
    user = f"{getattr(os, 'getuid', lambda: 0)()}:{getattr(os, 'getgid', lambda: 0)()}"
    created = False
    stage = "create"
    try:
        for recreated in (False, True):
            stage = "recreate" if recreated else "create"
            docker("run", "-d", "--name", name, "--network", "none", "--user", user,
                   "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                   "--memory", "512m", "--cpus", "1", "-e", "AGENTOS_KNOWLEDGE_BACKEND=disabled",
                   "-e", "AGENTOS_MODEL_PROVIDER=scripted", "-e", "AGENTOS_TEMPORAL_ADDRESS=",
                   "--mount", f"type=bind,src={state},dst=/data", args.image)
            created = True
            stage = "health"
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try:
                    docker("exec", name, "python", "-c",
                           "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=2)",
                           timeout=5)
                    break
                except (RuntimeError, subprocess.TimeoutExpired):
                    time.sleep(.5)
            else:
                raise RuntimeError("Container did not become healthy")
            stage = "node-runtime"
            node = docker("exec", name, "node", "--version")
            stage = "codex-runtime"
            codex = docker("exec", name, "codex", "--version")
            assert node.startswith("v22.") and codex == "codex-cli 0.159.2"
            stage = "governance-and-persistence"
            result = json.loads(docker("exec", "-i", name, "python", "-", input_text=PROBE))
            assert result["replayed_after_recreate"] is recreated
            print(json.dumps({"recreated": recreated, **result}), flush=True)
            docker("stop", "--time", "10", name)
            docker("rm", name)
            created = False
        print("Isolated container acceptance passed; synthetic state retained, no volumes deleted")
        return 0
    except (RuntimeError, AssertionError, subprocess.TimeoutExpired, OSError):
        print(f"Container acceptance failed at {stage}; private diagnostics withheld")
        return 1
    finally:
        if created:
            subprocess.run(["docker", "stop", "--time", "10", name], capture_output=True, timeout=30)
            subprocess.run(["docker", "rm", name], capture_output=True, timeout=30)


if __name__ == "__main__":
    raise SystemExit(main())
