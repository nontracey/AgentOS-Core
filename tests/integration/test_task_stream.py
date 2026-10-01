"""U4-04 真流式与运行事实恢复：增量事件、游标续接、授权与撤权关闭。

用 scripted 模型验证**机制**：增量到达即持久化、游标不丢不重、撤权立刻断流。
真实模型流式（首增量到 UI）在 U4-08/U4-10 用真端点另记证据。
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from fastapi.testclient import TestClient

from agentos.apps.composition import SystemHandle
from agentos.apps.gateway import create_app
from tests.conftest import add_subject, owner_token

STREAM_SCRIPT = [{"content": "流式正文：合成内容，用于验证增量与游标。"}]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@contextmanager
def client(make_system: Callable[..., SystemHandle]) -> Iterator[Any]:
    handle = make_system(model_script=STREAM_SCRIPT)
    with TestClient(create_app(system=handle)) as c:
        yield c, handle, owner_token(handle)


def submit(c: TestClient, token: str, goal: str, key: str) -> str:
    r = c.post("/v1/tasks", headers=auth(token),
               json={"payload": {"goal": goal}, "idempotency_key": key})
    assert r.status_code == 202, r.text
    return r.json()["run_id"]


def wait_state(c: TestClient, token: str, run_id: str, states: set[str], timeout: float = 10) -> str:
    deadline = time.monotonic() + timeout
    state = ""
    while time.monotonic() < deadline:
        state = c.get(f"/v1/tasks/{run_id}", headers=auth(token)).json()["run"]["state"]
        if state in states:
            return state
        time.sleep(0.05)
    return state


def read_stream(c: TestClient, url: str, headers: dict[str, str], stop=("stream.end",),
                limit: int = 400) -> list[dict[str, Any]]:
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


# ------------------------------------------------------------------ 增量与持久化
def test_stream_persists_deltas_and_ends(make_system: Callable[..., SystemHandle]) -> None:
    with client(make_system) as (c, handle, token):
        rid = submit(c, token, "流式验证目标", "s04")
        assert wait_state(c, token, rid, {"succeeded", "failed"}) == "succeeded"
        events = read_stream(c, f"/v1/tasks/{rid}/stream", auth(token))
        kinds = [e["kind"] for e in events]
        assert "answer.start" in kinds and "answer.delta" in kinds and "answer.end" in kinds
        assert kinds[-1] == "stream.end"
        text = "".join(e["payload"]["delta"] for e in events if e["kind"] == "answer.delta")
        assert "流式正文" in text, "增量拼接应还原模型正文"
        # 正文结束已持久化：后续快照能拿到完整消息，不依赖流是否在线
        persisted = c.get(f"/v1/tasks/{rid}/events?cursor=0&limit=500", headers=auth(token)).json()
        done = [e for e in persisted["events"] if e["kind"] == "message.completed"]
        assert done and done[0]["payload"]["persisted"] is True
        assert done[0]["payload"]["chars"] == len(text)


# ------------------------------------------------------------------ 游标一致性
def test_cursor_resume_is_gap_free_and_duplicate_free(
    make_system: Callable[..., SystemHandle],
) -> None:
    with client(make_system) as (c, _, token):
        rid = submit(c, token, "游标验证", "s04b")
        assert wait_state(c, token, rid, {"succeeded", "failed"}) == "succeeded"
        # 分页读取：每次 2 条，拼出的 seq 序列必须严格递增且与一次性读取一致
        cursor, paged = 0, []
        while True:
            page = c.get(f"/v1/tasks/{rid}/events?cursor={cursor}&limit=2",
                         headers=auth(token)).json()
            if not page["events"]:
                break
            paged.extend(e["seq"] for e in page["events"])
            cursor = page["cursor"]
            if len(paged) > 200:
                break
        full = c.get(f"/v1/tasks/{rid}/events?cursor=0&limit=500", headers=auth(token)).json()
        whole = [e["seq"] for e in full["events"]]
        assert paged == whole, "分页结果与一次读取必须一致（不丢不重）"
        assert paged == sorted(set(paged)), "seq 严格递增且无重复"
        # 用同一 cursor 再取一次：不重复历史
        again = c.get(f"/v1/tasks/{rid}/events?cursor={whole[-1]}&limit=100",
                      headers=auth(token)).json()
        assert again["events"] == []


# ------------------------------------------------------------------ 授权
def test_stream_requires_identity_and_scope(make_system: Callable[..., SystemHandle]) -> None:
    with client(make_system) as (c, handle, token):
        rid = submit(c, token, "授权验证", "s04c")
        assert c.get(f"/v1/tasks/{rid}/stream").status_code == 401
        other = add_subject(handle, "other", ["read"])
        assert c.get(f"/v1/tasks/{rid}/stream", headers=auth(other)).status_code in (401, 403, 404)


# ------------------------------------------------------------------ 游客投影
def test_guest_stream_is_projected_and_closes_on_revocation(
    make_system: Callable[..., SystemHandle],
) -> None:
    with client(make_system) as (c, handle, token):
        rid = submit(c, token, "游客流验证", "s04d")
        assert wait_state(c, token, rid, {"succeeded", "failed"}) == "succeeded"
        created = c.post("/v1/demo/grants", headers=auth(token),
                         json={"label": "stream", "ttl_hours": 24}).json()
        gtok = c.post("/v1/auth/guest/redeem",
                      json={"code": created["invite_code"]}).json()["access_token"]
        c.post(f"/v1/demo/grants/{created['grant_id']}/publish", headers=auth(token),
               json={"runs": [{"run_id": rid}]})

        events = read_stream(c, f"/v1/tasks/{rid}/stream", auth(gtok))
        kinds = [e["kind"] for e in events]
        # R-A：默认发布快照不含正文流——delta 正文须管理员显式开启直播才可见
        assert "answer.delta" not in kinds and "answer.start" not in kinds
        assert "model.request" not in kinds, "提示词类事件不得进入游客投影"
        assert "stream.end" in kinds

        # 撤权后新连接立即拒绝
        c.post(f"/v1/demo/grants/{created['grant_id']}/revoke", headers=auth(token))
        resp = c.get(f"/v1/tasks/{rid}/stream", headers=auth(gtok))
        assert resp.status_code in (401, 403, 404)


def test_guest_answer_stream_requires_explicit_live_opt_in(
    make_system: Callable[..., SystemHandle],
) -> None:
    """R-A：直播须逐任务显式开启；开启后 delta 可见且只带点名字段。"""
    with client(make_system) as (c, handle, token):
        rid = submit(c, token, "直播开关验证", "s04g")
        assert wait_state(c, token, rid, {"succeeded", "failed"}) == "succeeded"
        g_default = c.post("/v1/demo/grants", headers=auth(token),
                           json={"label": "no-live", "ttl_hours": 24}).json()
        tok_default = c.post("/v1/auth/guest/redeem",
                             json={"code": g_default["invite_code"]}).json()["access_token"]
        c.post(f"/v1/demo/grants/{g_default['grant_id']}/publish", headers=auth(token),
               json={"runs": [{"run_id": rid}]})
        kinds_default = [e["kind"] for e in
                         read_stream(c, f"/v1/tasks/{rid}/stream", auth(tok_default))]
        assert "answer.delta" not in kinds_default, "未开启直播的 grant 不得收到正文流"

        g_live = c.post("/v1/demo/grants", headers=auth(token),
                        json={"label": "live", "ttl_hours": 24}).json()
        tok_live = c.post("/v1/auth/guest/redeem",
                          json={"code": g_live["invite_code"]}).json()["access_token"]
        c.post(f"/v1/demo/grants/{g_live['grant_id']}/publish", headers=auth(token),
               json={"runs": [{"run_id": rid, "live": True}]})
        events = read_stream(c, f"/v1/tasks/{rid}/stream", auth(tok_live))
        kinds_live = [e["kind"] for e in events]
        assert "answer.delta" in kinds_live, "显式开启直播后正文流可见"
        text = "".join(e["payload"]["delta"] for e in events if e["kind"] == "answer.delta")
        assert "流式正文" in text, "直播增量拼接应还原模型正文"
        assert all("call_id" not in e["payload"]
                   for e in events if e["kind"] == "answer.delta"), "直播只放行点名字段"


def test_guest_stream_blocks_unpublished_delta_text(
    make_system: Callable[..., SystemHandle],
) -> None:
    """R-A：正文与后续新增 delta 带合成私有标记时，游客 SSE 不得泄露。"""
    handle = make_system(model_script=[{"content": "私有正文 SYNTH_UNPUBLISHED_MARKER"}])
    with TestClient(create_app(system=handle)) as c:
        token = owner_token(handle)
        rid = submit(c, token, "SSE 标记验证", "s04f")
        assert wait_state(c, token, rid, {"succeeded", "failed"}) == "succeeded"
        created = c.post("/v1/demo/grants", headers=auth(token),
                         json={"label": "sse-marker", "ttl_hours": 24}).json()
        gtok = c.post("/v1/auth/guest/redeem",
                      json={"code": created["invite_code"]}).json()["access_token"]
        c.post(f"/v1/demo/grants/{created['grant_id']}/publish", headers=auth(token),
               json={"runs": [{"run_id": rid}]})
        # 发布之后追加的未审阅 delta：直播未开启，不得出现
        handle.store.event_append("default", rid, "answer.delta",
                                  {"call_id": "late", "delta": "SYNTH_PRIVATE_MARKER",
                                   "seq_in_call": 99})
        events = read_stream(c, f"/v1/tasks/{rid}/stream", auth(gtok))
        blob = json.dumps(events, ensure_ascii=False)
        assert "SYNTH_UNPUBLISHED_MARKER" not in blob
        assert "SYNTH_PRIVATE_MARKER" not in blob
        assert "stream.end" in [e["kind"] for e in events]


def test_open_guest_stream_closes_when_revoked(
    make_system: Callable[..., SystemHandle],
) -> None:
    """已打开的流：撤销后必须在发送下一批前复核并关闭（不是继续推数据）。"""
    with client(make_system) as (c, handle, token):
        rid = submit(c, token, "撤权中断流验证", "s04e")
        assert wait_state(c, token, rid, {"succeeded", "failed"}) == "succeeded"
        created = c.post("/v1/demo/grants", headers=auth(token),
                         json={"label": "mid-stream", "ttl_hours": 24}).json()
        gtok = c.post("/v1/auth/guest/redeem",
                      json={"code": created["invite_code"]}).json()["access_token"]
        c.post(f"/v1/demo/grants/{created['grant_id']}/publish", headers=auth(token),
               json={"runs": [{"run_id": rid}]})

        timer = threading.Timer(
            0.5,
            lambda: c.post(f"/v1/demo/grants/{created['grant_id']}/revoke", headers=auth(token)),
        )
        timer.start()
        events = read_stream(c, f"/v1/tasks/{rid}/stream?follow=1", auth(gtok),
                             stop=("access.revoked", "stream.end"), limit=300)
        timer.cancel()
        kinds = [e["kind"] for e in events]
        assert "access.revoked" in kinds or "stream.end" in kinds
        if "access.revoked" in kinds:
            assert kinds.index("access.revoked") == len(kinds) - 1, "撤权必须是最后一条"
