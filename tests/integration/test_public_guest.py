"""U5-01 游客为默认访问角色：免登进入只读演示空间（不要求访问码）。

判据来自本轮裁定与服务端契约：免登只放开"进入"，不放开任何权限——逐资源
白名单、只读、独立 subject、到期/关闭即时失效与名额上限继续由服务端裁定。
拒绝时同时断言：无正文泄露、无新 job/副作用、审计有记录。
"""

from __future__ import annotations

import asyncio
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
from tests.conftest import owner_token

#: 演示脚本：只读工具 + 最终答复（无审批步骤，run 可达终态，满足 Q6 默认发布）
DEMO_SCRIPT: list[dict[str, Any]] = [
    {"tool_calls": [{"id": "c1", "name": "math.add", "arguments": {"a": 2, "b": 3}}]},
    {"content": "done: sum=5 and report ready"},
]

PUBLIC_GRANT = "grant_public"


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


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


def wait_terminal(c: TestClient, token: str, rid: str) -> str:
    deadline = time.monotonic() + 10
    state = ""
    while time.monotonic() < deadline:
        state = c.get(f"/v1/tasks/{rid}", headers=auth(token)).json()["run"]["state"]
        if state in {"succeeded", "failed", "cancelled"}:
            return state
        time.sleep(0.05)
    return state


def bootstrap(c: TestClient) -> tuple[int, dict[str, Any]]:
    """游客免登：不带任何凭据。"""
    r = c.post("/v1/auth/guest/bootstrap")
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    return r.status_code, body


def err_reason(body: dict[str, Any]) -> str:
    """HTTPException 把 AgentOSError 包成 detail{code,message,detail{reason}}。"""
    detail = body.get("detail") or {}
    inner = detail.get("detail") if isinstance(detail, dict) else None
    if isinstance(inner, dict) and inner.get("reason"):
        return str(inner["reason"])
    return str(detail.get("reason") or "") if isinstance(detail, dict) else ""


def publish_default(c: TestClient, token: str, runs: list[dict[str, Any]],
                    assets: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """把内容发布到默认演示通道（首次发布自动建立通道）。"""
    for item in runs:
        rid = str(item.get("run_id") or "")
        if rid and not item.get("live"):
            assert wait_terminal(c, token, rid) == "succeeded"
    r = c.post(f"/v1/demo/grants/{PUBLIC_GRANT}/publish", headers=auth(token),
               json={"runs": runs, "assets": assets or []})
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------------ 默认可进
def test_no_credentials_yields_read_only_guest(make_system: Callable[..., SystemHandle]) -> None:
    """免登是默认角色：一次 POST 换只读会话，不需要访问码，也不给任何写入口。"""
    with client(make_system) as (c, handle, token):
        rid = submit(c, token, "u5 default role", "u5d-1")
        publish_default(c, token, [{"run_id": rid}])

        status, body = bootstrap(c)
        assert status == 200, body
        gtok = body["access_token"]
        assert body["role"] == "guest" and str(body["subject"]).startswith("guest:")
        assert body["manifest_revision"] == 1
        # 免登会话的 access/refresh 都不含管理员凭据
        assert body["subject"] != "owner" and "owner" not in str(body)

        me = c.get("/v1/auth/me", headers=auth(gtok)).json()
        assert me["role"] == "guest" and me["grant"]["id"] != PUBLIC_GRANT
        home = c.get("/v1/demo/home", headers=auth(gtok))
        assert home.status_code == 200
        assert [r["id"] for r in home.json()["runs"]] == [rid]

        # 写入口仍由服务端拒绝，且没有任何入队/副作用
        runs_before = {r["id"] for r in c.get("/v1/runs", headers=auth(token)).json()}
        for call in (
            lambda: c.post("/v1/tasks", headers=auth(gtok),
                           json={"payload": {"goal": "evil"}, "idempotency_key": "u5d-write"}),
            lambda: c.post(f"/v1/tasks/{rid}/cancel", headers=auth(gtok), json={}),
            lambda: c.post("/v1/materials", headers=auth(gtok),
                           json={"name": "x.txt", "content_text": "x"}),
            lambda: c.post("/v1/approvals/any/decision", headers=auth(gtok),
                           json={"decision": "approve"}),
        ):
            assert call().status_code in (401, 403)
        assert {r["id"] for r in c.get("/v1/runs", headers=auth(token)).json()} == runs_before
        assert c.get(f"/v1/tasks/{rid}", headers=auth(token)).json()["run"]["state"] == "succeeded"


def test_setup_advertises_guest_entry_without_leaking_content(
    make_system: Callable[..., SystemHandle],
) -> None:
    """登录页只读到"能否免登/是否已发布"，不含任务标题、数量或正文。"""
    with client(make_system) as (c, handle, token):
        assert c.get("/v1/auth/setup").json()["guest"] == {"open": False, "published": False}
        rid = submit(c, token, "u5 setup goal 不应泄露", "u5d-2")
        c.post("/v1/demo/public/open", headers=auth(token))
        setup = c.get("/v1/auth/setup").json()["guest"]
        assert setup == {"open": True, "published": False}
        publish_default(c, token, [{"run_id": rid}])
        setup = c.get("/v1/auth/setup").json()["guest"]
        assert setup == {"open": True, "published": True}
        assert "不应泄露" not in str(setup)


# ------------------------------------------------------------------ 准入裁定
def test_bootstrap_without_channel_is_explicit_failure(
    make_system: Callable[..., SystemHandle],
) -> None:
    """通道不存在 → 403 明确"未开放"；开放但未发布 → 409，不签任何会话。"""
    with client(make_system) as (c, handle, token):
        status, body = bootstrap(c)
        assert status == 403, body
        assert err_reason(body) == "public_disabled"
        assert c.post("/v1/demo/public/open", headers=auth(token)).status_code == 200
        status, body = bootstrap(c)
        assert status == 409, body
        assert err_reason(body) == "not_published"
        # 未进入时只建了通道本身，不产生任何访客 grant
        public = handle.store.list_demo_grants("default", kind="public")
        assert [row["subject"] for row in public] == ["guest:public"]
        # 管理员可从 demo/public 读到"尚未发布"的真实状态（不伪装成可进）
        status_json = c.get("/v1/demo/public", headers=auth(token)).json()
        assert status_json["open"] is True and status_json["published"] is False


def test_config_kill_switch_overrides_admin_channel(
    make_system: Callable[..., SystemHandle],
) -> None:
    """配置总开关关闭时，即使管理员开放通道也一律 403（部署级 fail-closed）。"""
    handle = make_system(model_script=DEMO_SCRIPT, public_demo_enabled=False)
    with TestClient(create_app(system=handle)) as c:
        token = owner_token(handle)
        rid = submit(c, token, "kill switch", "u5d-3")
        publish_default(c, token, [{"run_id": rid}])
        status, body = bootstrap(c)
        assert status == 403, body
        assert c.get("/v1/auth/setup").json()["guest"]["open"] is False


def test_public_bootstrap_respects_session_quota(
    make_system: Callable[..., SystemHandle],
) -> None:
    """名额满 → 503 明确拒绝，不挤掉已在访问客，也不静默放行。"""
    handle = make_system(model_script=DEMO_SCRIPT, public_demo_max_sessions=1)
    with TestClient(create_app(system=handle)) as c:
        token = owner_token(handle)
        rid = submit(c, token, "quota", "u5d-4")
        publish_default(c, token, [{"run_id": rid}])
        assert bootstrap(c)[0] == 200
        status, body = bootstrap(c)
        assert status == 503, body
        assert err_reason(body) == "quota_exhausted"
        visitors = [row for row in handle.store.list_demo_grants("default", kind="public")
                    if str(row["subject"]) != "guest:public"]
        assert len(visitors) == 1


# ------------------------------------------------------------------ 隔离与失效
def test_each_visitor_gets_independent_subject_and_snapshot(
    make_system: Callable[..., SystemHandle],
) -> None:
    """每位访客一个 subject：撤销一位不影响另一位，也不共享管理员凭据。"""
    with client(make_system) as (c, handle, token):
        rid = submit(c, token, "shared snapshot", "u5d-5")
        publish_default(c, token, [{"run_id": rid}])
        _s1, v1 = bootstrap(c)
        _s2, v2 = bootstrap(c)
        assert v1["subject"] != v2["subject"]
        assert v1["manifest_revision"] == v2["manifest_revision"] == 1
        assert c.get("/v1/demo/home", headers=auth(v1["access_token"])).status_code == 200
        assert c.get("/v1/demo/home", headers=auth(v2["access_token"])).status_code == 200

        # 管理员从"游客访问记录"里撤销其中一位
        listed = c.get("/v1/demo/grants?kind=public", headers=auth(token)).json()
        by_subject = {row["subject"]: row["id"] for row in listed}
        c.post(f"/v1/demo/grants/{by_subject[v1['subject']]}/revoke", headers=auth(token))
        assert c.get("/v1/demo/home", headers=auth(v1["access_token"])).status_code in (401, 403)
        assert c.get("/v1/demo/home", headers=auth(v2["access_token"])).status_code == 200
        assert c.post("/v1/auth/refresh",
                      json={"refresh_token": v1["refresh_token"]}).status_code in (401, 403)


def test_closing_public_access_revokes_every_visitor(
    make_system: Callable[..., SystemHandle],
) -> None:
    """关闭开关：通道与全部在访会话立即失效，后续读取/刷新/下载统一拒绝。"""
    with client(make_system) as (c, handle, token):
        rid = submit(c, token, "closing", "u5d-6")
        a_id = handle.assets.register_report_version(
            asyncio.run(handle.deps.identity.authenticate({"token": token})),
            data=b"closing body", name="close.md", run_id=rid)["asset_id"]
        publish_default(c, token, [{"run_id": rid}],
                        [{"asset_id": a_id, "version": 1, "preview": True, "download": True}])
        _s, v = bootstrap(c)
        assert c.get(f"/v1/demo/assets/{a_id}/download", headers=auth(v["access_token"])).status_code == 200

        out = c.post("/v1/demo/public/close", headers=auth(token))
        assert out.status_code == 200 and out.json()["open"] is False
        for url in (f"/v1/demo/runs/{rid}", "/v1/demo/home", f"/v1/demo/assets/{a_id}"):
            assert c.get(url, headers=auth(v["access_token"])).status_code in (401, 403, 404), url
        assert c.get(f"/v1/demo/assets/{a_id}/download",
                     headers=auth(v["access_token"])).status_code in (401, 403, 404)
        assert c.post("/v1/auth/refresh",
                      json={"refresh_token": v["refresh_token"]}).status_code in (401, 403)
        assert bootstrap(c)[0] == 403


def test_reopen_keeps_published_revision_and_new_visitors_enter(
    make_system: Callable[..., SystemHandle],
) -> None:
    """重新开放保留已发布 revision（不空转成新清单），新访客可再进入。"""
    with client(make_system) as (c, handle, token):
        rid = submit(c, token, "reopen", "u5d-7")
        publish_default(c, token, [{"run_id": rid}])
        c.post("/v1/demo/public/close", headers=auth(token))
        assert bootstrap(c)[0] == 403
        reopened = c.post("/v1/demo/public/open", headers=auth(token)).json()
        assert reopened["open"] is True and reopened["manifest_revision"] == 1
        status, body = bootstrap(c)
        assert status == 200 and body["manifest_revision"] == 1


def test_closed_channel_does_not_revive_after_snapshot_restore(
    make_system: Callable[..., SystemHandle],
) -> None:
    """关闭是"事实"：恢复关闭前的旧快照 + 重启后通道与访客仍不可用（R08 同口径）。"""
    handle = make_system(model_script=DEMO_SCRIPT)
    c = TestClient(create_app(system=handle))
    with c:
        token = owner_token(handle)
        rid = submit(c, token, "restore revive attempt", "u5d-8")
        publish_default(c, token, [{"run_id": rid}])
        _s, visitor = bootstrap(c)
        db_path = handle.settings.db_path
        backup = handle.settings.data_dir / "pre-close.db"
        src, dst = sqlite3.connect(str(db_path)), sqlite3.connect(str(backup))
        src.backup(dst)
        dst.close()
        src.close()
        c.post("/v1/demo/public/close", headers=auth(token))
        assert bootstrap(c)[0] == 403

    handle.store.raw_connection().close()
    shutil.copy(backup, db_path)
    for suffix in ("-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    handle2 = make_system(model_script=DEMO_SCRIPT)
    handle2.boot()
    with TestClient(create_app(system=handle2)) as c2:
        assert c2.get(f"/v1/demo/runs/{rid}", headers=auth(visitor["access_token"])).status_code \
            in (401, 403, 404)
        assert c2.post("/v1/auth/guest/bootstrap").status_code == 403
