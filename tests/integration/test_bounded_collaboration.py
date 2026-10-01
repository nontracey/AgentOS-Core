"""Synthetic protocol evidence for same-run repair; does not claim real GPU evidence."""

import asyncio
import hashlib
import json

import pytest

from agentos.kernel.types import ToolResult, ToolSpec
from tests.integration.test_n3a_template import auth, client, upload, wait_state

REPORT = (
    "## 摘要\n合成品牌 A 户外活动，杭州（S1 L1）。\n## 关键结论\n建议组织徒步。"
    "\n## 证据与引用\nS1 L1\n## 未决项\n材料不足时需补日期预算。"
)


@pytest.mark.parametrize("missing_forever", [False, True])
def test_same_run_repair_keeps_visual_and_stable_supplement(make_system, monkeypatch, missing_forever):
    with client(make_system, script=[{"content": REPORT}]) as (c, handle, token):
        actor = asyncio.run(handle.deps.identity.authenticate({"token": token}))
        handle.nodes.pair(actor, "win", "http://synthetic-node.invalid", os="windows")
        original_call = handle.tool_hub.call
        count = []

        async def tools(a):
            local = await handle.tool_hub._inner.list_tools(a)
            return [
                *local,
                ToolSpec(
                    name="node_win_comfyui_generate",
                    version="1.0",
                    input_schema={"type": "object"},
                    risks=["write"],
                    timeout_ms=1000,
                ),
            ]

        async def call(name, args, ctx):
            if name != "node_win_comfyui_generate":
                return await original_call(name, args, ctx)
            count.append(args)
            data = b"synthetic-image-bytes"
            asset = handle.assets._write_and_register(
                ctx.actor, data, mime="image/png", meta={}, source_node="win", run_id=ctx.run_id
            )
            return ToolResult(
                call_id=ctx.call_id,
                status="completed",
                value={
                    "asset_id": asset["asset_id"],
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "delivery": "registered",
                },
            )

        original_resolve = handle.tool_hub.resolve

        async def resolve(a, name):
            return (await tools(a))[-1] if name == "node_win_comfyui_generate" else await original_resolve(a, name)

        monkeypatch.setattr(handle.tool_hub, "resolve", resolve)
        monkeypatch.setattr(handle.tool_hub, "list_tools", tools)
        monkeypatch.setattr(handle.tool_hub, "call", call)
        mid = upload(c, token, "合成品牌 A：主营户外装备，活动城市杭州。")
        r = c.post(
            "/v1/tasks",
            headers=auth(token),
            json={
                "payload": {"goal": "合成活动方案和主视觉", "template": "activity-collaboration"},
                "material_asset_ids": [mid],
                "idempotency_key": "c-initial",
            },
        )
        assert r.status_code == 202, r.text
        rid = r.json()["run_id"]
        assert wait_state(c, token, rid, {"awaiting_approval", "failed"}) == "awaiting_approval", c.get(
            f"/v1/tasks/{rid}", headers=auth(token)
        ).json()["error"]
        detail = c.get(f"/v1/tasks/{rid}", headers=auth(token)).json()
        for a in detail["approvals"]:
            c.post(
                f"/v1/approvals/{a['id']}/decision", headers=auth(token), json={"decision": "approve"}
            ).raise_for_status()
        assert wait_state(c, token, rid, {"awaiting_input", "failed"}) == "awaiting_input", c.get(
            f"/v1/tasks/{rid}", headers=auth(token)
        ).json()["error"]
        initial = c.get(f"/v1/tasks/{rid}", headers=auth(token)).json()
        visual = initial["artifacts"][0]
        supplement = upload(c, token, "合成补充资料：主营户外装备。" if missing_forever else
                            "合成补充资料：春季徒步节日期 2026-04-12，预算 8 万元。")
        body = {"message": "补充日期与预算来源", "material_asset_ids": [supplement], "idempotency_key": "repair"}
        response = c.post(f"/v1/tasks/{rid}/inputs", headers=auth(token), json=body)
        assert response.status_code == 202 and response.json()["run_id"] == rid
        replay = c.post(f"/v1/tasks/{rid}/inputs", headers=auth(token), json=body)
        assert replay.status_code == 202 and replay.json()["replayed"]
        assert (
            c.post(f"/v1/tasks/{rid}/inputs", headers=auth(token), json={**body, "message": "changed"}).status_code
            == 409
        )
        if missing_forever:
            assert wait_state(c, token, rid, {"awaiting_input", "failed"}) == "awaiting_input"
            second = {**body, "idempotency_key": "repair-2"}
            assert c.post(f"/v1/tasks/{rid}/inputs", headers=auth(token), json=second).status_code == 202
            assert wait_state(c, token, rid, {"failed"}) == "failed"
            assert c.post(f"/v1/tasks/{rid}/inputs", headers=auth(token),
                          json={**body, "idempotency_key": "repair-3"}).status_code == 409
            assert len(count) == 1
            assert len(c.get(f"/v1/tasks/{rid}", headers=auth(token)).json()["artifacts"]) == 1
            return
        assert wait_state(c, token, rid, {"awaiting_approval", "failed"}) == "awaiting_approval", c.get(
            f"/v1/tasks/{rid}", headers=auth(token)
        ).json()["error"]
        detail = c.get(f"/v1/tasks/{rid}", headers=auth(token)).json()
        for a in detail["approvals"]:
            c.post(
                f"/v1/approvals/{a['id']}/decision", headers=auth(token), json={"decision": "approve"}
            ).raise_for_status()
        assert wait_state(c, token, rid, {"succeeded", "failed"}) == "succeeded"
        final = c.get(f"/v1/tasks/{rid}", headers=auth(token)).json()
        assert final["plan"]["version"] == 2
        assert len(count) == 1
        cp = json.loads(handle.store.get_run("default", rid).checkpoint_json)
        assert cp["collaboration"]["tasks"]["deliver-1"]["state"] == "completed"
        assert cp["template"]["refs"]
        assert next(a for a in final["artifacts"] if a["id"] == visual["id"])["sha256"] == visual["sha256"]
        events = handle.store.event_list("default", rid, limit=2000)
        assert any(e["kind"] == "plan.rework_accepted" for e in events)
        assert {e["payload"]["plan"]["version"] for e in events if e["kind"] == "plan.version_created"} == {1, 2}
        resolved = c.post(
            f"/v1/tasks/{rid}/references/resolve", headers=auth(token), json={"source_id": supplement, "locator": "L1"}
        )
        assert resolved.status_code == 200 and "8 万元" in resolved.json()["quote"]
