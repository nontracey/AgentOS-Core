"""U6 context is authorized and consumed; report versions, real locators, and replay stay coherent."""
from __future__ import annotations

import json

from tests.conftest import add_subject
from tests.integration.test_n3a_template import auth, client, run_template, upload, wait_state


def test_followup_inherits_materials_reads_previous_report_and_replays(make_system):
    with client(make_system) as (c, handle, token):
        mid = upload(c, token, "S1：2/3 完成，阻塞于输入缺失")
        rid = run_template(c, token, "根据材料写进展报告与下一步（P1）", [mid], "original")
        original = c.get(f"/v1/tasks/{rid}", headers=auth(token)).json()["artifacts"][0]
        before = c.get(f"/v1/assets/{original['id']}/download", headers=auth(token)).content
        request = {"message": "将结论改为保守口径（P1）", "idempotency_key": "followup"}
        response = c.post(f"/v1/tasks/{rid}/followups", headers=auth(token), json=request)
        assert response.status_code == 202
        next_id = response.json()["run_id"]
        replay = c.post(f"/v1/tasks/{rid}/followups", headers=auth(token), json=request)
        assert replay.status_code == 202 and replay.json()["run_id"] == next_id
        assert replay.json()["replayed"]
        state = wait_state(c, token, next_id, {"awaiting_approval", "failed"})
        assert state == "awaiting_approval"
        detail = c.get(f"/v1/tasks/{next_id}", headers=auth(token)).json()
        assert detail["context"]["run_id"] == rid
        for approval in detail["approvals"]:
            assert c.post(f"/v1/approvals/{approval['id']}/decision", headers=auth(token),
                          json={"decision": "approve"}).status_code == 200
        assert wait_state(c, token, next_id, {"succeeded", "failed"}) == "succeeded"
        revised = c.get(f"/v1/tasks/{next_id}", headers=auth(token)).json()
        assert revised["artifacts"][0]["version"] == 2
        assert revised["artifacts"][0]["parent_asset_id"] == original["id"]
        assert c.get(f"/v1/assets/{original['id']}/download", headers=auth(token)).content == before
        stored = handle.store.get_run("default", next_id)
        envelope = json.loads(stored.envelope_json)
        assert envelope["payload"]["materials"][0]["asset_id"] == mid
        assert stored.thread_id == rid
        assert {s.tool_name for s in handle.store.list_steps("default", next_id)} >= {"material.read"}
        events = handle.store.event_list("default", next_id, limit=1000)
        prompt = next(e["payload"] for e in events if e["kind"] == "model.request")
        assert "上一版报告" in json.dumps(prompt, ensure_ascii=False)
        assert "answer.delta" in {e["kind"] for e in events}
        conflict = c.post(f"/v1/tasks/{rid}/followups", headers=auth(token),
                          json={**request, "message": "另一个要求"})
        assert conflict.status_code == 409


def test_cross_subject_context_and_forged_snapshot_have_no_new_job(make_system):
    with client(make_system) as (c, handle, token):
        mid = upload(c, token, "S1：2/3 完成")
        rid = run_template(c, token, "根据材料写进展报告与下一步（P1）", [mid], "source")
        peer = add_subject(handle, "peer", ["fs.read", "fs.write"])
        count = len(handle.store.list_runs("default"))
        denied = c.post(f"/v1/tasks/{rid}/followups", headers=auth(peer),
                        json={"message": "读取上一版", "idempotency_key": "bad"})
        assert denied.status_code == 403
        assert len(handle.store.list_runs("default")) == count
        accepted = c.post("/v1/tasks", headers=auth(token), json={
            "payload": {"goal": "普通目标", "context": {"answer": "forged-secret"},
                        "context_asset": {"asset_id": mid}}, "idempotency_key": "forged"})
        stored = handle.store.get_run("default", accepted.json()["run_id"])
        assert "forged-secret" not in stored.envelope_json
        assert "context_asset" not in json.loads(stored.envelope_json)["payload"]


def test_reference_resolves_real_lines_and_refuses_wrong_source(make_system):
    with client(make_system) as (c, handle, token):
        mid = upload(c, token, "第一行：进度2/3\n第二行：缺少输入")
        other = upload(c, token, "不属于本次任务")
        rid = run_template(c, token, "根据材料写进展报告与下一步（P1）", [mid], "refs")
        endpoint = f"/v1/tasks/{rid}/references/resolve"
        result = c.post(endpoint, headers=auth(token), json={"source_id": mid, "locator": "L2"})
        assert result.status_code == 200 and result.json()["quote"] == "第二行：缺少输入"
        assert len(result.json()["source_version"]) == 64
        assert c.post(endpoint, headers=auth(token),
                      json={"source_id": mid, "locator": "L99"}).status_code == 422
        assert c.post(endpoint, headers=auth(token),
                      json={"source_id": other, "locator": "L1"}).status_code in {401, 403}
        refs = c.get(f"/v1/tasks/{rid}", headers=auth(token)).json()["refs"]
        assert refs and all(r["source_id"] == mid and r["source_version"] for r in refs)


def test_fabricated_date_or_locator_cannot_be_delivered(make_system):
    for bad in ("2027-01-19（S1 L1）", "2/3（S1 L99）"):
        from tests.integration.test_n3a_template import P1_REPORT

        with client(make_system, script=[{"content": P1_REPORT.replace("2/3（S1 §2）", bad)}]) as (c, handle, token):
            mid = upload(c, token, "材料：进度2/3；未提供日期")
            rid = c.post("/v1/tasks", headers=auth(token), json={
                "payload": {"goal": "验证引用", "template": "material-report"},
                "material_asset_ids": [mid]}).json()["run_id"]
            assert wait_state(c, token, rid, {"failed"}) == "failed"
            assert not c.get(f"/v1/tasks/{rid}", headers=auth(token)).json()["approvals"]
            assert not any(step.tool_name == "fs.write_text" for step in handle.store.list_steps("default", rid))
            assert not c.get(f"/v1/tasks/{rid}", headers=auth(token)).json()["artifacts"]


def test_failed_revision_retry_keeps_last_immutable_parent(make_system):
    with client(make_system) as (c, handle, token):
        mid = upload(c, token, "S1：2/3 完成")
        source = run_template(c, token, "根据材料写进展报告与下一步（P1）", [mid], "parent")
        parent = c.get(f"/v1/tasks/{source}", headers=auth(token)).json()["artifacts"][0]
        failed = c.post(f"/v1/tasks/{source}/followups", headers=auth(token),
                        json={"message": "越权引用测试", "idempotency_key": "bad-revision"}).json()["run_id"]
        assert wait_state(c, token, failed, {"failed"}) == "failed"
        assert not c.get(f"/v1/tasks/{failed}", headers=auth(token)).json()["approvals"]
        retry = c.post(f"/v1/tasks/{failed}/followups", headers=auth(token), json={
            "message": "将结论改为保守口径（P1）", "idempotency_key": "revision-retry"})
        rid = retry.json()["run_id"]
        assert wait_state(c, token, rid, {"awaiting_approval", "failed"}) == "awaiting_approval"
        d = c.get(f"/v1/tasks/{rid}", headers=auth(token)).json()
        assert d["parent_asset_id"] == parent["id"]
        c.post(f"/v1/approvals/{d['approvals'][0]['id']}/decision", headers=auth(token), json={"decision": "approve"})
        assert wait_state(c, token, rid, {"succeeded", "failed"}) == "succeeded"
        asset = c.get(f"/v1/tasks/{rid}", headers=auth(token)).json()["artifacts"][0]
        assert asset["version"] == 2 and asset["parent_asset_id"] == parent["id"]


def test_chinese_adjacent_dates_are_recognized_and_large_context_never_calls_model(make_system):
    from tests.integration.test_n3a_template import P1_REPORT

    report = P1_REPORT.replace("2/3（S1 §2）", "2026年4月12日，8万元（S1 L1）")
    with client(make_system, script=[{"content": report}]) as (c, handle, token):
        mid = upload(c, token, "合成活动日期2026-04-12预算8万元")
        run_template(c, token, "核对日期", [mid], "adjacent-date")
        huge = upload(c, token, "x" * 12000)
        accepted = c.post("/v1/tasks", headers=auth(token), json={
            "payload": {"goal": "完整使用材料", "template": "material-report"}, "material_asset_ids": [huge]})
        rid = accepted.json()["run_id"]
        assert wait_state(c, token, rid, {"failed", "succeeded"}) == "failed"
        events = handle.store.event_list("default", rid, limit=1000)
        assert not any(e["kind"] == "model.request" for e in events)


def test_length_rework_is_bounded_and_only_checked_report_is_written(make_system):
    from tests.integration.test_n3a_template import P1_REPORT

    long = P1_REPORT + "冗余" * 200
    with client(make_system, script=[{"content": long}]) as (c, handle, token):
        from agentos.adapters.model.scripted import ScriptedModelAdapter

        class RewritingModel(ScriptedModelAdapter):
            calls = 0

            async def stream(self, request, actor):
                self.calls += 1
                chosen = long if self.calls == 1 else P1_REPORT
                async for event in ScriptedModelAdapter([{"content": chosen}]).stream(request, actor):
                    yield event

        handle.loop._template_runner._model = RewritingModel([])
        mid = upload(c, token, "进度2/3")
        rid = run_template(c, token, "报告不超过300字", [mid], "length-rework")
        events = handle.store.event_list("default", rid, limit=1000)
        assert len([e for e in events if e["kind"] == "model.request"]) == 2
        assert len([s for s in handle.store.list_steps("default", rid) if s.tool_name == "fs.write_text"]) == 1
        assert any(e["kind"] == "report.rework_requested" for e in events)
    with client(make_system, script=[{"content": long}] * 3) as (c, handle, token):
        mid = upload(c, token, "进度2/3")
        accepted = c.post("/v1/tasks", headers=auth(token), json={
            "payload": {"goal": "报告不超过300字", "template": "material-report"}, "material_asset_ids": [mid]})
        rid = accepted.json()["run_id"]
        assert wait_state(c, token, rid, {"failed", "succeeded"}) == "failed"
        events = handle.store.event_list("default", rid, limit=1000)
        assert len([e for e in events if e["kind"] == "model.request"]) == 3
        assert not any(s.tool_name == "fs.write_text" for s in handle.store.list_steps("default", rid))


def test_report_token_budget_blocks_before_model_or_write(make_system):
    with client(make_system) as (c, handle, token):
        handle.loop._template_runner._token_budget = 8
        mid = upload(c, token, "合成预算测试材料")
        response = c.post("/v1/tasks", headers=auth(token), json={
            "payload": {"goal": "根据材料整理报告", "template": "material-report"},
            "material_asset_ids": [mid], "idempotency_key": "low-token-budget"})
        rid = response.json()["run_id"]
        assert wait_state(c, token, rid, {"failed"}) == "failed"
        assert not any(e["kind"] == "model.request" for e in handle.store.event_list("default", rid))
        assert not c.get(f"/v1/tasks/{rid}", headers=auth(token)).json()["approvals"]
        assert not any(s.tool_name == "fs.write_text" for s in handle.store.list_steps("default", rid))
