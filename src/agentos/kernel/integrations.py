"""Extension contracts for enterprise systems; adapters own existing-system protocols.

These are contracts, not an installed project/quota/IM product. Identities must be
verified before constructing these requests. Raw IM sender IDs are not authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from agentos.kernel.types import AuthContext


class ProjectPort(Protocol):
    async def list_projects(self, actor: AuthContext) -> list[dict[str, Any]]: ...
    async def get_project(self, actor: AuthContext, external_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class QuotaReservation:
    reservation_id: str
    allowed: bool
    upper_tokens: int


class EnterpriseQuotaPort(Protocol):
    async def reserve(self, actor: AuthContext, run_id: str, call_id: str,
                      upper_tokens: int) -> QuotaReservation: ...
    async def settle(self, actor: AuthContext, reservation_id: str, actual_tokens: int | None) -> None: ...


@dataclass(frozen=True)
class VerifiedChannelMessage:
    channel: str
    message_id: str
    credential_ref: str
    goal: str


class ChannelPort(Protocol):
    async def verify(self, headers: dict[str, str], body: bytes) -> VerifiedChannelMessage: ...
