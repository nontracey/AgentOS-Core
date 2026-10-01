"""HTTP API 集成（蓝图 §8）：受理回执、幂等重放、409、取消、审批、越权。

TestClient 以 with 上下文使用以触发 lifespan（后台 worker）。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from fastapi.testclient import TestClient

from agentos.apps.composition import SystemHandle
from agentos.apps.gateway import create_app
from tests.conftest import WRITE_FLOW_SCRIPT, add_subject, owner_token


@contextmanager
def make_client(
    make_system: Callable[..., SystemHandle], **kw: Any
) -> Iterator[tuple[TestClient, SystemHandle, str]]:
    handle = make_system(model_script=kw.pop("script", WRITE_FLOW_SCRIPT), **kw)
    app = create_app(system=handle)
    with TestClient(app) as client:
        yield client, handle, owner_token(handle)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def wait_state(client: TestClient, token: str, run_id: str, states: set[str], timeout: float = 10) -> str:
    deadline = time.monotonic() + timeout
    state = ""
    while time.monotonic() < deadline:
        state = client.get(f"/v1/tasks/{run_id}", headers=auth(token)).json()["run"]["state"]
        if state in states:
            return state
        time.sleep(0.05)
    return state


def test_unauthenticated_rejected(make_system: Callable[..., SystemHandle]) -> None:
    with make_client(make_system) as (client, _, _):
        r = client.post("/v1/tasks", json={"payload": {"goal": "x"}})
        assert r.status_code == 401
        r = client.post("/v1/tasks", json={"payload": {"goal": "x"}}, headers=auth("forged"))
        assert r.status_code == 401


def test_submit_returns_202_receipt(make_system: Callable[..., SystemHandle]) -> None:
    with make_client(make_system) as (client, _, token):
        r = client.post(
            "/v1/tasks",
            json={"payload": {"goal": "demo"}, "idempotency_key": "k1"},
            headers=auth(token),
        )
        assert r.status_code == 202
        body = r.json()
        assert body["accepted"] is True and body["run_id"].startswith("run_")


def test_replay_and_conflict(make_system: Callable[..., SystemHandle]) -> None:
    with make_client(make_system) as (client, _, token):
        payload = {"payload": {"goal": "demo"}, "idempotency_key": "k1"}
        r1 = client.post("/v1/tasks", json=payload, headers=auth(token))
        r2 = client.post("/v1/tasks", json=payload, headers=auth(token))
        assert r2.json()["replayed"] is True
        assert r2.json()["run_id"] == r1.json()["run_id"]
        r3 = client.post(
            "/v1/tasks",
            json={"payload": {"goal": "other"}, "idempotency_key": "k1"},
            headers=auth(token),
        )
        assert r3.status_code == 409


def test_invalid_payload_422(make_system: Callable[..., SystemHandle]) -> None:
    with make_client(make_system) as (client, _, token):
        r = client.post("/v1/tasks", json={"payload": {}}, headers=auth(token))
        assert r.status_code == 422


def test_cross_subject_get_is_403(make_system: Callable[..., SystemHandle]) -> None:
    with make_client(make_system) as (client, handle, token):
        run_id = client.post(
            "/v1/tasks", json={"payload": {"goal": "demo"}}, headers=auth(token)
        ).json()["run_id"]
        helper_token = add_subject(handle, "helper", ["read"])
        r = client.get(f"/v1/tasks/{run_id}", headers=auth(helper_token))
        assert r.status_code == 403
        kinds = [e.kind for e in handle.deps.audit.query("default")]
        assert "task.cross_subject_denied" in kinds


def test_full_approval_loop_over_http(make_system: Callable[..., SystemHandle]) -> None:
    with make_client(make_system) as (client, handle, token):
        run_id = client.post(
            "/v1/tasks", json={"payload": {"goal": "write"}}, headers=auth(token)
        ).json()["run_id"]

        state = wait_state(client, token, run_id, {"awaiting_approval"})
        assert state == "awaiting_approval"

        approvals = client.get("/v1/approvals", headers=auth(token)).json()
        assert len(approvals) == 1
        approval_id = approvals[0]["id"]
        args_hash = approvals[0]["args_hash"]

        r = client.post(
            f"/v1/approvals/{approval_id}/decision",
            json={"decision": "approve"},
            headers=auth(token),
        )
        assert r.status_code == 200 and r.json()["state"] == "approved"

        state = wait_state(client, token, run_id, {"succeeded", "failed"})
        assert state == "succeeded"
        assert args_hash
        _ = handle


def test_events_cursor(make_system: Callable[..., SystemHandle]) -> None:
    with make_client(make_system) as (client, _, token):
        run_id = client.post(
            "/v1/tasks", json={"payload": {"goal": "demo"}}, headers=auth(token)
        ).json()["run_id"]
        wait_state(client, token, run_id, {"awaiting_approval", "succeeded", "failed"})
        page1 = client.get(f"/v1/tasks/{run_id}/events", headers=auth(token)).json()
        assert page1["events"], "expected at least run.created"
        if page1["cursor"] > 0:
            page2 = client.get(
                f"/v1/tasks/{run_id}/events",
                params={"cursor": page1["cursor"]},
                headers=auth(token),
            ).json()
            assert all(e["seq"] > page1["cursor"] for e in page2["events"])


def test_cancel_endpoint(make_system: Callable[..., SystemHandle]) -> None:
    with make_client(make_system) as (client, handle, token):
        run_id = client.post(
            "/v1/tasks", json={"payload": {"goal": "demo"}, "idempotency_key": "cancel-1"},
            headers=auth(token),
        ).json()["run_id"]
        state = wait_state(client, token, run_id, {"awaiting_approval"})
        assert state == "awaiting_approval"
        r = client.post(f"/v1/tasks/{run_id}/cancel", headers=auth(token))
        assert r.status_code == 202
        state = wait_state(client, token, run_id, {"cancelled"}, timeout=5)
        assert state == "cancelled"
        # 已审批但未消费的批准不产生副作用
        assert not (handle.settings.sandbox_dir / "out" / "hello.txt").exists()
