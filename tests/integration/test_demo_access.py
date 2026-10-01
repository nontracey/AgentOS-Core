"""U4-01 演示访问与统一资源授权（A 权限基准，HTTP 与服务两层都测）。

判据来自 tests/fixtures/u4/a_access_matrix.json（A01～A13）。拒绝时同时断言：
无数据正文泄露、无新 job/副作用、资源归属未变、审计有记录。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from fastapi.testclient import TestClient

from agentos.apps.composition import SystemHandle
from agentos.apps.gateway import create_app
from agentos.kernel.types import AuthContext
from tests.conftest import add_subject, owner_token

PRIVATE_MARKER = "PRIVATE-SENTINEL-42"

#: 演示测试用脚本：只读工具 + 最终答复（无审批步骤，run 可达终态，满足 Q6 默认发布）
DEMO_SCRIPT: list[dict[str, Any]] = [
    {"tool_calls": [{"id": "c1", "name": "math.add", "arguments": {"a": 2, "b": 3}}]},
    {"content": "done: sum=5 and report ready"},
]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@contextmanager
def client(make_system: Callable[..., SystemHandle]) -> Iterator[Any]:
    handle = make_system(model_script=DEMO_SCRIPT)
    app = create_app(system=handle)
    with TestClient(app) as c:
        yield c, handle, owner_token(handle)


def submit(c: TestClient, token: str, goal: str, key: str) -> str:
    r = c.post("/v1/tasks", headers=auth(token),
               json={"payload": {"goal": goal}, "idempotency_key": key})
    assert r.status_code == 202, r.text
    return r.json()["run_id"]


def upload(c: TestClient, token: str, text: str, name: str = "S1.txt") -> str:
    r = c.post("/v1/materials", json={"name": name, "content_text": text}, headers=auth(token))
    assert r.status_code in (200, 201), r.text
    return r.json()["asset_id"]


def make_guest(c: TestClient, handle: SystemHandle, token: str, label: str = "guest-a",
               ttl_hours: int = 24) -> tuple[str, str, str]:
    """管理员建 grant + 邀请；游客兑换。返回 (grant_id, guest_subject, guest_token)。"""
    r = c.post("/v1/demo/grants", headers=auth(token),
               json={"label": label, "ttl_hours": ttl_hours})
    assert r.status_code == 201, r.text
    grant_id, code = r.json()["grant_id"], r.json()["invite_code"]
    rr = c.post("/v1/auth/guest/redeem", json={"code": code})
    assert rr.status_code == 200, rr.text
    return grant_id, rr.json()["subject"], rr.json()["access_token"]


def publish(c: TestClient, token: str, grant_id: str, runs: list[dict[str, Any]],
            assets: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    # Q6：默认发布只接受已完成任务——助手先等各 run 到终态再发布
    for item in runs:
        rid = str(item.get("run_id") or "")
        if rid and not item.get("live"):
            wait_state(c, token, rid, {"succeeded", "failed", "cancelled"})
    r = c.post(f"/v1/demo/grants/{grant_id}/publish", headers=auth(token),
               json={"runs": runs, "assets": assets or []})
    assert r.status_code == 201, r.text
    return r.json()


def wait_state(c: TestClient, token: str, run_id: str, states: set[str], timeout: float = 8) -> str:
    deadline = time.monotonic() + timeout
    state = ""
    while time.monotonic() < deadline:
        state = c.get(f"/v1/tasks/{run_id}", headers=auth(token)).json()["run"]["state"]
        if state in states:
            return state
        time.sleep(0.05)
    return state


# ------------------------------------------------------------------ A01/A02/A05
def test_admin_keeps_full_access_and_role_is_admin(make_system: Callable[..., SystemHandle]) -> None:
    with client(make_system) as (c, handle, token):
        me = c.get("/v1/auth/me", headers=auth(token)).json()
        assert me["role"] == "admin" and me["subject"] == "owner"
        rid = submit(c, token, "admin baseline goal", "a01")
        assert c.get(f"/v1/tasks/{rid}", headers=auth(token)).status_code == 200
        # 同租户普通主体不能读管理员任务（既有归属规则继续成立：A13）
        peer = add_subject(handle, "peer", ["read"])
        assert c.get(f"/v1/tasks/{rid}", headers=auth(peer)).status_code in (401, 403, 404)


def test_guest_role_is_independent_subject(make_system: Callable[..., SystemHandle]) -> None:
    with client(make_system) as (c, handle, token):
        _grant, subject, gtok = make_guest(c, handle, token)
        assert subject.startswith("guest:")
        me = c.get("/v1/auth/me", headers=auth(gtok)).json()
        assert me["role"] == "guest" and me["subject"] == subject
        assert me["scopes"] == ["read"], "游客只有只读 scope"
        # 游客没有静态令牌：身份文件里没有可用 token（不能走 CLI/静态令牌路径）
        data = handle.settings.identity_file.read_text(encoding="utf-8")
        assert subject in data
        assert '"token": ""' in data


# ------------------------------------------------------------------ A10：guest 写拒绝
def test_guest_writes_rejected_without_side_effects(
    make_system: Callable[..., SystemHandle],
) -> None:
    with client(make_system) as (c, handle, token):
        rid = submit(c, token, "existing admin task", "a10")
        _grant, _subj, gtok = make_guest(c, handle, token)
        runs_before = len(c.get("/v1/runs", headers=auth(token)).json())
        jobs_before = len(handle.store.list_runs("default"))

        writes = [
            ("post", "/v1/tasks", {"payload": {"goal": "guest tries to run"}}),
            ("post", f"/v1/tasks/{rid}/cancel", {}),
            ("post", f"/v1/tasks/{rid}/reconcile", {"step_id": "s1"}),
            ("post", "/v1/materials", {"name": "x.txt", "content_text": "y"}),
            ("post", "/v1/workflows/w1/publish", {"nodes": []}),
            ("post", "/v1/workflows/w1/start", {"version": 1}),
            ("post", "/v1/nodes/pair", {"node_id": "n1", "endpoint": "http://x"}),
        ]
        for method, path, body in writes:
            r = c.post(path, headers=auth(gtok), json=body) if method == "post" else None
            assert r is not None and r.status_code in (401, 403, 404), (path, r.status_code)
            body = r.json() if "application/json" in r.headers.get("content-type", "") else {}
            assert "run_id" not in body

        assert len(c.get("/v1/runs", headers=auth(token)).json()) == runs_before, "游客写不能产生新 run"
        assert len(handle.store.list_runs("default")) == jobs_before, "不得有入队/副作用"
        # 拒绝必须留痕（审计有记录）
        audit = handle.store.audit_query("default", kind_prefix="access.denied", limit=50)
        assert any(a.subject and a.subject.startswith("guest:") for a in audit)


# ------------------------------------------------------------------ A02/A03/A04/A06
def test_guest_reads_only_published_runs(make_system: Callable[..., SystemHandle]) -> None:
    with client(make_system) as (c, handle, token):
        t1 = submit(c, token, "published task", "t1")
        t2 = submit(c, token, "second task", "t2")
        t3 = submit(c, token, "private task", "t3")
        grant_a, _sa, tok_a = make_guest(c, handle, token, "guest-a")
        publish(c, token, grant_a, [{"run_id": t1}])
        grant_b, _sb, tok_b = make_guest(c, handle, token, "guest-b")
        publish(c, token, grant_b, [{"run_id": t2}])

        home = c.get("/v1/demo/home", headers=auth(tok_a)).json()
        assert [r["id"] for r in home["runs"]] == [t1]

        assert c.get(f"/v1/demo/runs/{t1}", headers=auth(tok_a)).status_code == 200
        # A03：换任务（T2/T3）一律 404，不泄露标题/正文/存在性
        for rid in (t2, t3, "does-not-exist"):
            r = c.get(f"/v1/demo/runs/{rid}", headers=auth(tok_a))
            assert r.status_code == 404, (rid, r.status_code)
            assert t1 not in r.text and "goal" not in r.text
        # A04：guest-b 的清单不能给 guest-a 用
        assert c.get(f"/v1/demo/runs/{t2}", headers=auth(tok_a)).status_code == 404
        assert c.get(f"/v1/demo/runs/{t1}", headers=auth(tok_b)).status_code == 404
        # A06：列表/计数都过滤
        listed = c.get("/v1/runs", headers=auth(tok_a)).json()
        assert [r["id"] for r in listed] == [t1]


# ------------------------------------------------------------------ A07/A08/A09：逐资源/逐版本
def test_guest_asset_version_and_download_are_separate(
    make_system: Callable[..., SystemHandle],
) -> None:
    with client(make_system) as (c, handle, token):
        s1 = upload(c, token, "S1 授权材料内容", name="S1.txt")
        s2 = upload(c, token, f"S2 私有内容 {PRIVATE_MARKER}", name="S2.txt")
        rid = submit(c, token, "run that will own a report", "a07")

        # 用服务端注册一个 v1 成果与它的 v2 版本（不可变版本链）
        actor: AuthContext = asyncio.run(handle.deps.identity.authenticate({"token": token}))
        v1 = handle.assets.register_report_version(
            actor, data=b"# report v1\n", name="report.md", run_id=rid)
        v2 = handle.assets.register_report_version(
            actor, data=b"# report v2 private\n", name="report.md", run_id=rid,
            parent_asset_id=v1["asset_id"], revision_reason="修改")
        assert v2["version"] == 2

        grant, _subj, gtok = make_guest(c, handle, token)
        publish(c, token, grant, [{"run_id": rid}],
                [{"asset_id": v1["asset_id"], "version": 1, "preview": True, "download": False}])

        # A07：只发布了 v1 → v2 拒绝；未发布的 S1/S2 拒绝
        assert c.get(f"/v1/demo/assets/{v1['asset_id']}", headers=auth(gtok)).status_code == 200
        assert c.get(f"/v1/demo/assets/{v2['asset_id']}", headers=auth(gtok)).status_code == 404
        for denied in (s1, s2):
            assert c.get(f"/v1/demo/assets/{denied}", headers=auth(gtok)).status_code == 404
        # A08：可预览 ≠ 可下载
        dl = c.get(f"/v1/demo/assets/{v1['asset_id']}/download", headers=auth(gtok))
        assert dl.status_code == 404 and b"report v1" not in dl.content
        # A09：管理员显式开启下载后才允许，且字节与发布版本一致
        publish(c, token, grant, [{"run_id": rid}],
                [{"asset_id": v1["asset_id"], "version": 1, "preview": True, "download": True}])
        ok = c.get(f"/v1/demo/assets/{v1['asset_id']}/download", headers=auth(gtok))
        assert ok.status_code == 200 and ok.content == b"# report v1\n"
        assert ok.headers["X-Asset-Sha256"] == v1["sha256"]
        assert PRIVATE_MARKER not in ok.text


# ------------------------------------------------------------------ R04：投影不泄露
def test_guest_projection_has_no_private_fields(
    make_system: Callable[..., SystemHandle],
) -> None:
    with client(make_system) as (c, handle, token):
        rid = submit(c, token, "projection target", "r04")
        wait_state(c, token, rid, {"awaiting_approval", "succeeded", "failed"})
        grant, _subj, gtok = make_guest(c, handle, token)
        publish(c, token, grant, [{"run_id": rid}])
        detail = c.get(f"/v1/demo/runs/{rid}", headers=auth(gtok)).json()
        blob = str(detail).lower()
        for leak in ("path", "storage_path", "envelope_json", "checkpoint_json",
                     "system_prompt", "endpoint", "token", "secret"):
            assert leak not in blob, f"leaked {leak}"
        assert set(detail["run"].keys()) <= {
            "id", "state", "goal_text", "channel", "created_at", "updated_at", "goal",
        }
        events = c.get(f"/v1/demo/runs/{rid}/events", headers=auth(gtok)).json()["events"]
        for event in events:
            assert set(event.keys()) <= {"seq", "kind", "created_at", "run_id", "payload"}
            payload = str(event.get("payload", "")).lower()
            assert "system_prompt" not in payload and "checkpoint" not in payload
        # 游客不能读全局清单/管理接口
        assert c.get("/v1/demo/grants", headers=auth(gtok)).status_code in (401, 403)
        assert c.get("/v1/assets", headers=auth(gtok)).status_code in (401, 403)
        assert c.get("/v1/nodes", headers=auth(gtok)).status_code in (401, 403)


# ------------------------------------------------------------------ A11：兑换/刷新
def test_invite_single_use_and_refresh_keeps_guest_subject(
    make_system: Callable[..., SystemHandle],
) -> None:
    with client(make_system) as (c, handle, token):
        r = c.post("/v1/demo/grants", headers=auth(token),
                   json={"label": "once", "ttl_hours": 24})
        code = r.json()["invite_code"]
        first = c.post("/v1/auth/guest/redeem", json={"code": code})
        assert first.status_code == 200
        again = c.post("/v1/auth/guest/redeem", json={"code": code})
        assert again.status_code in (401, 403), "兑换码只能成功一次"
        assert c.post("/v1/auth/guest/redeem", json={"code": "bogus"}).status_code in (401, 403)

        refresh_token = first.json()["refresh_token"]
        subject = first.json()["subject"]
        rr = c.post("/v1/auth/refresh", json={"refresh_token": refresh_token})
        assert rr.status_code == 200, rr.text
        assert rr.json()["subject"] == subject and rr.json()["role"] == "guest"
        me = c.get("/v1/auth/me", headers=auth(rr.json()["access_token"])).json()
        assert me["role"] == "guest" and me["subject"] == subject, "refresh 不得签回 owner"
        # 旧 refresh 再用 = 重用检测：该主体会话全部撤销
        reuse = c.post("/v1/auth/refresh", json={"refresh_token": refresh_token})
        assert reuse.status_code in (401, 403)


# ------------------------------------------------------------------ A12：撤权
def test_revocation_stops_all_guest_reads(make_system: Callable[..., SystemHandle]) -> None:
    with client(make_system) as (c, handle, token):
        rid = submit(c, token, "revocation target", "a12")
        grant, _subj, gtok = make_guest(c, handle, token)
        published = publish(c, token, grant, [{"run_id": rid}])
        assert c.get(f"/v1/demo/runs/{rid}", headers=auth(gtok)).status_code == 200
        rv = c.post(f"/v1/demo/grants/{grant}/revoke", headers=auth(token))
        assert rv.status_code == 200 and rv.json()["status"] == "revoked"
        # 撤销后：详情/列表/首页/刷新全部失效
        assert c.get(f"/v1/demo/runs/{rid}", headers=auth(gtok)).status_code in (401, 403, 404)
        assert c.get("/v1/demo/home", headers=auth(gtok)).status_code in (401, 403)
        assert c.get("/v1/runs", headers=auth(gtok)).status_code in (401, 403)
        # 管理员原业务不受影响
        assert c.get(f"/v1/tasks/{rid}", headers=auth(token)).status_code == 200
        assert published["revision"] == 1


# ------------------------------------------------------------------ manifest 不可变
def test_manifest_revision_only_increases(make_system: Callable[..., SystemHandle]) -> None:
    with client(make_system) as (c, handle, token):
        t1 = submit(c, token, "rev1", "m1")
        grant, _subj, gtok = make_guest(c, handle, token)
        first = publish(c, token, grant, [{"run_id": t1}])
        t2 = submit(c, token, "rev2", "m2")
        second = publish(c, token, grant, [{"run_id": t1}, {"run_id": t2}])
        assert second["revision"] == first["revision"] + 1
        rows = handle.store.list_demo_manifests("default", grant)
        assert [r["revision"] for r in rows] == [1, 2]
        home = c.get("/v1/demo/home", headers=auth(gtok)).json()
        assert set(r["id"] for r in home["runs"]) == {t1, t2}
        # 旧 revision 不可改写（触发器保护）
        import sqlite3

        try:
            handle.store._conn.execute(  # type: ignore[attr-defined]
                "UPDATE demo_manifests SET payload_json='{}' WHERE id=?", (first["manifest_id"],))
            handle.store._conn.commit()  # type: ignore[attr-defined]
        except (sqlite3.OperationalError, sqlite3.IntegrityError) as exc:
            assert "immutable" in str(exc)
        else:  # pragma: no cover - 触发器存在时不该走到这里
            raise AssertionError("demo_manifests must be immutable")
