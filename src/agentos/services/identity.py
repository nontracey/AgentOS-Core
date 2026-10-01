"""身份服务（FR-ING-03/05、FR-GOV-01）：入口身份解析与派发前复核。

身份与外部渠道账号解耦：backend 只负责凭据→AuthContext；channel adapter 不感知身份实现。
"""

from __future__ import annotations

from typing import Any

from agentos.kernel.ports import IdentityPort
from agentos.kernel.types import AuthContext


class IdentityService:
    def __init__(self, backend: IdentityPort) -> None:
        self._backend = backend

    async def authenticate(self, credentials: dict[str, Any]) -> AuthContext:
        return await self._backend.authenticate(credentials)

    async def refresh(self, actor: AuthContext) -> AuthContext:
        """每次敏感读写与派发前重新核验当前权限（不信任创建时快照）。"""
        return await self._backend.refresh(actor)
