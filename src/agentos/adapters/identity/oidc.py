"""OIDC JWT 身份后端（FR-GOV-07 企业 OIDC 切片）：验证自建 IdP 签发的 bearer JWT。

- 签名验证：JWKS（kid 选择，PyJWKClient 自带缓存），RS256；PyCA cryptography 后端。
- iss / aud / exp 严格校验，任一不符 → DENIED（fail-closed，FR-GOV-01 语义）。
- 角色映射（默认拒绝）：realm_access.roles 中 role_reader→[read]、role_operator→
  [read,write,approve]；未映射角色不授任何 scope。
- 组织隔离：claims["org"] → tenant_id（两组织互为独立租户，跨租户访问由既有
  403 机制拦截）。
- 凭据形态分派：含两个"."的 bearer 视为 JWT 走 OIDC；其余回落本地令牌后端
  （owner 本地令牌与企业 IdP 并存，互不影响）。
- 撤权语义：JWT 无状态，已签发令牌在 exp 前仍有效——撤权通过 IdP 禁用用户
  （新 token 拿不到）+ 短 TTL + refresh 复验实现，如实标注（不冒充即时全局撤权）。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import jwt

from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import AuthContext, utc_now

_ROLE_SCOPES: dict[str, list[str]] = {
    "role_reader": ["read"],
    "role_operator": ["read", "write", "approve"],
}
TOKEN_TTL_S = 300  # 企业身份短 TTL：撤权窗口收敛（刷新走 IdP refresh flow）


class OIDCJwtIdentity:
    policy_version = "oidc-f6-1"

    def __init__(
        self,
        issuer: str,
        audience: str,
        *,
        local_fallback: Any | None = None,
        http_timeout_s: float = 10.0,
    ) -> None:
        self._issuer = issuer.rstrip("/")
        self._audience = audience
        self._fallback = local_fallback
        self._timeout = http_timeout_s
        self._jwks_client = jwt.PyJWKClient(
            f"{self._issuer}/protocol/openid-connect/certs",
            cache_keys=True, timeout=http_timeout_s,
        )

    def _decode(self, token: str) -> dict[str, Any]:
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
            return jwt.decode(
                token, signing_key.key,
                algorithms=["RS256"], audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AgentOSError(ErrorCode.DENIED, "token expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise AgentOSError(ErrorCode.DENIED, "audience mismatch") from exc
        except jwt.InvalidIssuerError as exc:
            raise AgentOSError(ErrorCode.DENIED, "issuer mismatch") from exc
        except jwt.PyJWKClientError as exc:
            # JWKS 不可达/kid 未知：fail-closed，不降级放行
            raise AgentOSError(ErrorCode.DENIED, f"jwks unavailable: {exc}") from exc
        except jwt.InvalidSignatureError as exc:
            raise AgentOSError(ErrorCode.DENIED, "signature verification failed") from exc
        except jwt.InvalidTokenError as exc:
            raise AgentOSError(ErrorCode.DENIED, f"invalid token: {exc}") from exc

    @staticmethod
    def _scopes_from(claims: dict[str, Any]) -> list[str]:
        roles = list((claims.get("realm_access") or {}).get("roles") or [])
        scopes: list[str] = []
        for role in roles:
            for s in _ROLE_SCOPES.get(role, []):
                if s not in scopes:
                    scopes.append(s)
        return scopes  # 未映射角色 → 空 scopes（默认拒绝，Policy 仍会拦写操作）

    def _issue(self, claims: dict[str, Any]) -> AuthContext:
        subject = str(claims.get("preferred_username") or claims["sub"])
        org = claims.get("org")
        if isinstance(org, list):
            org = org[0] if org else "default"
        return AuthContext(
            subject=subject,
            tenant_id=str(org or "default"),
            scopes=self._scopes_from(claims),
            policy_version=self.policy_version,
            expires_at=utc_now() + timedelta(seconds=TOKEN_TTL_S),
        )

    async def authenticate(self, credentials: dict[str, object]) -> AuthContext:
        token = credentials.get("token")
        if not isinstance(token, str) or not token:
            raise AgentOSError(ErrorCode.DENIED, "missing credentials")
        if token.count(".") != 2:  # 非 JWT 形态 → 本地令牌后端
            if self._fallback is not None:
                actor: AuthContext = await self._fallback.authenticate(credentials)
                return actor
            raise AgentOSError(ErrorCode.DENIED, "credentials rejected")
        claims = self._decode(token)
        return self._issue(claims)

    async def refresh(self, actor: AuthContext) -> AuthContext:
        """JWT 无状态：refresh 以既有 claims 复核有效期（过期即拒）。

        真实撤权路径 = IdP 禁用用户（拒绝新 token）+ 短 TTL 收敛窗口；
        本方法不延长有效期（不冒充即时全局撤权，FR-GOV-07 边界如实标注）。
        """
        if actor.expires_at is not None and actor.expires_at <= utc_now():
            raise AgentOSError(ErrorCode.DENIED, "token expired; re-authenticate via IdP")
        return actor
