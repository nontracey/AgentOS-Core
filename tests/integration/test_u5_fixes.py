"""U5-02 安全与恢复负向回归（独立复核 Q7～Q10 的合成复现转为持续测试）。

Q7 撤权账本不可写/损坏 → 失败关闭，不得报"已撤销"后让旧游客复活；
Q8 来源删除后，成果正文即使没命中已发布 quote 也要撤下，不可核对字节同样拒绝；
Q9 非直播任务的游客游标不得越过本 revision 高水位，重发后按旧游标续接不漏事件；
Q10 管理员在租户范围内可处理受控主体任务的审批/取消/对账，普通主体与游客仍受限。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from agentos.apps.composition import SystemHandle
from agentos.apps.gateway import create_app
from agentos.kernel.states import StepState
from agentos.kernel.store import StepRecord
from agentos.kernel.types import AuthContext
from tests.conftest import WRITE_FLOW_SCRIPT, add_subject, owner_token


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


#: 无审批步骤、可达 succeeded 的脚本（默认发布要求已完成任务 Q6）
DEMO_SCRIPT: list[dict[str, Any]] = [
    {"tool_calls": [{"id": "c1", "name": "math.add", "arguments": {"a": 2, "b": 3}}]},
    {"content": "done: sum=5 and report ready"},
]


@contextmanager
def client(make_system: Callable[..., SystemHandle],
           script: list[dict[str, Any]] | None = None) -> Iterator[Any]:
    handle = make_system(model_script=script or DEMO_SCRIPT)
    with TestClient(create_app(system=handle)) as c:
        yield c, handle, owner_token(handle)


def submit(c: TestClient, token: str, goal: str, key: str) -> str:
    r = c.post("/v1/tasks", headers=auth(token),
               json={"payload": {"goal": goal}, "idempotency_key": key})
    assert r.status_code == 202, r.text
    return r.json()["run_id"]


def wait_state(c: TestClient, token: str, rid: str, states: set[str],
               timeout: float = 10) -> str:
    deadline = time.monotonic() + timeout
    state = ""
    while time.monotonic() < deadline:
        state = c.get(f"/v1/tasks/{rid}", headers=auth(token)).json()["run"]["state"]
        if state in states:
            return state
        time.sleep(0.05)
    return state


def guest(c: TestClient, token: str, label: str) -> tuple[str, str, str, str]:
    r = c.post("/v1/demo/grants", headers=auth(token), json={"label": label, "ttl_hours": 24})
    body = r.json()
    rr = c.post("/v1/auth/guest/redeem", json={"code": body["invite_code"]})
    return body["grant_id"], rr.json()["subject"], rr.json()["access_token"], rr.json()["refresh_token"]


def publish(c: TestClient, token: str, grant_id: str, runs: list[dict[str, Any]],
            assets: list[dict[str, Any]] | None = None,
            refs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    for item in runs:
        rid = str(item.get("run_id") or "")
        if rid and not item.get("live"):
            wait_state(c, token, rid, {"succeeded", "failed", "cancelled", "awaiting_approval"})
    r = c.post(f"/v1/demo/grants/{grant_id}/publish", headers=auth(token),
               json={"runs": runs, "assets": assets or [], "refs": refs or []})
    assert r.status_code == 201, r.text
    return r.json()


def add_ref_step(handle: SystemHandle, rid: str, source_id: str, chunk_id: str) -> None:
    from agentos.kernel.types import Ref, ToolResult

    call_id = f"call-{chunk_id}"
    result = ToolResult(
        call_id=call_id, status="completed",
        refs=[Ref(source_id=source_id, source_version="1", locator=chunk_id, quote="")],
    )
    step_id = f"step-{chunk_id}"
    handle.store.create_step(StepRecord(
        tenant_id="default", run_id=rid, id=step_id, call_id=call_id,
        tool_name="knw.search", state=StepState.PREPARED,
    ))
    handle.store.transition_step(
        "default", rid, step_id, {StepState.PREPARED}, StepState.COMPLETED,
        result_json=result.model_dump_json(),
    )


# ------------------------------------------------------------------ Q7
def test_unwritable_ledger_blocks_revocation_instead_of_false_success(
    make_system: Callable[..., SystemHandle],
) -> None:
    """账本写不下去时撤销必须失败：不得改库并回报成功（否则旧快照能让游客复活）。"""
    with client(make_system) as (c, handle, token):
        # 让账本路径不可写：父目录位置放一个普通文件
        blocked = handle.settings.data_dir / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")
        handle.demo._ledger_path = blocked / "ledger" / "demo_revocations.json"

        rid = submit(c, token, "q7 target", "q7-1")
        grant, _subj, gtok, _ref = guest(c, token, "q7")
        publish(c, token, grant, [{"run_id": rid}])
        assert c.get(f"/v1/demo/runs/{rid}", headers=auth(gtok)).status_code == 200

        r = c.post(f"/v1/demo/grants/{grant}/revoke", headers=auth(token))
        assert r.status_code == 500, r.text        # UNCERTAIN：撤销结果未知，不冒充成功
        assert "账本" in r.json()["detail"]["message"]
        # 未被"口头撤销"：库内仍是 active，管理员能看清失败原因并重试
        assert str(handle.store.get_demo_grant("default", grant)["status"]) == "active"

        # void-all 同样失败关闭，不作废任何 grant
        r2 = c.post("/v1/demo/void-all", headers=auth(token), json={"reason": "restore"})
        assert r2.status_code == 500, r2.text
        assert str(handle.store.get_demo_grant("default", grant)["status"]) == "active"

        # 修好账本位置后撤销成功，游客读取立即失效
        real = handle.settings.data_dir / "fixed-ledger.json"
        handle.demo._ledger_path = real
        assert c.post(f"/v1/demo/grants/{grant}/revoke", headers=auth(token)).status_code == 200
        assert c.get(f"/v1/demo/runs/{rid}", headers=auth(gtok)).status_code in (401, 403, 404)


def test_corrupt_ledger_fails_closed_on_restart(
    make_system: Callable[..., SystemHandle],
) -> None:
    """账本不可读（这里是整个文件被换成目录）且库内有 active grant → 启动重放一律作废。"""
    handle = make_system(model_script=DEMO_SCRIPT)
    with TestClient(create_app(system=handle)) as c:
        token = owner_token(handle)
        rid = submit(c, token, "q7 corrupt", "q7-2")
        grant, _subj, gtok, _ref = guest(c, token, "q7b")
        publish(c, token, grant, [{"run_id": rid}])
        assert c.get(f"/v1/demo/runs/{rid}", headers=auth(gtok)).status_code == 200
        # 账本路径不写死：从服务取当前配置的位置
        ledger = Path(str(handle.demo._ledger_path))
    handle.store.raw_connection().close()
    # 用目录顶掉账本文件：读取抛 OSError，无法证明 active grant 未被撤销
    ledger.unlink(missing_ok=True)
    ledger.mkdir()
    handle2 = make_system(model_script=DEMO_SCRIPT)
    handle2.boot()
    assert str(handle2.store.get_demo_grant("default", grant)["status"]) == "revoked"
    with TestClient(create_app(system=handle2)) as c2:
        assert c2.get(f"/v1/demo/runs/{rid}", headers=auth(gtok)).status_code in (401, 403, 404)


# ------------------------------------------------------------------ Q8
def test_deleted_source_pulls_asset_even_without_matching_quote(
    make_system: Callable[..., SystemHandle],
) -> None:
    """成果正文未命中已发布 quote，但同任务来源被删 → 仍必须撤下预览与下载。"""
    with client(make_system) as (c, handle, token):
        actor: AuthContext = asyncio.run(handle.deps.identity.authenticate({"token": token}))
        rid = submit(c, token, "q8 report", "q8-1")
        asyncio.run(handle.knowledge.ingest(actor, "src-q8", "活动日期以 2026-04-12 为准。", ["*"]))
        add_ref_step(handle, rid, "src-q8", "src-q8:0")
        # 正文含来源内容，但发布的展示片段是另一小段（旧实现按 quote 精确匹配会漏）
        report = handle.assets.register_report_version(
            actor, data="报告结论：活动日期 2026-04-12，另附未决项。".encode(),
            name="q8.md", run_id=rid)
        grant, _s, gtok, _r = guest(c, token, "q8")
        publish(c, token, grant, [{"run_id": rid, "include": ["artifacts", "plan", "steps"]}],
                assets=[{"asset_id": report["asset_id"], "version": 1,
                         "preview": True, "download": True}],
                refs=[{"source_id": "src-q8", "chunk_id": "src-q8:0",
                       "quote": "活动日期", "run_id": rid}])
        aid = report["asset_id"]
        assert c.get(f"/v1/demo/assets/{aid}", headers=auth(gtok)).status_code == 200
        assert c.get(f"/v1/demo/assets/{aid}/download", headers=auth(gtok)).status_code == 200

        asyncio.run(handle.knowledge.delete(actor, "src-q8"))
        # 撤下：详情不再列该成果，元数据/下载统一 404，正文不泄露
        detail = c.get(f"/v1/demo/runs/{rid}", headers=auth(gtok)).json()
        assert all(a.get("id") != aid for a in detail["artifacts"])
        assert c.get(f"/v1/demo/assets/{aid}", headers=auth(gtok)).status_code == 404
        dl = c.get(f"/v1/demo/assets/{aid}/download", headers=auth(gtok))
        assert dl.status_code == 404 and "2026-04-12" not in dl.text


def test_unverifiable_binary_asset_with_dead_source_is_refused(
    make_system: Callable[..., SystemHandle],
) -> None:
    """媒体成果无法做内容扫描：来源失效时一律拒绝，不默认放行。"""
    with client(make_system) as (c, handle, token):
        actor: AuthContext = asyncio.run(handle.deps.identity.authenticate({"token": token}))
        rid = submit(c, token, "q8 media", "q8-2")
        asyncio.run(handle.knowledge.ingest(actor, "src-media", "品牌规范原文。", ["*"]))
        add_ref_step(handle, rid, "src-media", "src-media:0")
        media = handle.assets.register_report_version(
            actor, data=b"\x89PNG\r\n\x1a\nfake-bytes", name="visual.png", run_id=rid,
            mime="image/png")
        grant, _s, gtok, _r = guest(c, token, "q8m")
        publish(c, token, grant, [{"run_id": rid, "include": ["artifacts"]}],
                assets=[{"asset_id": media["asset_id"], "version": 1,
                         "preview": True, "download": True}],
                refs=[{"source_id": "src-media", "chunk_id": "src-media:0",
                       "quote": "品牌规范", "run_id": rid}])
        assert c.get(f"/v1/demo/assets/{media['asset_id']}",
                     headers=auth(gtok)).status_code == 200
        asyncio.run(handle.knowledge.delete(actor, "src-media"))
        assert c.get(f"/v1/demo/assets/{media['asset_id']}", headers=auth(gtok)).status_code == 404
        assert c.get(f"/v1/demo/assets/{media['asset_id']}/download",
                     headers=auth(gtok)).status_code == 404


# ------------------------------------------------------------------ Q9
def test_guest_cursor_never_exceeds_high_water_and_republish_continues(
    make_system: Callable[..., SystemHandle],
) -> None:
    """游标封顶在高水位；重发 revision 后按旧游标续接能收到之前不可见的新事件。"""
    with client(make_system) as (c, handle, token):
        rid = submit(c, token, "q9 cursor", "q9-1")
        assert wait_state(c, token, rid, {"succeeded"}) == "succeeded"
        # 冻结发布时刻的高水位，之后再追加事件也不进本次快照
        h1 = handle.store.event_high_water("default", rid)
        grant, _s, gtok, _ref = guest(c, token, "q9")
        publish(c, token, grant, [{"run_id": rid, "include": ["steps"]}])

        # 注入更多事件（含投影会丢弃的隐藏事件），游客游标不得越过 h1
        appended = []
        for i in range(220):
            kind = "model.request" if i % 2 else "run.state_changed"
            payload: dict[str, Any] = (
                {"system": f"private-{i}"} if kind == "model.request"
                else {"to": "running"})
            appended.append(handle.store.event_append("default", rid, kind, payload))
        page = c.get(f"/v1/demo/runs/{rid}/events?cursor=0&limit=500",
                     headers=auth(gtok)).json()
        assert int(page["cursor"]) <= h1, page["cursor"]
        assert all(int(e["seq"]) <= h1 for e in page["events"])
        assert not any("private-" in json.dumps(e, ensure_ascii=False) for e in page["events"])

        # 重发 revision 2：高水位上移，旧游标续接不漏事件（原缺陷：漏掉 h1 之后的事件）
        h2 = handle.store.event_high_water("default", rid)
        assert h2 > h1
        publish(c, token, grant, [{"run_id": rid, "include": ["steps", "plan"]}])
        after = c.get(f"/v1/demo/runs/{rid}/events?cursor={page['cursor']}&limit=500",
                      headers=auth(gtok)).json()
        assert after["manifest_revision"] == 2
        new_seqs = {int(e["seq"]) for e in after["events"]}
        assert any(int(e["seq"]) > h1 for e in after["events"]), \
            "旧游标越过未发布事件会造成续接漏事件"
        assert max(new_seqs, default=0) <= h2


# ------------------------------------------------------------------ U5 预览/下载与投影流
def test_preview_and_download_are_separate_routes_and_gates(
    make_system: Callable[..., SystemHandle],
) -> None:
    """能预览不等于能下载：两个入口各自核权，不能复用彼此开关。"""
    with client(make_system) as (c, handle, token):
        actor: AuthContext = asyncio.run(handle.deps.identity.authenticate({"token": token}))
        rid = submit(c, token, "preview gate", "u5p-1")
        assert wait_state(c, token, rid, {"succeeded"}) == "succeeded"
        preview_only = handle.assets.register_report_version(
            actor, data=b"preview only body", name="p.md", run_id=rid)
        with_download = handle.assets.register_report_version(
            actor, data=b"downloadable body", name="d.md", run_id=rid, mime="text/plain")
        grant, _s, gtok, _r = guest(c, token, "u5p")
        publish(c, token, grant, [{"run_id": rid, "include": ["artifacts", "steps"]}],
                assets=[{"asset_id": preview_only["asset_id"], "version": 1,
                         "preview": True, "download": False},
                        {"asset_id": with_download["asset_id"], "version": 1,
                         "preview": False, "download": True}])
        p_id, d_id = preview_only["asset_id"], with_download["asset_id"]
        # 只开预览：预览 200、下载 404
        r_prev = c.get(f"/v1/demo/assets/{p_id}/preview", headers=auth(gtok))
        assert r_prev.status_code == 200 and r_prev.content == b"preview only body"
        assert c.get(f"/v1/demo/assets/{p_id}/download", headers=auth(gtok)).status_code == 404
        # 只开下载：下载 200、预览 404（旧缺陷：预览复用下载开关）
        r_dl = c.get(f"/v1/demo/assets/{d_id}/download", headers=auth(gtok))
        assert r_dl.status_code == 200 and r_dl.content == b"downloadable body"
        assert c.get(f"/v1/demo/assets/{d_id}/preview", headers=auth(gtok)).status_code == 404
        # 元数据两个开关独立上报
        meta = c.get(f"/v1/demo/assets/{p_id}", headers=auth(gtok)).json()
        assert meta["preview"] is True and meta["download"] is False
        # 管理员不能走游客字节通道（非游客会话 → 拒绝）
        assert c.get(f"/v1/demo/assets/{p_id}/preview",
                     headers=auth(token)).status_code in (401, 403, 404)
        # 撤权后两个入口统一失效
        c.post(f"/v1/demo/grants/{grant}/revoke", headers=auth(token))
        assert c.get(f"/v1/demo/assets/{p_id}/preview", headers=auth(gtok)).status_code in (
            401, 403, 404)
        assert c.get(f"/v1/demo/assets/{d_id}/download", headers=auth(gtok)).status_code in (
            401, 403, 404)


def test_guest_stream_uses_demo_route_and_projection(
    make_system: Callable[..., SystemHandle],
) -> None:
    """游客流走 /v1/demo/.../stream：只收到白名单投影，隐藏事件与正文不透传。"""
    with client(make_system) as (c, handle, token):
        rid = submit(c, token, "demo stream", "u5s-1")
        assert wait_state(c, token, rid, {"succeeded"}) == "succeeded"
        grant, _s, gtok, _r = guest(c, token, "u5s")
        publish(c, token, grant, [{"run_id": rid, "include": ["steps"]}])
        # 未发布的任务在游客流上统一 404
        other = submit(c, token, "unpublished stream", "u5s-2")
        assert wait_state(c, token, other, {"succeeded"}) == "succeeded"
        assert c.get(f"/v1/demo/runs/{other}/stream?follow=0",
                     headers=auth(gtok)).status_code == 404

        # 注入一条含秘密的不可投影事件（高于已冻结高水位，双重不得下发）
        handle.store.event_append("default", rid, "model.request",
                                  {"messages": [{"role": "system", "content": "SECRET-PROMPT"}]})
        with c.stream("GET", f"/v1/demo/runs/{rid}/stream?cursor=0&follow=0",
                      headers=auth(gtok)) as resp:
            assert resp.status_code == 200
            raw = "".join(resp.iter_text())
        assert "SECRET-PROMPT" not in raw
        assert "model.request" not in raw
        assert "stream.end" in raw                       # 追平后正常结束
        assert '"kind": "step.state_changed"' in raw or '"kind":"step.state_changed"' in raw
        # 无凭据不能订阅游客流
        assert c.get(f"/v1/demo/runs/{rid}/stream?follow=0").status_code in (401, 403, 404)


# ------------------------------------------------------------------ Q10
def test_admin_can_process_cross_subject_task_and_approvals(
    make_system: Callable[..., SystemHandle],
) -> None:
    """管理员可处理受控主体任务的审批/取消/对账；普通主体与游客仍拒。"""
    with client(make_system, script=WRITE_FLOW_SCRIPT) as (c, handle, token):
        # 受控第二主体（webhook 服务身份形态）产生一个停在待审批的任务
        svc_token = add_subject(handle, "svc-webhook", ["read", "write", "approve"])
        rid = submit(c, svc_token, "webhook needs approval", "q10-1")
        assert wait_state(c, svc_token, rid, {"awaiting_approval"}) == "awaiting_approval"
        pending = handle.deps.approvals.list_pending("default")
        assert [p.subject for p in pending] == ["svc-webhook"]

        # 管理员：列表可见（不是被过滤掉），批准可执行，run 继续到终态
        listed = c.get("/v1/approvals", headers=auth(token)).json()
        assert [a["id"] for a in listed] == [p.id for p in pending]
        r = c.post(f"/v1/approvals/{pending[0].id}/decision", headers=auth(token),
                   json={"decision": "approve"})
        assert r.status_code == 200, r.text
        assert wait_state(c, token, rid, {"succeeded", "failed", "cancelled"}) in {
            "succeeded", "failed", "cancelled"}
        # 审计记真实操作者，不冒充任务 owner
        audits = handle.store.audit_query("default", kind_prefix="approval.decided_cross_subject")
        assert audits and audits[0].subject == "owner"
        assert audits[0].detail["owner"] == "svc-webhook"

        # 管理员取消跨主体任务
        rid2 = submit(c, svc_token, "webhook cancel", "q10-2")
        wait_state(c, svc_token, rid2, {"running", "awaiting_approval", "succeeded"})
        assert c.post(f"/v1/tasks/{rid2}/cancel", headers=auth(token)).status_code == 202

        # 管理员核对跨主体 uncertain 步骤：授权门通过（后续为状态/探针判定，不是 404）
        handle.store.create_step(StepRecord(
            tenant_id="default", run_id=rid2, id="step-u", call_id=f"{rid2}:step-u",
            tool_name="custom.effect", state=StepState.UNCERTAIN,
        ))
        rr = c.post(f"/v1/tasks/{rid2}/reconcile", headers=auth(token),
                    json={"step_id": "step-u"})
        assert rr.status_code in (409, 501), rr.text

        # 同租户普通主体：跨主体仍然 404（与不存在同形），且不能处理别人的审批
        other = add_subject(handle, "member-x", ["read", "write", "approve"])
        rid3 = submit(c, token, "owner task", "q10-3")
        assert c.post(f"/v1/tasks/{rid3}/cancel", headers=auth(other)).status_code == 404
        assert c.post(f"/v1/tasks/{rid3}/reconcile", headers=auth(other),
                      json={"step_id": "step-u"}).status_code == 404
        assert c.get("/v1/approvals", headers=auth(other)).json() == []
        actor_other: AuthContext = asyncio.run(
            handle.deps.identity.authenticate({"token": other}))
        from agentos.kernel.errors import AgentOSError

        try:
            handle.deps.approvals.decide(actor_other, pending[0].id, "approve")
            raise AssertionError("member must not decide another subject's approval")
        except AgentOSError as exc:
            assert exc.code.value == "denied"

        # 游客：任何写入口仍然拒绝，且不因管理员放开跨主体而取得权限
        grant, _s, gtok, _ref = guest(c, token, "q10")
        publish(c, token, grant, [{"run_id": rid}])
        assert c.post(f"/v1/tasks/{rid3}/cancel", headers=auth(gtok)).status_code in (401, 403)
        assert c.post(f"/v1/approvals/{pending[0].id}/decision", headers=auth(gtok),
                      json={"decision": "approve"}).status_code in (401, 403)


def test_restore_replay_respects_reopened_channel(
    make_system: Callable[..., SystemHandle],
) -> None:
    """重放按"最后一条账本事实"判定：先撤后开的通道恢复旧快照后不再被误撤。"""
    handle = make_system(model_script=DEMO_SCRIPT)
    with TestClient(create_app(system=handle)) as c:
        token = owner_token(handle)
        rid = submit(c, token, "reopened after revoke", "q7-3")
        grant, _subj, gtok, _ref = guest(c, token, "q7c")
        publish(c, token, grant, [{"run_id": rid}])
        # 制造"先撤销"事实，再显式重开该 grant
        handle.demo._ledger_append(grant)
        handle.store.set_demo_grant("default", grant, status="revoked",
                                    revoked_at="2026-01-01T00:00:00+00:00")
        handle.demo._ledger_append(grant, state="active")
        handle.store.set_demo_grant("default", grant, status="active",
                                    clear_revoked_at=True)
        assert c.get(f"/v1/demo/runs/{rid}", headers=auth(gtok)).status_code == 200
        db_path = handle.settings.db_path
        backup = handle.settings.data_dir / "pre-reopen.db"
        src, dst = sqlite3.connect(str(db_path)), sqlite3.connect(str(backup))
        src.backup(dst)
        dst.close()
        src.close()
    handle.store.raw_connection().close()
    shutil.copy(backup, db_path)
    for suffix in ("-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    handle2 = make_system(model_script=DEMO_SCRIPT)
    handle2.boot()
    row = handle2.store.get_demo_grant("default", grant)
    assert row is not None and str(row["status"]) == "active", \
        "账本最后一行是 active：重放不得把已重开的授权再撤一次"
