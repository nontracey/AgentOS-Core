"""本地文件令牌身份适配器（FR-ING-05：身份与渠道解耦；M0 单人受信身份）。

- 令牌文件位于数据目录（0600），首次 boot 自动生成 owner 令牌；真实凭据不入仓库。
- authenticate 未命中 → DENIED（默认拒绝）；AuthContext 只由本适配器签发。
- refresh 每次派发前重新读取当前 scopes/有效期（撤权即时生效）。
- 产品登录（双 token）：密码 PBKDF2 校验 + HMAC 短时 access 令牌（无状态验证）；
  长时 refresh 的轮换/撤销由 AuthSessionService + auth_sessions 表承担。
  静态 owner 令牌保留（CLI/自动化路径不变）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import AuthContext, utc_now

OWNER_SCOPES = ["read", "write", "approve", "exec", "egress"]
_PBKDF2_ITERATIONS = 200_000
_ACCESS_TOKEN_TTL_S = 30 * 60  # 双 token：access 短时；refresh 轮换由会话服务承担


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    ).hex()


class LocalTokenIdentity:
    policy_version = "personal-m0-1"

    def __init__(self, identity_file: Path, *, token_ttl_s: int = 3600) -> None:
        self._file = identity_file
        self._ttl = token_ttl_s

    def ensure_owner(self) -> str:
        """初始化 owner 身份；返回明文令牌（只在创建时展示一次，由数据目录保存）。

        同时初始化产品登录：无密码则生成初始密码，明文一次性写入
        `<data>/owner_password.once`（0600），修改密码后该文件自动删除。
        """
        self._file.parent.mkdir(parents=True, exist_ok=True)
        if self._file.exists():
            data = json.loads(self._file.read_text(encoding="utf-8"))
            if "auth" not in data:
                self._init_auth(data)
            return str(data["subjects"]["owner"]["token"])
        token = secrets.token_urlsafe(32)
        payload: dict[str, Any] = {
            "subjects": {
                # U4-01：owner 映射为管理员（访问角色 admin 由可信身份层写入）
                "owner": {
                    "token": token, "scopes": OWNER_SCOPES,
                    "tenant_id": "default", "role": "admin",
                }
            }
        }
        self._init_auth(payload)
        return token

    def _init_auth(self, data: dict[str, Any]) -> None:
        """生成初始密码（一次可见）并写入哈希；identity.json 以 0600 重写。"""
        password = secrets.token_urlsafe(9)  # ~12 字符
        salt = secrets.token_bytes(16)
        data["auth"] = {
            "password_salt": salt.hex(),
            "password_hash": hash_password(password, salt),
        }
        fd = os.open(self._file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        once = self._file.parent / "owner_password.once"
        fd2 = os.open(once, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd2, "w", encoding="utf-8") as fh:
            fh.write(password + "\n")

    # ---------------------------------------------------------------- 密码
    def verify_password(self, password: str) -> bool:
        auth = self._load_json().get("auth")
        if not auth:
            return False
        salt = bytes.fromhex(str(auth["password_salt"]))
        expected = str(auth["password_hash"])
        actual = hash_password(password or "", salt)
        return hmac.compare_digest(actual, expected)

    def set_password(self, password: str) -> None:
        """设置新密码并删除一次性初始密码文件（首次设置后不可再恢复）。"""
        if not password or len(password) < 8:
            raise AgentOSError(ErrorCode.INVALID, "password must be at least 8 characters")
        data = self._load_json()
        salt = secrets.token_bytes(16)
        data["auth"] = {
            "password_salt": salt.hex(),
            "password_hash": hash_password(password, salt),
        }
        fd = os.open(self._file, os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        (self._file.parent / "owner_password.once").unlink(missing_ok=True)

    def initial_password_pending(self) -> bool:
        """初始密码文件还在 = 尚未完成首次改密（登录页据此提示）。"""
        return (self._file.parent / "owner_password.once").exists()

    def _load_json(self) -> dict[str, Any]:
        if not self._file.exists():
            raise AgentOSError(ErrorCode.DENIED, "identity store missing; run agentos boot first")
        data: dict[str, Any] = json.loads(self._file.read_text(encoding="utf-8"))
        return data

    # ------------------------------------------------- 短时 access 令牌（无状态）
    def mint_access_token(self, subject: str, *, ttl_s: int = _ACCESS_TOKEN_TTL_S) -> str:
        payload = {"sub": subject, "exp": int(time.time()) + ttl_s, "typ": "access"}
        body = _b64url(json.dumps(payload, separators=(",", ":")).encode())
        sig = hmac.new(self._access_signing_key(), body.encode(), hashlib.sha256).hexdigest()
        return f"{body}.{sig}"

    def verify_access_token(self, token: str) -> AuthContext | None:
        """验证签名与有效期；命中返回对应主体的 AuthContext（每次重读身份文件）。"""
        try:
            body, sig = token.rsplit(".", 1)
            expected = hmac.new(
                self._access_signing_key(), body.encode(), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(sig, expected):
                return None
            payload = json.loads(_b64url_decode(body))
            if payload.get("typ") != "access" or int(payload["exp"]) < time.time():
                return None
            subject = str(payload["sub"])
            entry = self._load().get(subject)
            if entry is None or entry.get("revoked"):
                return None
            return self._issue(subject, entry,
                               expires_in_s=max(1, int(payload["exp"]) - int(time.time())))
        except Exception:  # noqa: BLE001 - 任何畸形令牌一律拒绝
            return None

    def _access_signing_key(self) -> bytes:
        """access 令牌签名密钥：由 owner 令牌派生（不新增秘密存储）。"""
        owner = str(self._load()["owner"]["token"])
        return hashlib.sha256(f"access-token:{owner}".encode()).digest()

    # ---------------------------------------------------------------- 原有路径
    def _load(self) -> dict[str, dict[str, object]]:
        subjects = self._load_json()["subjects"]
        assert isinstance(subjects, dict)
        return subjects

    def _issue(self, subject: str, entry: dict[str, object], *,
               expires_in_s: int | None = None) -> AuthContext:
        if entry.get("revoked"):
            raise AgentOSError(ErrorCode.DENIED, f"subject {subject} revoked")
        ttl = expires_in_s if expires_in_s is not None else self._ttl
        entry_expires = entry.get("expires_at")
        if isinstance(entry_expires, str) and entry_expires:
            # 游客主体带绝对到期：身份层即 fail-closed，不靠调用方记得查
            if utc_now().isoformat() >= entry_expires:
                raise AgentOSError(ErrorCode.DENIED, f"subject {subject} expired")
            remaining = int(
                (datetime.fromisoformat(entry_expires) - utc_now()).total_seconds()
            )
            ttl = max(1, min(ttl, remaining))
        role = str(entry.get("role") or "")
        return AuthContext(
            subject=subject,
            tenant_id=str(entry["tenant_id"]),
            scopes=list(entry["scopes"]) if isinstance(entry["scopes"], list) else [],
            policy_version=self.policy_version,
            expires_at=utc_now() + timedelta(seconds=ttl),
            roles=[role] if role else [],
        )

    # ------------------------------------------------- U4：游客主体（独立 subject）
    def register_guest_subject(
        self, subject: str, *, grant_id: str, expires_at: str, tenant_id: str = "default"
    ) -> None:
        """登记一位游客的独立 subject：无静态令牌（不能走 CLI/静态令牌路径），只读 scope。

        邀请兑换时写入；撤销 grant 时由 revoke_subject 禁用。绝不接受客户端自报。
        """
        data = self._load_json()
        subjects = data.setdefault("subjects", {})
        subjects[subject] = {
            "token": "",  # 空令牌：静态令牌路径永不命中
            "scopes": ["read"],
            "tenant_id": tenant_id,
            "role": "guest",
            "grant_id": grant_id,
            "expires_at": expires_at,
        }
        self._write_json(data)

    def revoke_subject(self, subject: str) -> None:
        """撤销主体（撤权即时生效）：后续签发/刷新一律 DENIED。"""
        if subject == "owner":
            return
        data = self._load_json()
        if subject in data.get("subjects", {}):
            data["subjects"][subject]["revoked"] = True
            data["subjects"][subject]["scopes"] = []
            self._write_json(data)

    def unban_subject(self, subject: str) -> None:
        """解除撤销标记（管理员重新开放默认演示时复用同一主体）。"""
        if subject == "owner":
            return
        data = self._load_json()
        entry = data.get("subjects", {}).get(subject)
        if entry is not None:
            entry.pop("revoked", None)
            entry["scopes"] = ["read"]
            self._write_json(data)

    def prune_expired_subjects(self) -> int:
        """删除已过期的游客主体（只动 `guest:` 前缀）：保持身份文件有界。

        已过期主体的 access/refresh 已各自失效，这里不改变任何授权事实。
        """
        data = self._load_json()
        subjects = data.get("subjects", {})
        now_iso = utc_now().isoformat()
        removed = 0
        for name in list(subjects):
            if not name.startswith("guest:"):
                continue
            entry = subjects[name] or {}
            expires_at = str(entry.get("expires_at") or "")
            if entry.get("revoked") or (expires_at and expires_at <= now_iso):
                del subjects[name]
                removed += 1
        if removed:
            self._write_json(data)
        return removed

    def _write_json(self, data: dict[str, Any]) -> None:
        fd = os.open(self._file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)

    async def authenticate(self, credentials: dict[str, object]) -> AuthContext:
        token = credentials.get("token")
        if not isinstance(token, str) or not token:
            raise AgentOSError(ErrorCode.DENIED, "missing credentials")
        # 先验证签名 access 令牌（登录会话签发），再回退静态令牌（CLI/自动化）
        access = self.verify_access_token(token)
        if access is not None:
            return access
        for subject, entry in self._load().items():
            stored = str(entry.get("token") or "")
            # 游客主体没有静态令牌：空串永不命中，防止用空令牌冒充游客
            if stored and secrets.compare_digest(token, stored):
                return self._issue(subject, entry)
        raise AgentOSError(ErrorCode.DENIED, "credentials rejected")

    async def refresh(self, actor: AuthContext) -> AuthContext:
        """重读身份文件：撤权/收窄 scope 立即生效（M0 以重写文件模拟撤权）。"""
        subjects = self._load()
        entry = subjects.get(actor.subject)
        if entry is None or entry.get("revoked"):
            raise AgentOSError(ErrorCode.DENIED, f"subject {actor.subject} revoked")
        refreshed = self._issue(actor.subject, entry)
        if refreshed.tenant_id != actor.tenant_id:
            raise AgentOSError(ErrorCode.DENIED, "subject organization changed; re-authenticate")
        return refreshed

    def signing_key(self) -> str:
        """派生资源 URL 签名密钥（仅服务端持有）：由 owner 令牌单向散列，不落第二份存储。"""
        owner = str(self._load()["owner"]["token"])
        return hashlib.sha256(f"asset-url:{owner}".encode()).hexdigest()
