"""Identity trust, persisted revocation and organization-to-worker ownership regressions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from agentos.adapters.identity.federation import CredentialVault, IdentityLane
from agentos.apps.composition import build_system
from agentos.apps.config import Settings
from agentos.kernel.errors import AgentOSError
from agentos.kernel.states import RunState
from tests.conftest import make_settings


@pytest.fixture
def trusted_issuer():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="key-1", use="sig", alg="RS256")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            base = f"http://127.0.0.1:{self.server.server_port}"
            data = {"issuer": base, "jwks_uri": base + "/trusted-keys"} if self.path.endswith(
                "openid-configuration") else {"keys": [jwk]}
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", key
    server.shutdown()


def lane(issuer: str) -> dict:
    return {"id": "enterprise", "issuer": issuer, "audience": "agentos-api", "allow_http": True,
            "tenant_map": {"A": "org-a", "B": "org-b"}, "roles_claim": "role",
            "scope_map": {"operator": ["read", "write", "approve"]}}


def token(issuer, key, **claims):
    payload = {"iss": issuer, "aud": "agentos-api", "sub": "user-1", "org": "A",
               "role": "operator", "exp": time.time() + 90, **claims}
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": "key-1"})


def test_oidc_in_actual_composition_drives_nondefault_task(tmp_path, trusted_issuer):
    issuer, key = trusted_issuer
    settings = make_settings(tmp_path, identity_lanes=[lane(issuer)])
    handle = build_system(settings, model_script=[{"content": "enterprise task complete"}])
    handle.boot()
    receipt = asyncio.run(handle.ingress.accept({"payload": {"goal": "synthetic enterprise task"}},
                         channel="http", credentials={"token": token(issuer, key)}))
    assert handle.store.get_run("default", receipt.run_id) is None
    assert handle.store.list_job_tenants() == ["org-a"]
    state = asyncio.run(handle.worker.drive_until_settled(receipt.run_id, tenant_id="org-a"))
    assert state == "succeeded"
    assert handle.store.get_run("org-a", receipt.run_id).state == RunState.SUCCEEDED
    assert handle.store.get_run("org-b", receipt.run_id) is None


def test_original_expiry_and_revocation_survive_process_rebuild(tmp_path, trusted_issuer):
    issuer, key = trusted_issuer
    settings = Settings(data_dir=tmp_path, identity_lanes=[lane(issuer)])
    handle = build_system(settings)
    handle.boot()
    raw = token(issuer, key, exp=time.time() + 30)
    actor = asyncio.run(handle.deps.identity.authenticate({"token": raw}))
    assert actor.expires_at.timestamp() <= jwt.decode(raw, options={"verify_signature": False})["exp"]
    vault = CredentialVault(tmp_path / "private-identities")
    assert raw.encode() not in (tmp_path / "private-identities/federation.db").read_bytes()
    vault.revoke(actor.identity_ref)
    rebuilt = build_system(settings)
    with pytest.raises(AgentOSError, match="revoked"):
        asyncio.run(rebuilt.deps.identity.refresh(actor))
    with pytest.raises(AgentOSError, match="revoked"):
        asyncio.run(rebuilt.deps.identity.authenticate({"token": raw}))


@pytest.mark.parametrize("claims", [{"org": "unknown"}, {"aud": "different-api"},
                                   {"role": "unmapped"}, {"iss": "https://untrusted.example"}])
def test_untrusted_mapping_never_falls_back_to_local(tmp_path, trusted_issuer, claims):
    issuer, key = trusted_issuer
    system = build_system(Settings(data_dir=tmp_path, identity_lanes=[lane(issuer)]))
    system.boot()
    with pytest.raises(AgentOSError):
        asyncio.run(system.deps.identity.authenticate({"token": token(issuer, key, **claims)}))


def test_stable_subject_uses_issuer_and_sub_not_mutable_username(tmp_path, trusted_issuer):
    issuer, key = trusted_issuer
    system = build_system(Settings(data_dir=tmp_path, identity_lanes=[lane(issuer)]))
    system.boot()
    one = asyncio.run(system.deps.identity.authenticate({"token": token(issuer, key, preferred_username="old")}))
    two = asyncio.run(system.deps.identity.authenticate({"token": token(issuer, key, preferred_username="new")}))
    assert one.subject == two.subject == "external:" + hashlib.sha256(f"{issuer}\0user-1".encode()).hexdigest()


def test_jwt_lane_without_discovery(tmp_path, trusted_issuer):
    issuer, key = trusted_issuer
    system = build_system(Settings(data_dir=tmp_path, identity_lanes=[{**lane(issuer), "kind": "jwt",
                                                                    "jwks_url": issuer + "/trusted-keys"}]))
    system.boot()
    actor = asyncio.run(system.deps.identity.authenticate({"token": token(issuer, key)}))
    assert actor.tenant_id == "org-a"


def test_invalid_algorithm_and_missing_org_mapping_rejected():
    with pytest.raises(ValueError):
        IdentityLane(id="bad", issuer="https://sso.example", audience="api", algorithms=["HS256"], fixed_tenant="a")
    with pytest.raises(ValueError):
        IdentityLane(id="bad", issuer="https://sso.example", audience="api")


def test_worker_background_claims_enterprise_organization(tmp_path: Path, trusted_issuer):
    issuer, key = trusted_issuer
    system = build_system(Settings(data_dir=tmp_path, identity_lanes=[lane(issuer)]),
                          model_script=[{"content": "background completion"}])
    system.boot()

    async def run():
        receipt = await system.ingress.accept({"payload": {"goal": "background"}}, channel="http",
                                             credentials={"token": token(issuer, key, org="B")})
        stop = asyncio.Event()
        worker = asyncio.create_task(system.worker.run_forever(stop))
        try:
            for _ in range(100):
                if system.store.get_run("org-b", receipt.run_id).state == RunState.SUCCEEDED:
                    return
                await asyncio.sleep(.02)
            pytest.fail("enterprise job was not driven by the background worker")
        finally:
            stop.set()
            await worker

    asyncio.run(run())


def test_legacy_hs256_is_a_separate_configured_trust_lane(tmp_path, monkeypatch):
    import secrets

    secret = secrets.token_hex(32)
    monkeypatch.setenv("SYNTHETIC_LEGACY_JWT_SECRET", secret)
    config = {"id": "legacy", "kind": "jwt", "issuer": "https://legacy.example",
              "audience": "agentos-api", "algorithms": ["HS256"],
              "shared_secret_env": "SYNTHETIC_LEGACY_JWT_SECRET", "fixed_tenant": "legacy-org",
              "roles_claim": "role", "scope_map": {"operator": ["read", "write"]}}
    system = build_system(Settings(data_dir=tmp_path, identity_lanes=[config]))
    raw = jwt.encode({"iss": config["issuer"], "aud": config["audience"], "sub": "user-1",
                      "role": "operator", "exp": time.time() + 90}, secret, algorithm="HS256")
    actor = asyncio.run(system.deps.identity.authenticate({"token": raw}))
    assert actor.tenant_id == "legacy-org"
    with pytest.raises(ValueError):
        IdentityLane.model_validate({**config, "algorithms": ["HS256", "RS256"]})
    monkeypatch.setenv("SYNTHETIC_LEGACY_JWT_SECRET", "too-short")
    with pytest.raises(AgentOSError):
        asyncio.run(system.deps.identity.authenticate({"token": raw}))


def test_browser_pkce_nonce_cookie_and_cross_site_denial(tmp_path):
    """Real HTTP identity endpoints; browser receives only an opaque platform cookie."""
    import base64
    from urllib.parse import parse_qs, urlparse

    import httpx

    from agentos.apps.gateway import create_app

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="browser-key", use="sig", alg="RS256")
    login_state = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            base = f"http://127.0.0.1:{self.server.server_port}"
            data = ({"issuer": base, "authorization_endpoint": base + "/authorize",
                     "token_endpoint": base + "/token", "jwks_uri": base + "/keys",
                     "id_token_signing_alg_values_supported": ["RS256"]}
                    if self.path.endswith("openid-configuration") else {"keys": [jwk]})
            self.reply(data)

        def do_POST(self):  # noqa: N802
            form = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            verifier = form["code_verifier"][0]
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            assert challenge == login_state["code_challenge"][0]
            login_state["pkce_checked"] = True
            base = f"http://127.0.0.1:{self.server.server_port}"
            now = int(time.time())
            identity = jwt.encode({"iss": base, "aud": "browser-client", "sub": "user-1",
                                   "iat": now, "exp": now + 90, "nonce": login_state["nonce"][0]},
                                  key, algorithm="RS256", headers={"kid": "browser-key"})
            access = jwt.encode({"iss": base, "aud": "agentos-api", "sub": "user-1",
                                 "exp": now + 90, "org": "A", "role": "operator"},
                                key, algorithm="RS256", headers={"kid": "browser-key"})
            self.reply({"access_token": access, "id_token": identity,
                        "token_type": "Bearer", "expires_in": 90})

        def reply(self, data):
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    issuer = f"http://127.0.0.1:{server.server_port}"
    h = build_system(Settings(data_dir=tmp_path, identity_lanes=[{**lane(issuer), "client_id": "browser-client"}]))
    h.boot()
    app = create_app(system=h)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
            started = await c.get("/v1/auth/oidc/enterprise/login")
            assert started.status_code == 302
            login_state.update(parse_qs(urlparse(started.headers["location"]).query))
            assert login_state["code_challenge_method"] == ["S256"]
            invalid = await c.get("/v1/auth/oidc/enterprise/callback?code=good&state=wrong")
            assert invalid.status_code == 401
            callback = await c.get("/v1/auth/oidc/enterprise/callback",
                                   params={"code": "good", "state": login_state["state"][0]})
            assert callback.status_code == 303
            assert login_state["pkce_checked"]
            cookie = callback.headers["set-cookie"]
            assert "HttpOnly" in cookie and "access_token" not in cookie
            current = await c.get("/v1/auth/browser-session")
            assert current.json()["tenant_id"] == "org-a"
            denied = await c.post("/v1/auth/browser-logout", headers={"Origin": "https://attacker.example"})
            assert denied.status_code == 403
            logout = await c.post("/v1/auth/browser-logout", headers={"Origin": "http://testserver"})
            assert logout.status_code == 303
            assert (await c.get("/v1/auth/browser-session")).status_code == 401

    try:
        asyncio.run(run())
    finally:
        server.shutdown()
