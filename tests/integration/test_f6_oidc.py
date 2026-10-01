"""FR-GOV-07 企业 OIDC 验收：真实自建 IdP（Keycloak）+ JWT 验证后端。

- 本地假 JWKS（自管 RSA 密钥）覆盖错误矩阵：签名错/iss 错/aud 错/过期 → 全部 DENIED
- 真实 Keycloak 正向链路由 AGENTOS_OIDC_LIVE=1 开启（realm 由 scripts/f6_oidc_setup.py
  预配置）：password grant → authenticate 成功 → org 租户/角色 scope 映射；
  正式验收以 evidence 实测记录为准（skip 不算 pass）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import jwt
import pytest

from agentos.adapters.identity.oidc import OIDCJwtIdentity
from agentos.kernel.errors import AgentOSError, ErrorCode

_KEY = jwt.algorithms.RSAAlgorithm.generate_key = None  # placeholder replaced below
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

_PRIVATE = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUBLIC_NUMBERS = _PRIVATE.public_key().public_numbers()


def _b64(n: int) -> str:
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


_JWKS = {"keys": [{
    "kty": "RSA", "use": "sig", "alg": "RS256", "kid": "test-key",
    "n": _b64(_PUBLIC_NUMBERS.n), "e": _b64(_PUBLIC_NUMBERS.e),
}]}


def _issue(iss: str, aud: str, exp_offset: float = 3600,
           org: str = "orgA", roles: list[str] | None = None,
           username: str = "alice") -> str:
    import datetime

    now = datetime.datetime.now(datetime.UTC)
    payload = {
        "iss": iss, "aud": aud, "sub": "user-1", "exp": now.timestamp() + exp_offset,
        "iat": now.timestamp(), "preferred_username": username,
        "org": org, "realm_access": {"roles": roles or ["role_reader"]},
    }
    return jwt.encode(payload, _PRIVATE, algorithm="RS256", headers={"kid": "test-key"})


@pytest.fixture(scope="module")
def jwks_server() -> str:
    class H(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = json.dumps(_JWKS).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a: object) -> None:
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def _auth(issuer: str, audience: str) -> OIDCJwtIdentity:
    return OIDCJwtIdentity(issuer, audience)


def test_valid_token_authenticates_with_org_and_scopes(jwks_server: str) -> None:
    svc = _auth(f"{jwks_server}/realms/agentos", "agentos-api")
    tok = _issue(f"{jwks_server}/realms/agentos", "agentos-api")
    actor = asyncio.run(svc.authenticate({"token": tok}))
    assert actor.subject == "alice"
    assert actor.tenant_id == "orgA"
    assert actor.scopes == ["read"]  # role_reader → read（默认拒绝，仅映射角色）


def test_signature_error_rejected(jwks_server: str) -> None:
    """签名错误 → DENIED（用另一把密钥签发，JWKS 里没有对应私钥）。"""
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    import datetime

    now = datetime.datetime.now(datetime.UTC)
    tok = jwt.encode(
        {"iss": f"{jwks_server}/realms/agentos", "aud": "agentos-api", "sub": "x",
         "exp": now.timestamp() + 3600},
        other, algorithm="RS256", headers={"kid": "test-key"})
    with pytest.raises(AgentOSError) as ei:
        asyncio.run(_auth(f"{jwks_server}/realms/agentos", "agentos-api")
                    .authenticate({"token": tok}))
    assert ei.value.code == ErrorCode.DENIED


def test_issuer_mismatch_rejected(jwks_server: str) -> None:
    tok = _issue("https://evil.example/realms/agentos", "agentos-api")
    with pytest.raises(AgentOSError, match="issuer"):
        asyncio.run(_auth(f"{jwks_server}/realms/agentos", "agentos-api")
                    .authenticate({"token": tok}))


def test_audience_mismatch_rejected(jwks_server: str) -> None:
    tok = _issue(f"{jwks_server}/realms/agentos", "other-api")
    with pytest.raises(AgentOSError, match="audience"):
        asyncio.run(_auth(f"{jwks_server}/realms/agentos", "agentos-api")
                    .authenticate({"token": tok}))


def test_expired_rejected(jwks_server: str) -> None:
    tok = _issue(f"{jwks_server}/realms/agentos", "agentos-api", exp_offset=-60)
    with pytest.raises(AgentOSError, match="expired"):
        asyncio.run(_auth(f"{jwks_server}/realms/agentos", "agentos-api")
                    .authenticate({"token": tok}))


def test_unmapped_role_grants_no_scopes(jwks_server: str) -> None:
    """未映射角色 → 空 scopes（默认拒绝语义在身份层即生效）。"""
    svc = _auth(f"{jwks_server}/realms/agentos", "agentos-api")
    tok = _issue(f"{jwks_server}/realms/agentos", "agentos-api",
                 roles=["some_unknown_role"])
    actor = asyncio.run(svc.authenticate({"token": tok}))
    assert actor.scopes == []


@pytest.mark.skipif(not os.environ.get("AGENTOS_OIDC_LIVE"),
                    reason="需要真实 Keycloak（AGENTOS_OIDC_LIVE=1），正式验收以 evidence 为准")
def test_live_keycloak_password_grant_flow() -> None:
    """真实 Keycloak：取 token → 验证 → org 租户隔离（两组织互不可见由 tenant 机制保证）。"""
    sys_path = os.path.join(os.path.dirname(__file__), "../../scripts")
    import sys

    sys.path.insert(0, sys_path)
    from _runtime import CONF

    base = CONF["keycloak_url"].rstrip("/")
    realm = CONF.get("keycloak_realm", "agentos")
    c = __import__("httpx").Client(trust_env=False, timeout=30)
    r = c.post(f"{base}/realms/{realm}/protocol/openid-connect/token",
               data={"grant_type": "password", "client_id": "agentos-api",
                     "username": "orgb-operator", "password": "operator-pass-1"})
    assert r.status_code == 200, r.text
    tok = r.json()["access_token"]
    svc = _auth(f"{base}/realms/{realm}", "agentos-api")
    actor = asyncio.run(svc.authenticate({"token": tok}))
    assert actor.subject == "orgb-operator"
    assert actor.tenant_id == "orgB"
    assert "write" in actor.scopes  # role_operator → read/write/approve
    assert "approve" in actor.scopes
    time.sleep(0)  # 保持 import time 的引用（测试结构一致性）


@pytest.mark.skipif(not os.environ.get("AGENTOS_OIDC_LIVE"),
                    reason="需要真实 Keycloak（AGENTOS_OIDC_LIVE=1）")
def test_live_revocation_disables_new_tokens() -> None:
    """撤权（IdP 禁用用户）：新 token 拿不到（400）；重新启用即恢复。

    边界如实标注：已签发的 JWT 在 exp 前仍有效（无状态令牌），撤权收敛靠短 TTL。
    """
    import httpx

    sys_path = os.path.join(os.path.dirname(__file__), "../../scripts")
    import sys

    sys.path.insert(0, sys_path)
    from _runtime import CONF

    base = CONF["keycloak_url"].rstrip("/")
    realm = CONF.get("keycloak_realm", "agentos")
    c = httpx.Client(trust_env=False, timeout=30)
    tok = c.post(f"{base}/realms/master/protocol/openid-connect/token",
                 data={"grant_type": "password", "client_id": "admin-cli",
                       "username": CONF["keycloak_admin"],
                       "password": CONF["keycloak_password"]}).json()["access_token"]
    H = {"Authorization": f"Bearer {tok}"}
    u = c.get(f"{base}/admin/realms/{realm}/users",
              params={"username": "synth-000", "exact": "true"},
              headers=H, timeout=30).json()[0]
    c.put(f"{base}/admin/realms/{realm}/users/{u['id']}", headers=H,
          json={**u, "enabled": False})
    r = c.post(f"{base}/realms/{realm}/protocol/openid-connect/token",
               data={"grant_type": "password", "client_id": "agentos-api",
                     "username": "synth-000", "password": "synth-pass-000"})
    assert r.status_code == 400, "禁用后仍能取得 token"
    c.put(f"{base}/admin/realms/{realm}/users/{u['id']}", headers=H,
          json={**u, "enabled": True})
    r = c.post(f"{base}/realms/{realm}/protocol/openid-connect/token",
               data={"grant_type": "password", "client_id": "agentos-api",
                     "username": "synth-000", "password": "synth-pass-000"})
    assert r.status_code == 200


@pytest.mark.skipif(not os.environ.get("AGENTOS_OIDC_LIVE"),
                    reason="需要真实 Keycloak（AGENTOS_OIDC_LIVE=1）")
def test_live_synth_accounts_sample_flow() -> None:
    """100 合成账号抽验 6 个（两组织两角色交替）：同流程取 token 并验证身份。"""
    import httpx

    sys_path = os.path.join(os.path.dirname(__file__), "../../scripts")
    import sys

    sys.path.insert(0, sys_path)
    from _runtime import CONF

    base = CONF["keycloak_url"].rstrip("/")
    realm = CONF.get("keycloak_realm", "agentos")
    c = httpx.Client(trust_env=False, timeout=30)
    svc = _auth(f"{base}/realms/{realm}", "agentos-api")
    for i in (0, 1, 2, 48, 50, 99):
        uname = f"synth-{i:03d}"
        r = c.post(f"{base}/realms/{realm}/protocol/openid-connect/token",
                   data={"grant_type": "password", "client_id": "agentos-api",
                         "username": uname, "password": f"synth-pass-{i:03d}"})
        assert r.status_code == 200, f"{uname} 登录失败"
        actor = asyncio.run(svc.authenticate({"token": r.json()["access_token"]}))
        expect_org = "orgA" if i % 2 == 0 else "orgB"
        assert actor.tenant_id == expect_org, f"{uname} 组织不符"
        assert actor.subject == uname
