"""Owner-issued tokens for external agents: own subject, scoped, expiring, revocable, never approving."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import timedelta

from fastapi.testclient import TestClient

from agentos.apps.composition import SystemHandle
from agentos.apps.gateway import create_app
from agentos.apps.mcp_server import PlatformTokenVerifier
from agentos.kernel.types import utc_now
from tests.conftest import owner_token
from tests.integration.test_grants import WRITE
from tests.integration.test_n3a_template import auth, wait_state

DENIED = (401, 403)


def test_agent_token_is_scoped_hashed_and_never_approves(make_system: Callable[..., SystemHandle]) -> None:
    handle = make_system(model_script=WRITE * 2)
    with TestClient(create_app(system=handle)) as c:
        owner = owner_token(handle)
        issued = c.post("/v1/agent-tokens", headers=auth(owner), json={"label": "编码助手", "days": 7})
        assert issued.status_code == 201, issued.text
        body = issued.json()
        token = body["token"]
        assert token.startswith("aos_agent_") and body["scopes"] == ["read", "write"] and body["active"]
        listed = c.get("/v1/agent-tokens", headers=auth(owner)).json()
        assert listed["mcp_url"].endswith("/mcp/") and [t["id"] for t in listed["tokens"]] == [body["id"]]
        stored = handle.settings.identity_file.read_text(encoding="utf-8")
        assert token not in stored and token not in json.dumps(listed)

        # The agent is its own member subject: it can submit work but not decide approvals.
        rid = c.post("/v1/tasks", headers=auth(token), json={"payload": {"goal": "write"}}).json()["run_id"]
        assert wait_state(c, token, rid, {"succeeded", "awaiting_approval", "failed"}) == "awaiting_approval"
        approval = c.get(f"/v1/tasks/{rid}", headers=auth(token)).json()["approvals"][0]
        refused = c.post(f"/v1/approvals/{approval['id']}/decision", headers=auth(token), json={"decision": "approve"})
        assert refused.status_code in DENIED
        pending = c.get("/v1/approvals", headers=auth(owner)).json()
        assert approval["id"] in {p["id"] for p in pending}   # the owner sees and decides it
        decided = c.post(f"/v1/approvals/{approval['id']}/decision", headers=auth(owner), json={"decision": "approve"})
        assert decided.status_code == 200, decided.text
        assert wait_state(c, token, rid, {"succeeded", "failed"}) == "succeeded"

        # No owner powers: tokens, grants, nodes and shared knowledge stay with the owner.
        for method, url, payload in [
            ("get", "/v1/agent-tokens", None),
            ("post", "/v1/agent-tokens", {"label": "x"}),
            ("post", "/v1/grants", {"tool_name": "fs.write_text", "hours": 1}),
            ("post", "/v1/nodes/pair", {"node_id": "n1", "endpoint": "http://node.invalid"}),
            ("delete", "/v1/knowledge/sources/any", None),
        ]:
            response = c.request(method.upper(), url, headers=auth(token), json=payload)
            assert response.status_code in DENIED, (url, response.status_code)

        verified = asyncio.run(PlatformTokenVerifier(handle, listed["mcp_url"]).verify_token(token))
        assert verified is not None and verified.client_id == "agent:" + body["id"]
        with TestClient(create_app(system=handle), base_url="http://127.0.0.1:8000") as local:
            mcp = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
            listing = local.post("/mcp/", headers=mcp, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert listing.status_code == 200, listing.text
            assert "task_submit" in {t["name"] for t in listing.json()["result"]["tools"]}

        revoked = c.post(f"/v1/agent-tokens/{body['id']}/revoke", headers=auth(owner))
        assert revoked.status_code == 200 and revoked.json()["active"] is False
        assert c.get("/v1/runs", headers=auth(token)).status_code in DENIED
        assert asyncio.run(PlatformTokenVerifier(handle, listed["mcp_url"]).verify_token(token)) is None
        kinds = [r["kind"] for r in handle.store._conn.execute(  # type: ignore[attr-defined]
            "SELECT kind, detail_json FROM audit_log WHERE kind LIKE 'identity.agent_token_%'").fetchall()]
        assert kinds == ["identity.agent_token_issued", "identity.agent_token_revoked"]
        assert token not in json.dumps([dict(r) for r in handle.store._conn.execute(  # type: ignore[attr-defined]
            "SELECT detail_json FROM audit_log").fetchall()])


def test_agent_token_validation_and_expiry(make_system: Callable[..., SystemHandle]) -> None:
    handle = make_system()
    with TestClient(create_app(system=handle)) as c:
        owner = owner_token(handle)
        for payload in ({"label": "x", "scopes": ["read", "approve"]}, {"label": "x", "days": 0},
                        {"label": "  ", "days": 3}, {"label": "x", "days": "7"}):
            assert c.post("/v1/agent-tokens", headers=auth(owner), json=payload).status_code == 422, payload
        reader = c.post("/v1/agent-tokens", headers=auth(owner), json={"label": "只读", "scopes": ["read"],
                                                                        "days": 1}).json()
        assert reader["scopes"] == ["read"]
        assert c.get("/v1/runs", headers=auth(reader["token"])).status_code == 200

        path = handle.settings.identity_file
        data = json.loads(path.read_text(encoding="utf-8"))
        data["subjects"][reader["subject"]]["expires_at"] = (utc_now() - timedelta(minutes=1)).isoformat()
        path.write_text(json.dumps(data), encoding="utf-8")
        assert c.get("/v1/runs", headers=auth(reader["token"])).status_code in DENIED
        listed = c.get("/v1/agent-tokens", headers=auth(owner)).json()["tokens"]
        assert listed[0]["active"] is False and listed[0]["revoked"] is False
