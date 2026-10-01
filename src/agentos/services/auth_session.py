"""登录会话服务（双 token 的 refresh 侧，G-N02 产品化登录；U4 扩展为任意可信 subject）。

- refresh token：随机 256bit，仅存 sha256 哈希（auth_sessions 表）；持久化期限由
  登录时"保持登录"决定（30 天 / 12 小时），游客会话另受 grant 到期上限约束。
- 轮换：每次 refresh 签发新 refresh 并作废旧的；**已作废 token 再次出现 = 视为
  泄露，撤销该主体全部会话**（重用检测，OAuth BCP）。
- access token：短时 HMAC 签名令牌（LocalTokenIdentity.mint_access_token），
  无状态验证，不落库。

U4-01 修正：refresh 不再固定签回 `owner`；会话行记录真实 subject，refresh 只能延续
**自己的** subject、scope 与到期，不能提升生命周期，也不能越过 grant 到期。
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.store import StoreProtocol
from agentos.kernel.types import utc_now

REFRESH_TTL_REMEMBER_S = 30 * 24 * 3600  # 保持登录 30 天
REFRESH_TTL_SESSION_S = 12 * 3600         # 未勾选：浏览器会话级（12 小时上限）
GUEST_REFRESH_TTL_S = 24 * 3600           # 游客会话上限 24 小时（管理员可收短）


@dataclass
class TokenPair:
    access_token: str
    access_expires_in: int
    refresh_token: str
    refresh_expires_in: int
    subject: str = "owner"
    role: str = "admin"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _remaining_seconds(expires_at: str) -> int:
    exp = datetime.fromisoformat(str(expires_at))
    return int(exp.timestamp() - utc_now().timestamp())


class AuthSessionService:
    def __init__(self, store: StoreProtocol, identity: Any, *,
                 access_ttl_s: int = 30 * 60) -> None:
        self._store = store
        self._identity = identity
        self._access_ttl = access_ttl_s

    # ------------------------------------------------------------- 管理员登录
    def login(self, *, password: str, remember: bool) -> TokenPair:
        """密码验证 → 签发 access + refresh。失败一律 DENIED，不区分错在哪。"""
        if not self._identity.verify_password(password):
            raise AgentOSError(ErrorCode.DENIED, "用户名或密码不正确")
        return self._issue_pair(subject="owner", remember=remember, role="admin")

    def _issue_pair(
        self,
        *,
        subject: str,
        remember: bool,
        role: str,
        tenant_id: str = "default",
        max_ttl_s: int | None = None,
        hard_expiry: str | None = None,
    ) -> TokenPair:
        """为**指定主体**签发票据；不把任何主体默认成 owner。"""
        now = utc_now()
        refresh_ttl = REFRESH_TTL_REMEMBER_S if remember else REFRESH_TTL_SESSION_S
        if max_ttl_s is not None:
            refresh_ttl = min(refresh_ttl, max_ttl_s)
        expires_at = now + timedelta(seconds=refresh_ttl)
        if hard_expiry:
            # 刷新不能越过 grant 到期：不推断"剩余时间长"来提升生命周期
            hard = datetime.fromisoformat(hard_expiry)
            if hard <= now:
                raise AgentOSError(ErrorCode.DENIED, "demo grant expired")
            expires_at = min(expires_at, hard)
            refresh_ttl = max(1, int((expires_at - now).total_seconds()))
        access_ttl = min(self._access_ttl, refresh_ttl)
        refresh = secrets.token_urlsafe(32)
        self._store.insert_auth_session(
            tenant_id, subject, f"s_{secrets.token_hex(8)}",
            _hash(refresh), expires_at.isoformat(),
        )
        return TokenPair(
            access_token=self._identity.mint_access_token(subject, ttl_s=access_ttl),
            access_expires_in=access_ttl,
            refresh_token=refresh,
            refresh_expires_in=refresh_ttl,
            subject=subject,
            role=role,
        )

    def refresh(self, refresh_token: str) -> TokenPair:
        """轮换：旧 refresh 作废、签发新对；作废 token 再现 → 全部会话撤销。

        新票据沿用**会话行里的 subject**，不再固定 owner；游客会话继续受 grant 约束。
        """
        if not refresh_token:
            raise AgentOSError(ErrorCode.DENIED, "missing refresh token")
        row = self._store.find_auth_session_by_hash("default", _hash(refresh_token))
        if row is None:
            raise AgentOSError(ErrorCode.DENIED, "invalid refresh token")
        if row["revoked"]:
            # 重用已作废 refresh：按泄露处理，撤销该主体全部会话（fail closed）
            self._store.revoke_all_auth_sessions("default", str(row["subject"]))
            raise AgentOSError(ErrorCode.DENIED, "refresh token reuse detected; all sessions revoked")
        if str(row["expires_at"]) <= utc_now().isoformat():
            raise AgentOSError(ErrorCode.DENIED, "refresh token expired")
        subject = str(row["subject"])
        self._store.revoke_auth_session("default", str(row["id"]))
        role, hard_expiry, max_ttl = "admin", None, None
        if subject.startswith("guest:"):
            role = "guest"
            max_ttl = GUEST_REFRESH_TTL_S
            grant = self._store.get_demo_grant_by_subject("default", subject)
            if grant is None or str(grant.get("status")) != "active":
                raise AgentOSError(ErrorCode.DENIED, "demo grant revoked or expired")
            hard_expiry = str(grant.get("expires_at") or "") or None
        remember = _remaining_seconds(row["expires_at"]) > REFRESH_TTL_SESSION_S // 2
        try:
            return self._issue_pair(
                subject=subject, remember=remember, role=role,
                max_ttl_s=max_ttl, hard_expiry=hard_expiry,
            )
        except AgentOSError:
            # 主体已被撤销/过期：不签发任何票据
            self._store.revoke_all_auth_sessions("default", subject)
            raise

    # ------------------------------------------------------------- 游客免登（U5-01）
    def issue_public_guest_pair(self, subject: str, *, hard_expiry: str) -> TokenPair:
        """为刚建立的免登访客主体签会话：不碰管理员凭据，不越过 grant 到期。

        与兑换码同一会话形态（session 级 refresh、24h 上限、受 grant 硬到期），
        只是准入不再要求代码——服务端已在 DemoService 里裁定开关/发布/名额。
        """
        return self._issue_pair(
            subject=subject, remember=False, role="guest",
            max_ttl_s=GUEST_REFRESH_TTL_S, hard_expiry=hard_expiry,
        )

    # ------------------------------------------------------------- 游客邀请兑换
    def redeem_invite(self, code: str) -> TokenPair:
        """兑换码 → 游客会话。库内只存 hash，兑换只成功一次，不共享管理员凭据。"""
        if not code:
            raise AgentOSError(ErrorCode.DENIED, "missing invite code")
        row = self._store.find_demo_invite_by_hash("default", _hash(code.strip()))
        if row is None:
            raise AgentOSError(ErrorCode.DENIED, "invalid invite code")
        if str(row.get("expires_at")) <= utc_now().isoformat():
            raise AgentOSError(ErrorCode.DENIED, "invite code expired")
        grant = self._store.get_demo_grant("default", str(row["grant_id"]))
        if grant is None or str(grant.get("status")) != "active":
            raise AgentOSError(ErrorCode.DENIED, "demo grant not active")
        now_iso = utc_now().isoformat()
        invite = self._store.mark_demo_invite_redeemed(
            "default", str(row["id"]), now_iso)
        if invite is None or invite.get("redeemed_at") != now_iso:
            # 并发或已兑换过：一次性码不能二次兑换
            raise AgentOSError(ErrorCode.DENIED, "invite code already redeemed")
        subject = str(grant["subject"])
        hard_expiry = str(grant["expires_at"])
        self._identity.register_guest_subject(
            subject, grant_id=str(grant["id"]), expires_at=hard_expiry
        )
        return self._issue_pair(
            subject=subject, remember=False, role="guest",
            max_ttl_s=GUEST_REFRESH_TTL_S, hard_expiry=hard_expiry,
        )

    def revoke(self, refresh_token: str) -> None:
        """登出：作废该会话（token 即凭据，无需再验 access）。"""
        row = self._store.find_auth_session_by_hash("default", _hash(refresh_token))
        if row is not None:
            self._store.revoke_auth_session("default", str(row["id"]))

    def revoke_all(self, subject: str = "owner") -> None:
        self._store.revoke_all_auth_sessions("default", subject)
