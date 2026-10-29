"""Owner pre-authorization of single tools (FR-GOV-02): bounded, revocable, never for delete/egress."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from agentos.apps.composition import SystemHandle
from agentos.apps.gateway import create_app
from agentos.kernel.errors import AgentOSError
from agentos.kernel.types import AuthContext, ToolSpec, utc_now
from agentos.services.grants import GrantService
from tests.conftest import add_subject, owner_token
from tests.integration.test_n3a_template import auth, wait_state

WRITE = [{"tool_calls": [{"id": "w", "name": "fs.write_text", "arguments": {"path": "g.txt", "content": "x"}}]},
         {"content": "written"}]


def test_grant_lets_owner_tasks_skip_step_approval_until_revoked(make_system: Callable[..., SystemHandle]) -> None:
    handle = make_system(model_script=WRITE * 3)
    with TestClient(create_app(system=handle)) as c:
        token = owner_token(handle)
        issued = c.post("/v1/grants", headers=auth(token), json={"tool_name": "fs.write_text", "hours": 2})
        assert issued.status_code == 201, issued.text
        grant = issued.json()
        assert grant["active"] and grant["risks"] == ["write"]
        rid = c.post("/v1/tasks", headers=auth(token), json={"payload": {"goal": "write"}}).json()["run_id"]
        assert wait_state(c, token, rid, {"succeeded", "awaiting_approval", "failed"}) == "succeeded"
        assert c.get(f"/v1/tasks/{rid}", headers=auth(token)).json()["approvals"] == []

        assert c.post(f"/v1/grants/{grant['id']}/revoke", headers=auth(token)).json()["active"] is False
        rid = c.post("/v1/tasks", headers=auth(token), json={"payload": {"goal": "write again"}}).json()["run_id"]
        assert wait_state(c, token, rid, {"succeeded", "awaiting_approval", "failed"}) == "awaiting_approval"
        assert [g["id"] for g in c.get("/v1/grants", headers=auth(token)).json()] == [grant["id"]]

        assert c.post("/v1/grants", headers=auth(token),
                      json={"tool_name": "fs.write_text", "hours": 24 * 30}).status_code == 422
        assert c.post("/v1/grants", headers=auth(token), json={"tool_name": "no.such", "hours": 1}).status_code == 422
        member = add_subject(handle, "member", ["read", "write", "approve"])
        assert c.post("/v1/grants", headers=auth(member),
                      json={"tool_name": "fs.write_text", "hours": 1}).status_code == 401
        audit = [e.kind for e in handle.store.audit_query("default", kind_prefix="policy.grant")]
        assert audit == ["policy.grant_issued", "policy.grant_revoked"]


class Tools:
    def __init__(self, risks: list[str]) -> None:
        self.risks = risks

    async def resolve(self, actor: AuthContext, name: str) -> ToolSpec:
        return ToolSpec(name=name, version="1", description="", input_schema={"type": "object"}, risks=self.risks,
                        timeout_ms=1000)


def test_deleting_and_outbound_tools_keep_step_approval(system: SystemHandle) -> None:
    owner = AuthContext(subject="owner", tenant_id="default", roles=["admin"], scopes=["read", "write", "approve"],
                        policy_version="t", expires_at=utc_now() + timedelta(hours=1))
    for risks in (["read", "delete"], ["egress"]):
        with pytest.raises(AgentOSError, match="per-step approval"):
            asyncio.run(GrantService(system.store, system.deps.audit, Tools(risks)).issue(owner, "x.tool", 1))
    granted = asyncio.run(GrantService(system.store, system.deps.audit, Tools(["write", "exec"])).issue(
        owner, "node_gen_aigc_image", 1))
    assert granted["tool_name"] == "node_gen_aigc_image"
