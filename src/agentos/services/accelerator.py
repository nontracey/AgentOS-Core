"""Optional accelerator lease for one card shared by a chat model server and a generation node.

The generation node keeps its own admission queue for the jobs it receives, but it cannot see
requests that go straight to a chat model server on the same machine. When a deployment says
the two share a card (chat endpoints with ``gpu_shared`` and ``accelerator_nodes``), this lease
keeps the control plane from sending a chat generation and a node GPU job at the same time,
first come first served. Nothing is leased unless a deployment opts in.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

log = logging.getLogger(__name__)

ReleaseHook = Callable[[], Awaitable[None]]


class AcceleratorLease:
    def __init__(self) -> None:
        self._lock: asyncio.Lock | None = None
        self.holder: dict[str, Any] | None = None
        self.waiting: list[dict[str, Any]] = []
        # Called once a generation job holds the card, e.g. to ask chat servers to unload models.
        self.before_generation: list[ReleaseHook] = []

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @asynccontextmanager
    async def hold(self, kind: str, label: str, *, detail: dict[str, Any] | None = None) -> AsyncIterator[str]:
        """``kind`` is ``chat`` or ``generation``; the label is shown to the person waiting."""
        ticket: dict[str, Any] = {"id": uuid.uuid4().hex, "kind": kind, "label": label,
                                  "queued_at": time.time(), **(detail or {})}
        self.waiting.append(ticket)
        try:
            await self.lock.acquire()
        finally:
            self.waiting = [t for t in self.waiting if t["id"] != ticket["id"]]
        self.holder = {**ticket, "since": time.time()}
        try:
            if kind != "chat":
                for hook in list(self.before_generation):
                    try:
                        await hook()
                    except Exception:  # noqa: BLE001 - freeing memory is best effort
                        log.warning("accelerator release hook failed", exc_info=True)
            yield str(ticket["id"])
        finally:
            self.holder = None
            self.lock.release()

    def update(self, **fields: Any) -> None:
        if self.holder is not None:
            self.holder.update(fields)

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        holder = None
        if self.holder is not None:
            holder = {**self.holder, "elapsed_seconds": round(now - float(self.holder["since"]), 1)}
        return {"busy": self.holder is not None, "holder": holder,
                "waiting": [{**t, "waited_seconds": round(now - float(t["queued_at"]), 1)} for t in self.waiting]}
