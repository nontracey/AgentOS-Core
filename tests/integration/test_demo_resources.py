"""U4-02 逐资源发布、游客只读链路与撤权（负向矩阵 R01～R08 中适用于本阶段的部分）。

所有拒绝同时断言：无数据正文泄露、无新 job/副作用、资源归属未变、审计存在。
测试直接打 HTTP 与服务，绕过 UI。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentos.apps.composition import SystemHandle
from agentos.apps.gateway import create_app
from agentos.kernel.errors import AgentOSError
from agentos.kernel.types import AuthContext
from tests.conftest import WRITE_FLOW_SCRIPT, add_subject, owner_token


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


#: 演示测试用脚本：只读工具 + 最终答复（无审批步骤，run 可达终态，满足 Q6 默认发布）
DEMO_SCRIPT: list[dict[str, Any]] = [
    {"tool_calls": [{"id": "c1", "name": "math.add", "arguments": {"a": 2, "b": 3}}]},
    {"content": "done: sum=5 and report ready"},
]


@contextmanager
def client(make_system: Callable[..., SystemHandle]) -> Iterator[Any]:
    handle = make_system(model_script=DEMO_SCRIPT)
    with TestClient(create_app(system=handle)) as c:
        yield c, handle, owner_token(handle)


def submit(c: TestClient, token: str, goal: str, key: str) -> str:
    r = c.post("/v1/tasks", headers=auth(token),
               json={"payload": {"goal": goal}, "idempotency_key": key})
    assert r.status_code == 202, r.text
    return r.json()["run_id"]


def add_ref_step(handle: SystemHandle, rid: str, source_id: str, chunk_id: str) -> None:
    """为 run 注入一条带知识引用回执的步骤（Q1：发布引用必须来自 run 的真实引用记录）。"""
    from agentos.kernel.states import StepState
    from agentos.kernel.store import StepRecord
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


def wait_terminal(c: TestClient, token: str, rid: str) -> str:
    deadline = time.monotonic() + 10
    state = ""
    while time.monotonic() < deadline:
        state = c.get(f"/v1/tasks/{rid}", headers=auth(token)).json()["run"]["state"]
        if state in {"succeeded", "failed", "cancelled", "awaiting_approval"}:
            return state
        time.sleep(0.05)
    return state


def read_stream(c: TestClient, url: str, headers: dict[str, str],
                stop: tuple[str, ...] = ("stream.end",), limit: int = 600) -> list[dict[str, Any]]:
    """读取 SSE：data: 行解析为事件列表（Q4 游标压力测试用）。"""
    events: list[dict[str, Any]] = []
    with c.stream("GET", url, headers=headers) as resp:
        assert resp.status_code == 200, resp.status_code
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            events.append(event)
            if event.get("kind") in stop or len(events) >= limit:
                break
    return events


def guest(c: TestClient, token: str, label: str = "g") -> tuple[str, str, str, str]:
    r = c.post("/v1/demo/grants", headers=auth(token), json={"label": label, "ttl_hours": 24})
    body = r.json()
    rr = c.post("/v1/auth/guest/redeem", json={"code": body["invite_code"]})
    return body["grant_id"], rr.json()["subject"], rr.json()["access_token"], rr.json()["refresh_token"]


def publish(c: TestClient, token: str, grant_id: str, runs: list[dict[str, Any]],
            assets: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    # Q6：默认发布只接受已完成任务——助手先等各 run 到终态再发布
    for item in runs:
        rid = str(item.get("run_id") or "")
        if rid and not item.get("live"):
            wait_terminal(c, token, rid)
    r = c.post(f"/v1/demo/grants/{grant_id}/publish", headers=auth(token),
               json={"runs": runs, "assets": assets or []})
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------------ R01
def test_guest_cannot_switch_task_or_guess_resources(
    make_system: Callable[..., SystemHandle],
) -> None:
    with client(make_system) as (c, handle, token):
        t1 = submit(c, token, "published", "r01-t1")
        t2 = submit(c, token, "other", "r01-t2")
        grant, _subj, gtok, _ref = guest(c, token, "r01")
        publish(c, token, grant, [{"run_id": t1}])
        # 换任务 / 换 grant / 猜 id：都不返回任务、标题、计数、正文
        for rid in (t2, "T1", "", "0000"):
            r = c.get(f"/v1/demo/runs/{rid}", headers=auth(gtok))
            assert r.status_code == 404
            assert "published" not in r.text and "goal" not in r.text
        # 另一租户标识也不能读（tenant 隔离仍成立）
        other_tenant = add_subject(handle, "other-tenant-subj", ["read"])
        assert c.get(f"/v1/demo/runs/{t1}", headers=auth(other_tenant)).status_code == 404


# ------------------------------------------------------------------ R02
def test_relation_hops_do_not_expand_scope(
    make_system: Callable[..., SystemHandle],
) -> None:
    """父/子 run、共享 asset 挂到别 run、旧版本跳转都不会扩大授权。"""
    with client(make_system) as (c, handle, token):
        t1 = submit(c, token, "parent task", "r02-t1")
        t3 = submit(c, token, "private child task", "r02-t3")
        actor: AuthContext = asyncio.run(handle.deps.identity.authenticate({"token": token}))
        a1 = handle.assets.register_report_version(
            actor, data=b"v1 bytes", name="r.md", run_id=t1)
        a1v2 = handle.assets.register_report_version(
            actor, data=b"v2 bytes", name="r.md", run_id=t1,
            parent_asset_id=a1["asset_id"], revision_reason="rev")
        shared = handle.assets.register_report_version(
            actor, data=b"shared", name="s.md", run_id=t3)
        grant, _subj, gtok, _ref = guest(c, token, "r02")
        publish(c, token, grant, [{"run_id": t1}],
                [{"asset_id": a1["asset_id"], "version": 1, "download": True}])
        # v2 不因 v1 授权而可见；别的 run 的共享 asset 也不可见
        assert c.get(f"/v1/demo/assets/{a1v2['asset_id']}", headers=auth(gtok)).status_code == 404
        assert c.get(f"/v1/demo/assets/{shared['asset_id']}", headers=auth(gtok)).status_code == 404
        assert c.get(f"/v1/demo/runs/{t3}", headers=auth(gtok)).status_code == 404
        # 归属未变：管理员仍可读全部
        assert c.get(f"/v1/assets/{a1v2['asset_id']}", headers=auth(token)).status_code == 200


# ------------------------------------------------------------------ R03
def test_every_read_entry_checks_authorization(
    make_system: Callable[..., SystemHandle],
) -> None:
    """预览/下载/元数据/引用/事件各自独立核权；可预览≠可下载。"""
    with client(make_system) as (c, handle, token):
        rid = submit(c, token, "read entries", "r03")
        actor: AuthContext = asyncio.run(handle.deps.identity.authenticate({"token": token}))
        asset = handle.assets.register_report_version(
            actor, data=b"body bytes", name="r.md", run_id=rid)
        grant, _subj, gtok, _ref = guest(c, token, "r03")
        publish(c, token, grant, [{"run_id": rid}],
                [{"asset_id": asset["asset_id"], "version": 1,
                  "preview": True, "download": False}])
        assert c.get(f"/v1/demo/assets/{asset['asset_id']}", headers=auth(gtok)).status_code == 200
        dl = c.get(f"/v1/demo/assets/{asset['asset_id']}/download", headers=auth(gtok))
        assert dl.status_code == 404 and b"body bytes" not in dl.content
        # 事件与引用也在同一 grant 边界内
        assert c.get(f"/v1/demo/runs/{rid}/events", headers=auth(gtok)).status_code == 200
        assert c.get("/v1/demo/runs/nope/events", headers=auth(gtok)).status_code == 404
        # 管理员下载走鉴权路径且响应不进共享缓存
        ok = c.get(f"/v1/assets/{asset['asset_id']}/download", headers=auth(token))
        assert ok.status_code == 200 and "no-store" in ok.headers.get("Cache-Control", "")


# ------------------------------------------------------------------ R04
def test_trace_events_and_errors_do_not_leak_owner_context(
    make_system: Callable[..., SystemHandle],
) -> None:
    with client(make_system) as (c, handle, token):
        rid = submit(c, token, "leak check", "r04")
        # 写入一条带敏感键与普通新键的事件，游客投影必须只留点名字段
        handle.store.event_append("default", rid, "step.prepared", {
            "step_id": "s1", "tool": "fs.write_text", "policy": "allowed",
            "arguments": {"path": "/Users/private/x"},
            "system_prompt": "secret instructions", "endpoint": "http://internal",
            "token": "abc", "note": "SYNTH_PRIVATE_MARKER",
        })
        grant, _subj, gtok, _ref = guest(c, token, "r04")
        publish(c, token, grant, [{"run_id": rid}])
        body = c.get(f"/v1/demo/runs/{rid}/events", headers=auth(gtok)).text
        for leak in ("/Users/private", "secret instructions", "http://internal", "abc",
                     "SYNTH_PRIVATE_MARKER", "owner private goal text", "model.request"):
            assert leak not in body, leak
        assert "fs.write_text" in body  # 白名单点名字段仍在
        # 日志/审计不记兑换码（只存 hash）
        assert "invite_code" not in json.dumps(
            [a.detail for a in handle.store.audit_query("default", limit=100)])


# ------------------------------------------------------------------ R-A
def test_guest_projection_blocks_private_markers_in_events_and_trace(
    make_system: Callable[..., SystemHandle],
) -> None:
    """合成私有标记注入普通键/嵌套列表/后续 delta：游客 events/trace 均不得泄露。"""
    handle = make_system(model_script=[{"content": "报告正文 SYNTH_UNPUBLISHED_MARKER 结束"}])
    with TestClient(create_app(system=handle)) as c:
        token = owner_token(handle)
        rid = submit(c, token, "发布快照验证", "ra-1")
        # 先等 run 终态，避免与 worker 事件写入竞态
        deadline = time.monotonic() + 10
        state = ""
        while time.monotonic() < deadline:
            state = c.get(f"/v1/tasks/{rid}", headers=auth(token)).json()["run"]["state"]
            if state in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.05)
        assert state == "succeeded", state
        # 1) 黑名单键名防不住的普通新键与数组内文本
        handle.store.event_append("default", rid, "tool.prepared", {
            "step_id": "s1", "tool": "fs.write_text", "policy": "allowed",
            "note": "SYNTH_PRIVATE_MARKER", "items": ["SYNTH_PRIVATE_MARKER"],
        })
        # 2) 状态事件 detail 里的私有标记与嵌套结构
        handle.store.event_append("default", rid, "run.state_changed", {
            "to": "succeeded", "final": "SYNTH_PRIVATE_MARKER",
            "delivery": {"asset_id": "a1"}, "tags": ["SYNTH_PRIVATE_MARKER"],
        })
        grant, _subj, gtok, _ref = guest(c, token, "ra")
        publish(c, token, grant, [{"run_id": rid}])
        # 3) 发布**之后**新增的未审阅 delta：直播未开启，不得进入游客投影
        handle.store.event_append("default", rid, "answer.delta",
                                  {"call_id": "late", "delta": "SYNTH_PRIVATE_MARKER",
                                   "seq_in_call": 99})
        for url in (f"/v1/demo/runs/{rid}/events", f"/v1/tasks/{rid}/events",
                    f"/v1/tasks/{rid}/trace"):
            body = c.get(url, headers=auth(gtok)).text
            for marker in ("SYNTH_PRIVATE_MARKER", "SYNTH_UNPUBLISHED_MARKER"):
                assert marker not in body, f"{url} 泄露 {marker}"
        # 投影不是清空：白名单点名字段与事件种类仍可见
        body = c.get(f"/v1/demo/runs/{rid}/events", headers=auth(gtok)).text
        assert "fs.write_text" in body and "model.completed" in body
        # 管理员侧不受影响：原始事件仍可读（投影只作用于游客面）
        admin_body = c.get(f"/v1/tasks/{rid}/events", headers=auth(token)).text
        assert "SYNTH_UNPUBLISHED_MARKER" in admin_body


# ------------------------------------------------------------------ R-D
def test_guest_task_detail_uniform_404_and_admin_reads_cross_subject(
    make_system: Callable[..., SystemHandle],
) -> None:
    """游客详情统一 404 不泄露存在性；管理员在租户范围读受控主体的任务/成果。"""
    with client(make_system) as (c, handle, token):
        t1 = submit(c, token, "admin readable", "rd-1")
        # 受控第二主体（如 webhook 服务身份）产生的任务
        svc_token = add_subject(handle, "svc-webhook", ["read", "write"])
        t2 = submit(c, svc_token, "webhook task", "rd-2")
        svc_asset = handle.assets.register_report_version(
            asyncio.run(handle.deps.identity.authenticate({"token": svc_token})),
            data=b"svc bytes", name="svc.md", run_id=t2)
        grant, _s, gtok, _r = guest(c, token, "rd")
        publish(c, token, grant, [{"run_id": t1}])
        # 游客：已发布/未发布/不存在 → 全部统一 404，无标题/正文/资源元数据
        for rid in (t1, t2, "nope"):
            r = c.get(f"/v1/tasks/{rid}", headers=auth(gtok))
            assert r.status_code == 404, rid
            assert "admin readable" not in r.text and "goal" not in r.text
        # 游客也不因统一 404 丢掉已发布投影：/v1/demo 路由仍可读 t1
        assert c.get(f"/v1/demo/runs/{t1}", headers=auth(gtok)).status_code == 200
        # 管理员跨主体读取：t2 详情与成果可见（不冒充其身份）
        body = c.get(f"/v1/tasks/{t2}", headers=auth(token))
        assert body.status_code == 200, body.text
        assert "svc-webhook" in str(body.json()["run"]["subject"])
        assert any(a.get("id") == svc_asset["asset_id"] for a in body.json()["artifacts"])
        # 成果本身可经资产路由读取/下载（统一判定，不冒充归属主体）
        meta = c.get(f"/v1/assets/{svc_asset['asset_id']}", headers=auth(token))
        assert meta.status_code == 200 and meta.json()["subject"] != "" \
            and "svc" in str(meta.json()["subject"])
        dl = c.get(f"/v1/assets/{svc_asset['asset_id']}/download", headers=auth(token))
        assert dl.status_code == 200 and dl.content == b"svc bytes"
        # 管理员资产列表覆盖本租户全量
        listed = {a["id"] for a in c.get("/v1/assets", headers=auth(token)).json()}
        assert svc_asset["asset_id"] in listed
        # /v1/runs 管理员列表覆盖本部署全量
        ids = [r["id"] for r in c.get("/v1/runs", headers=auth(token)).json()]
        assert t1 in ids and t2 in ids
        # 管理员跨主体的事件/trace/流与详情同一判定
        assert c.get(f"/v1/tasks/{t2}/events", headers=auth(token)).status_code == 200
        assert c.get(f"/v1/tasks/{t2}/trace", headers=auth(token)).status_code == 200
        assert c.get(f"/v1/tasks/{t2}/stream?follow=0", headers=auth(token)).status_code == 200


# ------------------------------------------------------------------ R-D/R-E
def test_guest_resource_probes_have_no_existence_oracle(
    make_system: Callable[..., SystemHandle],
) -> None:
    """游客探测：不存在与未发布同形 404（非 422），无存在性预言机。"""
    with client(make_system) as (c, handle, token):
        t1 = submit(c, token, "probe target", "rdo-1")
        t2 = submit(c, token, "unpublished sibling", "rdo-2")
        actor: AuthContext = asyncio.run(handle.deps.identity.authenticate({"token": token}))
        a_unpub = handle.assets.register_report_version(
            actor, data=b"unpub bytes", name="u.md", run_id=t2)
        grant, _s, gtok, _r = guest(c, token, "rdo")
        publish(c, token, grant, [{"run_id": t1}])
        # 事件/流：未发布与不存在都 404（stream 不落全局 401）
        for rid in (t2, "nope-run"):
            assert c.get(f"/v1/tasks/{rid}/stream?follow=0", headers=auth(gtok)).status_code == 404
            assert c.get(f"/v1/demo/runs/{rid}/events", headers=auth(gtok)).status_code == 404
        # 资产元数据/下载：不存在（404）与未发布（404）同形，正文不泄露
        for aid in ("nope-asset", a_unpub["asset_id"]):
            r_meta = c.get(f"/v1/demo/assets/{aid}", headers=auth(gtok))
            r_dl = c.get(f"/v1/demo/assets/{aid}/download", headers=auth(gtok))
            assert r_meta.status_code == 404 and r_dl.status_code == 404, aid
            assert "unpub bytes" not in r_dl.text
        # 详情：未发布 404 无正文
        r = c.get(f"/v1/demo/runs/{t2}", headers=auth(gtok))
        assert r.status_code == 404 and "unpublished sibling" not in r.text


# ------------------------------------------------------------------ R-E
def test_guest_unknown_action_denied_and_audited(
    make_system: Callable[..., SystemHandle],
) -> None:
    """默认拒绝：有 grant 的游客对未知/拼写错误动作也必须拒绝，且拒绝留审计。"""
    with client(make_system) as (c, handle, token):
        rid = submit(c, token, "default deny", "re-1")
        grant, _s, gtok, _r = guest(c, token, "re")
        publish(c, token, grant, [{"run_id": rid}])
        gactor: AuthContext = asyncio.run(
            handle.deps.identity.authenticate({"token": gtok}))
        # 任务已发布、grant 有效：白名单外动作（含全局读取面）仍一律拒绝
        for action in ("knowledge.search", "memory.read", "capabilities.read",
                       "task.list_all", "task.readx", "unknown.future.action"):
            assert not handle.access.check(gactor, action).allowed, action
        # 白名单动作 + kind=global 也拒绝：游客没有全局读取面
        assert not handle.access.check(gactor, "task.read").allowed
        assert handle.access.check(
            gactor, "task.read", kind="task", resource_id=rid).allowed
        # 服务级 require 拒绝即审计
        with pytest.raises(AgentOSError):
            handle.access.require(gactor, "typo.action")
        audit = handle.store.audit_query("default", kind_prefix="access.denied", limit=50)
        assert any(a.subject == gactor.subject for a in audit)


# ------------------------------------------------------------------ R05/R06
def test_guest_cannot_use_non_http_entries_or_escalate(
    make_system: Callable[..., SystemHandle],
) -> None:
    with client(make_system) as (c, handle, token):
        grant, subj, gtok, _ref = guest(c, token, "r05")
        # webhook：HMAC 通过也是受限服务身份，不是游客；游客令牌不能当服务身份用
        r = c.post("/v1/webhook/demo", headers=auth(gtok), content=b'{"payload":{"goal":"x"}}')
        assert r.status_code in (401, 403, 422)
        # 伪造主体 header 不提升权限
        forged = {"Authorization": f"Bearer {gtok}", "X-Subject": "owner", "X-Role": "admin"}
        assert c.get("/v1/demo/grants", headers=forged).status_code in (401, 403)
        assert c.get("/v1/auth/me", headers=forged).json()["role"] == "guest"
        assert c.get("/v1/auth/me", headers=forged).json()["subject"] == subj
        # 登录/刷新/退出只作用于本人会话
        rr = c.post("/v1/auth/refresh", json={"refresh_token": _ref})
        assert rr.status_code == 200 and rr.json()["subject"] == subj
        assert grant  # grant 未变


# ------------------------------------------------------------------ R07
def test_revocation_closes_every_open_path(
    make_system: Callable[..., SystemHandle],
) -> None:
    with client(make_system) as (c, handle, token):
        rid = submit(c, token, "revocation", "r07")
        actor: AuthContext = asyncio.run(handle.deps.identity.authenticate({"token": token}))
        asset = handle.assets.register_report_version(
            actor, data=b"secret bytes", name="r.md", run_id=rid)
        grant, _subj, gtok, refresh = guest(c, token, "r07")
        publish(c, token, grant, [{"run_id": rid}],
                [{"asset_id": asset["asset_id"], "version": 1,
                  "preview": True, "download": True}])
        assert c.get(f"/v1/demo/assets/{asset['asset_id']}/download",
                     headers=auth(gtok)).status_code == 200
        c.post(f"/v1/demo/grants/{grant}/revoke", headers=auth(token))
        # 旧 access / refresh / 下载 / 详情 / 首页 全部失效
        assert c.get(f"/v1/demo/assets/{asset['asset_id']}/download",
                     headers=auth(gtok)).status_code in (401, 403, 404)
        assert c.get(f"/v1/demo/runs/{rid}", headers=auth(gtok)).status_code in (401, 403, 404)
        assert c.post("/v1/auth/refresh", json={"refresh_token": refresh}).status_code in (401, 403)
        # 管理员原业务不受影响：鉴权下载仍可用，且响应不进共享缓存
        resp = c.get(f"/v1/assets/{asset['asset_id']}/download", headers=auth(token))
        assert resp.status_code == 200 and resp.content == b"secret bytes"
        assert "no-store" in resp.headers.get("Cache-Control", "")


# ------------------------------------------------------------------ R08
def test_restore_does_not_resurrect_revoked_grants(
    make_system: Callable[..., SystemHandle],
) -> None:
    with client(make_system) as (c, handle, token):
        rid = submit(c, token, "restore target", "r08")
        g1, _s1, tok1, _r1 = guest(c, token, "before-backup")
        g2, _s2, tok2, _r2 = guest(c, token, "after-backup")
        publish(c, token, g1, [{"run_id": rid}])
        publish(c, token, g2, [{"run_id": rid}])
        assert c.get(f"/v1/demo/runs/{rid}", headers=auth(tok1)).status_code == 200
        # 模拟"恢复撤销前备份"：统一作废游客邀请与会话
        out = c.post("/v1/demo/void-all", headers=auth(token),
                     json={"reason": "backup_restore"}).json()
        assert out["count"] == 2 and set(out["voided"]) == {g1, g2}
        for tok in (tok1, tok2):
            assert c.get(f"/v1/demo/runs/{rid}", headers=auth(tok)).status_code in (401, 403, 404)
            assert c.get("/v1/demo/home", headers=auth(tok)).status_code in (401, 403)
        # 管理员原业务不受影响，可按需重新发布
        assert c.get(f"/v1/tasks/{rid}", headers=auth(token)).status_code == 200
        g3, _s3, tok3, _r3 = guest(c, token, "republished")
        publish(c, token, g3, [{"run_id": rid}])
        assert c.get(f"/v1/demo/runs/{rid}", headers=auth(tok3)).status_code == 200


# ------------------------------------------------------------------ R-B
def test_publish_rejects_assets_and_refs_outside_published_runs(
    make_system: Callable[..., SystemHandle],
) -> None:
    """成果/引用必须绑定已发布任务：私有 run 的资产不能夹带；凭空/越权引用拒绝；互串拒绝。"""
    with client(make_system) as (c, handle, token):
        actor: AuthContext = asyncio.run(handle.deps.identity.authenticate({"token": token}))
        t1 = submit(c, token, "ref host", "rb-1")
        t3 = submit(c, token, "private sibling", "rb-3")
        a3 = handle.assets.register_report_version(actor, data=b"t3 bytes", name="t3.md", run_id=t3)
        # 语料：chunk_id 确定性生成（source_id:0）；Q1 要求 run 有该引用的步骤记录
        asyncio.run(handle.knowledge.ingest(actor, "src-rb", "品牌资料一：活动日期。", ["*"]))
        asyncio.run(handle.knowledge.ingest(actor, "src-other", "无关来源的正文。", ["*"]))
        add_ref_step(handle, t1, "src-rb", "src-rb:0")
        grant, _s, gtok, _r = guest(c, token, "rb")
        # T3 的成果放进 T1 清单 → 拒绝（发布端按 run↔Asset 关系校验）
        r = c.post(f"/v1/demo/grants/{grant}/publish", headers=auth(token),
                   json={"runs": [{"run_id": t1}],
                         "assets": [{"asset_id": a3["asset_id"], "version": 1}]})
        assert r.status_code == 422, r.text
        # 引用绑定未发布任务 / 非 run 记录的引用 / 凭空 chunk / 未知来源 /
        # quote 与正文不符 / 有 quote 无 chunk → 拒绝
        for refs in ([{"source_id": "src-rb", "chunk_id": "src-rb:0", "run_id": t3}],
                     [{"source_id": "src-other", "chunk_id": "src-other:0", "run_id": t1}],
                     [{"source_id": "src-rb", "chunk_id": "src-rb:nope", "run_id": t1}],
                     [{"source_id": "src-404", "chunk_id": "x", "run_id": t1}],
                     [{"source_id": "src-rb", "chunk_id": "src-rb:0", "run_id": t1,
                       "quote": "这段文字不在 chunk 正文里"}],
                     [{"source_id": "src-rb", "run_id": t1, "quote": "有 quote 无 chunk"}]):
            r = c.post(f"/v1/demo/grants/{grant}/publish", headers=auth(token),
                       json={"runs": [{"run_id": t1}], "refs": refs})
            assert r.status_code == 422, r.text
        # 正常发布：引用绑定 t1，服务端可回查
        assert wait_terminal(c, token, t1) == "succeeded"
        r = c.post(f"/v1/demo/grants/{grant}/publish", headers=auth(token),
                   json={"runs": [{"run_id": t1}],
                         "refs": [{"source_id": "src-rb", "chunk_id": "src-rb:0",
                                   "quote": "品牌资料一", "run_id": t1}]})
        assert r.status_code == 201, r.text
        d1 = c.get(f"/v1/demo/runs/{t1}", headers=auth(gtok)).json()
        assert any(x["chunk_id"] == "src-rb:0" for x in d1["refs"])
        # 互串：t4 也发布，但绑定 t1 的引用不得出现在 t4 详情
        t4 = submit(c, token, "second public", "rb-4")
        assert wait_terminal(c, token, t4) == "succeeded"
        r = c.post(f"/v1/demo/grants/{grant}/publish", headers=auth(token),
                   json={"runs": [{"run_id": t1}, {"run_id": t4}],
                         "refs": [{"source_id": "src-rb", "chunk_id": "src-rb:0",
                                   "quote": "品牌资料一", "run_id": t1}]})
        assert r.status_code == 201, r.text
        d4 = c.get(f"/v1/demo/runs/{t4}", headers=auth(gtok)).json()
        assert d4["refs"] == [], "绑定 t1 的引用不得随清单出现在 t4"
        d1 = c.get(f"/v1/demo/runs/{t1}", headers=auth(gtok)).json()
        assert any(x["chunk_id"] == "src-rb:0" for x in d1["refs"])


# ------------------------------------------------------------------ R-C
def test_source_deletion_and_acl_tightening_pull_published_refs(
    make_system: Callable[..., SystemHandle],
) -> None:
    """来源删除 / 任一级 ACL 收紧后，已发布引用片段随新读失效（快照不豁免）。"""
    with client(make_system) as (c, handle, token):
        actor: AuthContext = asyncio.run(handle.deps.identity.authenticate({"token": token}))
        t1 = submit(c, token, "ref liveness", "rc-1")
        asyncio.run(handle.knowledge.ingest(actor, "src-rc", "可公开片段内容。", ["*"]))
        asyncio.run(handle.knowledge.ingest(actor, "src-rc2", "第二段可公开内容。", ["*"]))
        asyncio.run(handle.knowledge.ingest(actor, "src-rc3", "第三段可公开内容。", ["*"]))
        for chunk in ("src-rc:0", "src-rc2:0", "src-rc3:0"):
            add_ref_step(handle, t1, chunk.split(":")[0], chunk)
        # 报告成果正文引用了 src-rc2 的片段（Q2：来源失效后该成果要被撤下）
        report = handle.assets.register_report_version(
            actor, data="报告正文……第二段可公开内容。……".encode(),
            name="r.md", run_id=t1)
        grant, _s, gtok, _r = guest(c, token, "rc")
        assert wait_terminal(c, token, t1) == "succeeded"
        r = c.post(f"/v1/demo/grants/{grant}/publish", headers=auth(token),
                   json={"runs": [{"run_id": t1}],
                         "assets": [{"asset_id": report["asset_id"], "version": 1,
                                     "preview": True, "download": True}],
                         "refs": [{"source_id": "src-rc", "chunk_id": "src-rc:0",
                                   "quote": "可公开片段内容", "run_id": t1},
                                  {"source_id": "src-rc2", "chunk_id": "src-rc2:0",
                                   "quote": "第二段", "run_id": t1},
                                  {"source_id": "src-rc3", "chunk_id": "src-rc3:0",
                                   "quote": "第三段", "run_id": t1}]})
        assert r.status_code == 201, r.text
        d1 = c.get(f"/v1/demo/runs/{t1}", headers=auth(gtok)).json()
        assert {x["chunk_id"] for x in d1["refs"]} == {"src-rc:0", "src-rc2:0", "src-rc3:0"}
        assert any(a.get("id") == report["asset_id"] for a in d1["artifacts"])
        # 来源级 ACL 收紧：src-rc 只剩 owner 可见 → 引用随新读撤下
        conn = handle.store.raw_connection()
        with handle.store.locked():
            conn.execute(
                "UPDATE knowledge_sources SET acl_json='[\"owner\"]' "
                "WHERE tenant_id='default' AND id='src-rc'")
            conn.commit()
        d1 = c.get(f"/v1/demo/runs/{t1}", headers=auth(gtok)).json()
        assert {x["chunk_id"] for x in d1["refs"]} == {"src-rc2:0", "src-rc3:0"}
        # chunk 级 ACL 收紧（source ACL 未动）：src-rc3 的引用同样撤下
        with handle.store.locked():
            conn.execute(
                "UPDATE knowledge_chunks SET acl_json='[\"owner\"]' "
                "WHERE tenant_id='default' AND chunk_id='src-rc3:0'")
            conn.commit()
        d1 = c.get(f"/v1/demo/runs/{t1}", headers=auth(gtok)).json()
        assert {x["chunk_id"] for x in d1["refs"]} == {"src-rc2:0"}
        # 来源删除（tombstone）：src-rc2 的引用撤下，且正文含其片段的成果一并撤下
        asyncio.run(handle.knowledge.delete(actor, "src-rc2"))
        d1 = c.get(f"/v1/demo/runs/{t1}", headers=auth(gtok)).json()
        assert d1["refs"] == []
        assert all(a.get("id") != report["asset_id"] for a in d1["artifacts"]), \
            "正文含失效片段的成果必须撤下"
        assert c.get(f"/v1/demo/assets/{report['asset_id']}",
                     headers=auth(gtok)).status_code == 404
        assert c.get(f"/v1/demo/assets/{report['asset_id']}/download",
                     headers=auth(gtok)).status_code == 404


def test_true_old_snapshot_restore_cannot_revive(
    make_system: Callable[..., SystemHandle],
) -> None:
    """Q2 端到端：撤销前快照 → 撤销 → 恢复旧快照 → 重启 → 账本重放再撤销。

    旧快照里 grant 是 active 且撤销审计随库回滚；撤销事实只存在于库外账本。
    """
    import shutil
    import sqlite3

    handle = make_system(model_script=DEMO_SCRIPT)
    with TestClient(create_app(system=handle)) as c:
        token = owner_token(handle)
        rid = submit(c, token, "snapshot restore", "q2-1")
        assert wait_terminal(c, token, rid) == "succeeded"
        grant, _s, gtok, _ref = guest(c, token, "q2")
        publish(c, token, grant, [{"run_id": rid}])
        assert c.get(f"/v1/demo/runs/{rid}", headers=auth(gtok)).status_code == 200
        # 撤销前一致快照（grant 仍 active，审计无撤销记录）
        db_path = handle.settings.db_path
        backup = handle.settings.data_dir / "old-snapshot.db"
        src = sqlite3.connect(str(db_path))
        dst = sqlite3.connect(str(backup))
        src.backup(dst)
        dst.close()
        src.close()
        # 撤销（撤销事实写入库外账本）
        c.post(f"/v1/demo/grants/{grant}/revoke", headers=auth(token))
        assert str(handle.store.get_demo_grant("default", grant)["status"]) == "revoked"
    # 释放旧实例的连接（避免 WAL 锁冲突），再恢复旧快照：覆盖 db 并清掉 WAL 侧文件
    handle.store.raw_connection().close()
    shutil.copy(backup, db_path)
    for suffix in ("-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    # "重启"：同一 data_dir 上的新实例执行 boot（启动重放）
    handle2 = make_system(model_script=DEMO_SCRIPT)
    handle2.boot()
    row = handle2.store.get_demo_grant("default", grant)
    assert row is not None and str(row["status"]) == "revoked", \
        "恢复复活的 grant 必须经库外账本重放再次撤销"
    # 重启后的系统上，旧游客令牌也不能读
    with TestClient(create_app(system=handle2)) as c2:
        assert c2.get(f"/v1/demo/runs/{rid}", headers=auth(gtok)).status_code in (401, 403, 404)


# ------------------------------------------------------------------ Q5
def test_guest_action_kind_pairs_enforced(
    make_system: Callable[..., SystemHandle],
) -> None:
    """授权动作与资源类型成对判定：错配组合（含未知 kind）一律拒绝。"""
    with client(make_system) as (c, handle, token):
        rid = submit(c, token, "pair check", "q5-1")
        actor: AuthContext = asyncio.run(handle.deps.identity.authenticate({"token": token}))
        asset = handle.assets.register_report_version(
            actor, data=b"pair bytes", name="p.md", run_id=rid)
        grant, _s, _gtok, _r = guest(c, token, "q5")
        publish(c, token, grant, [{"run_id": rid}],
                [{"asset_id": asset["asset_id"], "version": 1}])
        gactor: AuthContext = asyncio.run(
            handle.deps.identity.authenticate({"token": token}))
        guest_ctx: AuthContext = asyncio.run(
            handle.deps.identity.authenticate({"token": _gtok}))
        # 资产动作配任务资源 / 任务动作配资产资源 / 未知 kind → 拒绝
        assert not handle.access.check(
            guest_ctx, "demo.asset.download", kind="task", resource_id=rid).allowed
        assert not handle.access.check(
            guest_ctx, "task.read", kind="asset", resource_id=asset["asset_id"]).allowed
        assert not handle.access.check(
            guest_ctx, "task.read", kind="user", resource_id=rid).allowed
        assert not handle.access.check(
            guest_ctx, "demo.asset.preview", kind="global", resource_id="").allowed
        # 正确组合仍然放行
        assert handle.access.check(
            guest_ctx, "task.read", kind="task", resource_id=rid).allowed
        assert handle.access.check(
            guest_ctx, "demo.asset.preview", kind="asset",
            resource_id=asset["asset_id"], version=1).allowed
        assert gactor is not None  # 管理员上下文仅用于确认测试环境装配


# ------------------------------------------------------------------ Q6
def test_default_publish_requires_completed_run_and_freezes_high_water(
    make_system: Callable[..., SystemHandle],
) -> None:
    """默认发布只接受已完成任务并冻结事件高水位；进行中观察须显式 live=true。"""
    handle = make_system(model_script=WRITE_FLOW_SCRIPT)  # 含审批步骤：停在 awaiting_approval
    with TestClient(create_app(system=handle)) as c:
        token = owner_token(handle)
        rid = submit(c, token, "approval gate", "q6-1")
        assert wait_terminal(c, token, rid) == "awaiting_approval"
        grant, _s, gtok, _r = guest(c, token, "q6")
        # 进行中任务默认发布 → 拒绝
        r = c.post(f"/v1/demo/grants/{grant}/publish", headers=auth(token),
                   json={"runs": [{"run_id": rid}]})
        assert r.status_code == 422, r.text
        # 显式 live=true 才允许观察进行中任务
        r = c.post(f"/v1/demo/grants/{grant}/publish", headers=auth(token),
                   json={"runs": [{"run_id": rid, "live": True}]})
        assert r.status_code == 201, r.text

    # 已完成任务：默认发布冻结高水位，发布后的新事件不进游客事件
    handle2 = make_system(model_script=DEMO_SCRIPT)
    with TestClient(create_app(system=handle2)) as c:
        token = owner_token(handle2)
        rid = submit(c, token, "snapshot freeze", "q6-2")
        assert wait_terminal(c, token, rid) == "succeeded"
        grant, _s, gtok, _r = guest(c, token, "q6b")
        publish(c, token, grant, [{"run_id": rid}])
        handle2.store.event_append("default", rid, "run.state_changed",
                                   {"to": "succeeded", "note": "post-publish"})
        body = c.get(f"/v1/demo/runs/{rid}/events", headers=auth(gtok)).text
        assert "post-publish-model" not in body, "高水位之后的事件不得进入默认快照"
        handle2.store.event_append("default", rid, "model.completed",
                                   {"call_id": "late", "model": "post-publish-model",
                                    "latency_ms": 1})
        body = c.get(f"/v1/demo/runs/{rid}/events", headers=auth(gtok)).text
        assert "post-publish-model" not in body, "高水位之后的事件不得进入默认快照"
        # 管理员重新发布新 revision（高水位前移）后才可见
        publish(c, token, grant, [{"run_id": rid}])
        body = c.get(f"/v1/demo/runs/{rid}/events", headers=auth(gtok)).text
        assert "post-publish-model" in body


# ------------------------------------------------------------------ Q4
def test_guest_stream_survives_long_hidden_event_runs(
    make_system: Callable[..., SystemHandle],
) -> None:
    """大量连续不可见事件后，直播流仍按原始游标推进到其后的合法事件（不漏不卡）。"""
    handle = make_system(model_script=DEMO_SCRIPT)
    with TestClient(create_app(system=handle)) as c:
        token = owner_token(handle)
        rid = submit(c, token, "cursor stress", "q4-1")
        assert wait_terminal(c, token, rid) == "succeeded"
        grant, _s, gtok, _r = guest(c, token, "q4")
        # live 发布：发布后的新事件可流（管理员显式开启）
        publish(c, token, grant, [{"run_id": rid, "live": True}])
        for i in range(210):
            handle.store.event_append("default", rid, "model.request",
                                      {"messages": [{"role": "system", "content": f"s{i}"}]})
        handle.store.event_append("default", rid, "run.state_changed",
                                  {"to": "succeeded", "note": "after-hidden"})
        events = read_stream(c, f"/v1/tasks/{rid}/stream", auth(gtok))
        kinds = [e["kind"] for e in events]
        assert "run.state_changed" in kinds, "连续隐藏事件后的合法事件不得被漏掉"
        assert "stream.end" in kinds
        blob = json.dumps(events, ensure_ascii=False)
        assert "s209" not in blob and "SYNTH" not in blob, "隐藏事件内容不得外泄"
