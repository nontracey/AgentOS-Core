"""In-process workflow engine: same flow semantics and ledger as Temporal, without a server.

Scripted tools and custom nodes only; this is not evidence for long timers or multi-process use.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient

from agentos.apps.composition import SystemHandle
from agentos.apps.gateway import create_app
from tests.conftest import owner_token
from tests.integration.test_n3a_template import auth, wait_state


def start_flow(c: TestClient, token: str, graph: dict[str, Any], key: str) -> str:
    published = c.post(f"/v1/workflows/{graph['id']}/publish", headers=auth(token), json=graph)
    assert published.status_code == 201, published.text
    version = published.json()["version"]
    started = c.post(f"/v1/workflows/{graph['id']}/start", headers=auth(token),
                     json={"version": version, "inputs": {}, "idempotency_key": key})
    assert started.status_code == 202, started.text
    return str(started.json()["internal_run_id"])


def outputs(handle: SystemHandle, run_id: str) -> dict[str, Any]:
    run = handle.store.get_run("default", run_id)
    assert run is not None
    return {k: v.get("output") for k, v in json.loads(run.checkpoint_json or "{}").get("workflow_nodes", {}).items()}


def test_dependencies_branches_and_custom_nodes(make_system: Callable[..., SystemHandle]) -> None:
    handle = make_system(workflow_engine="local")
    calls: list[str] = []

    async def record(inputs: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        calls.append(str(inputs.get("label")))
        return {"seen": inputs}

    handle.workflow_driver.custom["record"] = record
    graph = {"id": "local-basic", "name": "local", "nodes": [
        {"id": "add", "kind": "tool", "config": {"tool": "math.add", "arguments": {"a": 2, "b": 3}}},
        {"id": "check", "kind": "condition", "depends_on": ["add"],
         "config": {"path": "nodes.add.result", "operator": "eq", "value": 5}},
        {"id": "yes", "kind": "custom", "depends_on": ["add", "check"], "when": {"check": True},
         "config": {"adapter": "record", "inputs": {"label": "yes", "sum": {"$ref": "nodes.add"}}}},
        {"id": "no", "kind": "custom", "depends_on": ["check"], "when": {"check": False},
         "config": {"adapter": "record", "inputs": {"label": "no"}}},
        {"id": "after_no", "kind": "custom", "depends_on": ["no"], "config": {"adapter": "record",
                                                                             "inputs": {"label": "after"}}},
    ]}
    with TestClient(create_app(system=handle)) as c:
        token = owner_token(handle)
        run_id = start_flow(c, token, graph, "basic")
        assert wait_state(c, token, run_id, {"succeeded", "failed"}) == "succeeded"
        done = outputs(handle, run_id)
        assert done["check"] is True and calls == ["yes"]
        assert done["yes"]["seen"]["sum"]["result"] == 5
        assert "no" not in done and "after_no" not in done
        events = [e["kind"] for e in c.get(f"/v1/tasks/{run_id}/trace", headers=auth(token)).json()["events"]]
        assert "workflow.node_completed" in events
        assert handle.store.get_run("default", run_id).state.value == "succeeded"  # type: ignore[union-attr]


def test_failed_node_settles_run(make_system: Callable[..., SystemHandle]) -> None:
    handle = make_system(workflow_engine="local")

    async def broken(inputs: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        from agentos.kernel.errors import AgentOSError, ErrorCode

        raise AgentOSError(ErrorCode.INVALID, "synthetic failure")

    handle.workflow_driver.custom["broken"] = broken
    graph = {"id": "local-fail", "nodes": [{"id": "x", "kind": "custom", "config": {"adapter": "broken"}},
                                           {"id": "y", "kind": "custom", "depends_on": ["x"],
                                            "config": {"adapter": "broken"}}]}
    with TestClient(create_app(system=handle)) as c:
        token = owner_token(handle)
        run_id = start_flow(c, token, graph, "fail")
        assert wait_state(c, token, run_id, {"succeeded", "failed"}) == "failed"


def test_approval_blocks_then_signal_resumes(make_system: Callable[..., SystemHandle]) -> None:
    handle = make_system(workflow_engine="local")
    graph = {"id": "local-approval", "nodes": [
        {"id": "save", "kind": "tool", "config": {"tool": "fs.write_text",
                                                  "arguments": {"path": "flow.txt", "content": "ok"}}}]}
    with TestClient(create_app(system=handle)) as c:
        token = owner_token(handle)
        run_id = start_flow(c, token, graph, "approval")
        assert wait_state(c, token, run_id, {"awaiting_approval", "failed", "succeeded"}) == "awaiting_approval"
        approvals = c.get(f"/v1/tasks/{run_id}", headers=auth(token)).json()["approvals"]
        assert approvals and not (handle.settings.sandbox_dir / "flow.txt").exists()
        decided = c.post(f"/v1/approvals/{approvals[0]['id']}/decision", headers=auth(token),
                         json={"decision": "approve"})
        assert decided.status_code == 200
        assert wait_state(c, token, run_id, {"succeeded", "failed"}) == "succeeded"
        assert (handle.settings.sandbox_dir / "flow.txt").read_text() == "ok"


def test_restart_resumes_unfinished_flow_without_repeating_done_nodes(
        make_system: Callable[..., SystemHandle]) -> None:
    executed: list[str] = []
    gate = {"open": False}

    async def step(inputs: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        executed.append(str(inputs["name"]))
        while inputs["name"] == "second" and not gate["open"]:
            await asyncio.sleep(0.02)
        return {"name": inputs["name"]}

    graph = {"id": "local-resume", "nodes": [
        {"id": "first", "kind": "custom", "config": {"adapter": "step", "inputs": {"name": "first"}}},
        {"id": "second", "kind": "custom", "depends_on": ["first"],
         "config": {"adapter": "step", "inputs": {"name": "second"}}}]}
    first = make_system(workflow_engine="local")
    first.workflow_driver.custom["step"] = step
    with TestClient(create_app(system=first)) as c:
        token = owner_token(first)
        run_id = start_flow(c, token, graph, "resume")
        deadline = time.monotonic() + 10
        while executed != ["first", "second"] and time.monotonic() < deadline:
            time.sleep(0.02)
        assert executed == ["first", "second"]
    # The process stopped while "second" was running; a new process picks the flow up again.
    gate["open"] = True
    second = make_system(workflow_engine="local")
    second.workflow_driver.custom["step"] = step
    assert [run.id for run in second.store.list_engine_runs("local")] == [run_id]
    with TestClient(create_app(system=second)) as c:
        token = owner_token(second)
        assert wait_state(c, token, run_id, {"succeeded", "failed"}) == "succeeded"
    assert executed == ["first", "second", "second"]
    assert outputs(second, run_id) == {"first": {"name": "first"}, "second": {"name": "second"}}
