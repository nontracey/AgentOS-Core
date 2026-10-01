"""产品登录（双 token）验收：密码登录、refresh 轮换/重用检测、登出撤销、改密失效。

静态 owner 令牌路径不受影响（CLI/自动化兼容）。
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from fastapi.testclient import TestClient

from agentos.apps.composition import SystemHandle
from agentos.apps.gateway import create_app
from tests.conftest import WRITE_FLOW_SCRIPT, owner_token


@contextmanager
def client(make_system: Callable[..., SystemHandle], **kw: Any):
    handle = make_system(model_script=kw.pop("script", WRITE_FLOW_SCRIPT), **kw)
    app = create_app(system=handle)
    with TestClient(app) as c:
        yield c, handle, owner_token(handle)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _login(c: TestClient, password: str, remember: bool = False) -> dict[str, Any]:
    r = c.post("/v1/auth/login", json={"password": password, "remember": remember})
    assert r.status_code == 200, r.text
    return r.json()


def _initial_password(handle: SystemHandle) -> str:
    once = handle.settings.identity_file.parent / "owner_password.once"
    assert once.exists(), "首次 boot 应生成初始密码文件"
    return once.read_text(encoding="utf-8").strip()


def test_wrong_password_rejected(make_system: Callable[..., SystemHandle]) -> None:
    with client(make_system) as (c, handle, _):
        r = c.post("/v1/auth/login", json={"password": "错误密码", "remember": True})
        assert r.status_code == 401
        r = c.post("/v1/auth/login", json={"password": "", "remember": True})
        assert r.status_code == 401


def test_login_access_token_works(make_system: Callable[..., SystemHandle]) -> None:
    with client(make_system) as (c, handle, _):
        body = _login(c, _initial_password(handle), remember=True)
        assert body["token_type"] == "bearer" and body["refresh_token"]
        r = c.get("/v1/runs", headers=auth(body["access_token"]))
        assert r.status_code == 200


def test_initial_password_file_cleared_after_change(
    make_system: Callable[..., SystemHandle],
) -> None:
    with client(make_system) as (c, handle, _):
        pw = _initial_password(handle)
        body = _login(c, pw)
        r = c.post("/v1/auth/password", headers=auth(body["access_token"]),
                   json={"current_password": pw, "new_password": "新的密码12345"})
        assert r.status_code == 200
        once = handle.settings.identity_file.parent / "owner_password.once"
        assert not once.exists()
        # 旧密码失效、新密码可登录
        assert c.post("/v1/auth/login", json={"password": pw, "remember": False}).status_code == 401
        _login(c, "新的密码12345")
        # 改密撤销全部会话：旧 access 令牌（短时签名）仍验证通过属预期（≤30min 窗口），
        # 但 refresh 必须全部撤销
        assert c.post("/v1/auth/refresh", json={"refresh_token": body["refresh_token"]}).status_code == 401


def test_refresh_rotation_and_reuse_detection(
    make_system: Callable[..., SystemHandle],
) -> None:
    with client(make_system) as (c, handle, _):
        pw = _initial_password(handle)
        body = _login(c, pw, remember=True)
        old_refresh = body["refresh_token"]
        # 轮换：旧 refresh 用一次后作废
        r1 = c.post("/v1/auth/refresh", json={"refresh_token": old_refresh})
        assert r1.status_code == 200
        new_refresh = r1.json()["refresh_token"]
        assert new_refresh != old_refresh
        # 重用已作废 refresh → 全部会话撤销
        r2 = c.post("/v1/auth/refresh", json={"refresh_token": old_refresh})
        assert r2.status_code == 401
        assert "revoke" in r2.json()["detail"]["message"]
        r3 = c.post("/v1/auth/refresh", json={"refresh_token": new_refresh})
        assert r3.status_code == 401  # 连坐撤销
        # 静态 owner 令牌不受影响（CLI/自动化路径）
        assert c.get("/v1/runs", headers=auth(owner_token(handle))).status_code == 200


def test_logout_revokes_refresh(make_system: Callable[..., SystemHandle]) -> None:
    with client(make_system) as (c, handle, _):
        body = _login(c, _initial_password(handle), remember=True)
        r = c.post("/v1/auth/logout", json={"refresh_token": body["refresh_token"]})
        assert r.status_code == 200
        assert c.post("/v1/auth/refresh", json={
            "refresh_token": body["refresh_token"]}).status_code == 401


def test_setup_endpoint_reports_initial_state(
    make_system: Callable[..., SystemHandle],
) -> None:
    with client(make_system) as (c, handle, _):
        r = c.get("/v1/auth/setup")
        assert r.status_code == 200
        assert r.json()["initial_password_pending"] is True
        # 改密后 pending 消失
        body = _login(c, _initial_password(handle))
        c.post("/v1/auth/password", headers=auth(body["access_token"]),
               json={"current_password": _initial_password(handle),
                     "new_password": "新的密码12345"})
        assert c.get("/v1/auth/setup").json()["initial_password_pending"] is False


def test_wrong_current_password_cannot_change(
    make_system: Callable[..., SystemHandle],
) -> None:
    with client(make_system) as (c, handle, _):
        body = _login(c, _initial_password(handle))
        r = c.post("/v1/auth/password", headers=auth(body["access_token"]),
                   json={"current_password": "错的", "new_password": "新的密码12345"})
        assert r.status_code == 403
        # 短密码拒绝
        r = c.post("/v1/auth/password", headers=auth(body["access_token"]),
                   json={"current_password": _initial_password(handle),
                         "new_password": "短"})
        assert r.status_code == 422
