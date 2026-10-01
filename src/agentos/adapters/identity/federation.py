"""Configured enterprise trust lanes; external credentials stay encrypted server-side."""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
import jwt
from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, Field, model_validator

from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.ports import IdentityPort
from agentos.kernel.types import AuthContext, utc_now


class IdentityLane(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    kind: Literal["oidc", "jwt"] = "oidc"
    issuer: str
    audience: str | list[str]
    discovery_url: str = ""
    jwks_url: str = ""
    algorithms: list[str] = Field(default_factory=lambda: ["RS256"])
    tenant_claim: str = "org"
    tenant_map: dict[str, str] = Field(default_factory=dict)
    fixed_tenant: str = ""
    roles_claim: str = "realm_access.roles"
    scope_map: dict[str, list[str]] = Field(default_factory=dict)
    role_map: dict[str, list[str]] = Field(default_factory=dict)
    leeway_s: int = Field(default=15, ge=0, le=60)
    cache_ttl_s: int = Field(default=300, ge=10, le=3600)
    auth_ttl_s: int = Field(default=300, ge=1, le=3600)
    shared_secret_env: str = ""  # Dedicated HS256 legacy lane; never mixed with public-key trust.
    login_scope: str = "openid profile"
    client_id: str = ""
    client_secret_env: str = ""
    introspection_url: str = ""
    allow_http: bool = False

    @model_validator(mode="after")
    def validate_trust(self) -> IdentityLane:
        if self.shared_secret_env:
            if self.kind != "jwt" or self.algorithms != ["HS256"] or self.jwks_url:
                raise ValueError("shared secret requires a separate HS256 JWT lane without JWKS")
        else:
            if not self.algorithms or any(a not in {"RS256", "RS384", "RS512", "ES256", "ES384", "EdDSA"}
                                          for a in self.algorithms):
                raise ValueError("only explicitly allowed asymmetric JWT algorithms are accepted")
            if self.kind == "jwt" and not self.jwks_url:
                raise ValueError("JWT lane requires a trusted JWKS URL")
        for url in (self.issuer, self.discovery_url, self.jwks_url, self.introspection_url):
            if url and not (url.startswith("https://") or self.allow_http and url.startswith("http://")):
                raise ValueError("identity endpoints require HTTPS (HTTP only with explicit local test opt-in)")
        if not self.fixed_tenant and not self.tenant_map:
            raise ValueError("explicit trusted organization mapping is required")
        return self


class CredentialVault:
    """Fernet envelope encryption; lookup handles confer no user permissions."""

    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        keyfile = directory / "federation.key"
        try:
            fd = os.open(keyfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(Fernet.generate_key())
        self._cipher = Fernet(keyfile.read_bytes())
        database = directory / "federation.db"
        self._db = sqlite3.connect(database, check_same_thread=False)
        database.chmod(0o600)
        self._lock = threading.RLock()
        self._db.execute("CREATE TABLE IF NOT EXISTS credentials "
                         "(ref TEXT PRIMARY KEY, provider TEXT NOT NULL, encrypted BLOB NOT NULL, "
                         "revoked INT DEFAULT 0)")
        self._db.commit()

    def put(self, ref: str, provider: str, token: str) -> None:
        with self._lock, self._db:
            # A revoked reference must not be resurrected by replaying the same access token.
            self._db.execute("INSERT OR IGNORE INTO credentials(ref,provider,encrypted) VALUES(?,?,?)",
                             (ref, provider, self._cipher.encrypt(token.encode())))

    def get(self, ref: str) -> tuple[str, str]:
        with self._lock:
            row = self._db.execute("SELECT provider,encrypted FROM credentials WHERE ref=? AND revoked=0",
                                   (ref,)).fetchone()
        if row is None:
            raise AgentOSError(ErrorCode.DENIED, "external session unavailable or revoked")
        try:
            return str(row[0]), self._cipher.decrypt(row[1]).decode()
        except InvalidToken as exc:
            raise AgentOSError(ErrorCode.DENIED, "external session cannot be decrypted") from exc

    def revoke(self, ref: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE credentials SET revoked=1 WHERE ref=?", (ref,))


def _claim(claims: dict[str, Any], path: str) -> Any:
    value: Any = claims
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


class FederatedIdentity:
    policy_version = "federation-1"

    def __init__(self, local: IdentityPort, lanes: list[IdentityLane], vault: CredentialVault) -> None:
        if len({lane.id for lane in lanes}) != len(lanes) or len({lane.issuer for lane in lanes}) != len(lanes):
            raise ValueError("identity lane IDs and issuers must be unique")
        self._local, self._vault = local, vault
        self.lanes = {lane.id: lane for lane in lanes}
        self._issuers = {lane.issuer: lane.id for lane in lanes}
        self._keys: dict[str, tuple[float, dict[str, Any]]] = {}
        self._locks = {lane.id: asyncio.Lock() for lane in lanes}

    async def _json(self, url: str) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
                response = await client.get(url)
                response.raise_for_status()
                result: dict[str, Any] = response.json()
                return result
        except (httpx.HTTPError, ValueError) as exc:
            raise AgentOSError(ErrorCode.DENIED, "trusted identity service unavailable") from exc

    async def _jwks(self, lane: IdentityLane, *, force: bool = False) -> dict[str, Any]:
        async with self._locks[lane.id]:
            cached = self._keys.get(lane.id)
            if cached and not force and cached[0] > time.monotonic():
                return cached[1]
            url = lane.jwks_url
            if lane.kind == "oidc":
                discovery = await self._json(lane.discovery_url or lane.issuer.rstrip("/") +
                                             "/.well-known/openid-configuration")
                if discovery.get("issuer") != lane.issuer:
                    raise AgentOSError(ErrorCode.DENIED, "discovery issuer mismatch")
                discovered = str(discovery.get("jwks_uri") or "")
                if not discovered.startswith("https://") and not (lane.allow_http and discovered.startswith("http://")):
                    raise AgentOSError(ErrorCode.DENIED, "invalid trusted JWKS endpoint")
                url = url or discovered
            keys = await self._json(url)
            if not isinstance(keys.get("keys"), list):
                raise AgentOSError(ErrorCode.DENIED, "invalid trusted key set")
            self._keys[lane.id] = (time.monotonic() + lane.cache_ttl_s, keys)
            return keys

    async def _verify(self, lane: IdentityLane, token: str, ref: str) -> AuthContext:
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") not in lane.algorithms:
                raise AgentOSError(ErrorCode.DENIED, "untrusted token algorithm")
            signing_key: Any
            if lane.shared_secret_env:
                signing_key = os.environ.get(lane.shared_secret_env, "")
                if len(signing_key.encode()) < 32:
                    raise AgentOSError(ErrorCode.DENIED, "legacy signing secret unavailable or too short")
            else:
                if not header.get("kid"):
                    raise AgentOSError(ErrorCode.DENIED, "missing signing key ID")
                keys = await self._jwks(lane)
                matches = [k for k in keys["keys"] if k.get("kid") == header["kid"]]
                if not matches:
                    keys = await self._jwks(lane, force=True)
                    matches = [k for k in keys["keys"] if k.get("kid") == header["kid"]]
                if len(matches) != 1:
                    raise AgentOSError(ErrorCode.DENIED, "signing key unavailable")
                signing_key = jwt.PyJWK.from_dict(matches[0], algorithm=header["alg"]).key
            claims = jwt.decode(token, signing_key, algorithms=lane.algorithms, issuer=lane.issuer,
                                audience=lane.audience, leeway=lane.leeway_s,
                                options={"require": ["iss", "aud", "sub", "exp"]})
        except (jwt.InvalidTokenError, ValueError, KeyError, TypeError) as exc:
            raise AgentOSError(ErrorCode.DENIED, "external access token rejected") from exc
        if lane.introspection_url:
            secret = os.environ.get(lane.client_secret_env, "")
            try:
                async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                    response = await client.post(lane.introspection_url, data={"token": token},
                                                 auth=(lane.client_id, secret))
                    response.raise_for_status()
                    if response.json().get("active") is not True:
                        raise AgentOSError(ErrorCode.DENIED, "external token revoked")
            except (httpx.HTTPError, ValueError) as exc:
                raise AgentOSError(ErrorCode.DENIED, "token introspection unavailable") from exc
        tenant_value = _claim(claims, lane.tenant_claim)
        if isinstance(tenant_value, list):
            if len(tenant_value) != 1 or not isinstance(tenant_value[0], str):
                raise AgentOSError(ErrorCode.DENIED, "ambiguous organization claim")
            tenant_value = tenant_value[0]
        tenant = lane.fixed_tenant or lane.tenant_map.get(str(tenant_value or ""), "")
        if not tenant:
            raise AgentOSError(ErrorCode.DENIED, "organization is not mapped")
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            raise AgentOSError(ErrorCode.DENIED, "stable subject required")
        roles = _claim(claims, lane.roles_claim)
        roles = roles.split() if isinstance(roles, str) else roles
        if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
            roles = []
        scopes = sorted({s for r in roles for s in lane.scope_map.get(r, [])})
        mapped_roles = sorted({r for external in roles for r in lane.role_map.get(external, [])})
        if not scopes:
            raise AgentOSError(ErrorCode.DENIED, "no mapped permissions")
        expiry = min(datetime.fromtimestamp(int(claims["exp"]), UTC),
                     utc_now() + timedelta(seconds=lane.auth_ttl_s))
        if expiry <= utc_now():
            raise AgentOSError(ErrorCode.DENIED, "external token expired")
        subject = "external:" + hashlib.sha256(f"{lane.issuer}\0{sub}".encode()).hexdigest()
        return AuthContext(subject=subject, tenant_id=tenant, scopes=scopes, roles=mapped_roles,
                           policy_version=self.policy_version, expires_at=expiry,
                           identity_provider=lane.id, identity_ref=ref)

    async def authenticate(self, credentials: dict[str, Any]) -> AuthContext:
        token = str(credentials.get("token") or "")
        if token.startswith("sso."):
            ref = hashlib.sha256(token.encode()).hexdigest()
            provider, raw = self._vault.get(ref)
            lane = self.lanes.get(provider)
            if lane is None:
                raise AgentOSError(ErrorCode.DENIED, "identity provider disabled")
            return await self._verify(lane, raw, ref)
        try:
            claims = jwt.decode(token, options={"verify_signature": False})
        except jwt.InvalidTokenError:
            return await self._local.authenticate(credentials)
        selected = self._issuers.get(str(claims.get("iss") or ""))
        if selected is None:
            raise AgentOSError(ErrorCode.DENIED, "issuer is not configured")
        ref = hashlib.sha256(token.encode()).hexdigest()
        actor = await self._verify(self.lanes[selected], token, ref)
        self._vault.put(ref, selected, token)
        self._vault.get(ref)
        return actor

    async def issue_browser_session(self, provider: str, access_token: str) -> str:
        credential = "sso." + secrets.token_urlsafe(32)
        ref = hashlib.sha256(credential.encode()).hexdigest()
        await self._verify(self.lanes[provider], access_token, ref)
        self._vault.put(ref, provider, access_token)
        return credential

    async def refresh(self, actor: AuthContext) -> AuthContext:
        if actor.identity_provider == "local":
            return await self._local.refresh(actor)
        if actor.is_expired:
            raise AgentOSError(ErrorCode.DENIED, "identity authorization expired; re-authenticate")
        provider, token = self._vault.get(actor.identity_ref)
        lane = self.lanes.get(provider)
        if lane is None or provider != actor.identity_provider:
            raise AgentOSError(ErrorCode.DENIED, "identity provider disabled")
        fresh = await self._verify(lane, token, actor.identity_ref)
        if fresh.subject != actor.subject or fresh.tenant_id != actor.tenant_id:
            raise AgentOSError(ErrorCode.DENIED, "identity mapping changed; re-authenticate")
        return fresh.model_copy(update={"expires_at": min(fresh.expires_at, actor.expires_at)})

    def revoke(self, credential: str) -> None:
        self._vault.revoke(hashlib.sha256(credential.encode()).hexdigest())
