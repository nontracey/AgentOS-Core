"""In-process workflow engine for single-process deployments without a Temporal service.

It follows the same flow semantics as the Temporal ``IntegrationFlow``: dependency order,
skipped branches, ``wait`` timers, retrying busy resources and pausing blocked steps until a
signal (approval, reconciliation). AgentOS still owns every node's authorization, inputs,
checkpoint and side-effect ledger through ``WorkflowDriver.node``; this adapter only routes.

Durability is the checkpoint's: a restarted process resumes unfinished flows, nodes already
completed are not run again and an interrupted tool step is reconciled by the ledger rather
than replayed. Timers restart from zero after a restart. Multi-process or long-timer
deployments should keep using Temporal.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

NodeCall = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
FinishCall = Callable[[dict[str, Any]], Awaitable[None]]
PendingFlows = Callable[[str], list[tuple[str, dict[str, Any]]]]

_ATTEMPTS = 3  # same as the Temporal activity retry policy
_BLOCKED_RECHECK_S = 60.0


class LocalWorkflowAdapter:
    name = "local"

    def __init__(self, node: NodeCall, finish: FinishCall, *, pending: PendingFlows | None = None) -> None:
        self.node, self.finish, self.pending = node, finish, pending
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.revision: dict[str, int] = {}
        self._changed: dict[str, asyncio.Condition] = {}

    async def open(self) -> None:
        for handle, binding in (self.pending(self.name) if self.pending else []):
            await self.start(handle, binding)

    async def close(self) -> None:
        tasks = [t for t in self.tasks.values() if not t.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()

    async def start(self, workflow_id: str, binding: dict[str, Any]) -> None:
        current = self.tasks.get(workflow_id)
        if current is not None and not current.done():
            return  # the stable handle is the idempotency key, as with Temporal workflow IDs
        self.revision.setdefault(workflow_id, 0)
        self.tasks[workflow_id] = asyncio.create_task(self._flow(workflow_id, binding),
                                                      name=f"workflow:{workflow_id[:24]}")

    async def signal(self, workflow_id: str) -> None:
        self.revision[workflow_id] = self.revision.get(workflow_id, 0) + 1
        condition = self._condition(workflow_id)
        async with condition:
            condition.notify_all()

    async def cancel(self, workflow_id: str) -> None:
        task = self.tasks.get(workflow_id)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def running(self) -> int:
        return sum(1 for task in self.tasks.values() if not task.done())

    def _condition(self, workflow_id: str) -> asyncio.Condition:
        if workflow_id not in self._changed:
            self._changed[workflow_id] = asyncio.Condition()
        return self._changed[workflow_id]

    async def _wait_for_signal(self, workflow_id: str, seen: int) -> None:
        condition = self._condition(workflow_id)
        async with condition:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(condition.wait_for(lambda: self.revision.get(workflow_id, 0) > seen),
                                       timeout=_BLOCKED_RECHECK_S)

    async def _flow(self, workflow_id: str, binding: dict[str, Any]) -> None:
        tenant, run_id = binding["tenant_id"], binding["run_id"]
        done: dict[str, dict[str, Any]] = {}

        async def run_node(node: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            if any(done[k].get("status") == "skipped" for k in node["depends_on"]):
                return node["id"], {"status": "skipped"}
            if any(done[k].get("status") == "skipped" or done[k].get("branch") != v
                   for k, v in node["when"].items()):
                return node["id"], {"status": "skipped"}
            if node["kind"] == "wait":
                await asyncio.sleep(int(node.get("seconds") or 0))
            failures = 0
            while True:
                seen = self.revision.get(workflow_id, 0)
                try:
                    result = await self.node({"tenant_id": tenant, "run_id": run_id, "node_id": node["id"]})
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - same bounded retry as the Temporal activity
                    failures += 1
                    log.warning("workflow node %s failed (attempt %d)", node["id"], failures, exc_info=True)
                    if failures >= _ATTEMPTS:
                        return node["id"], {"status": "failed"}
                    await asyncio.sleep(2 * failures)
                    continue
                if result.get("status") == "resource_busy":
                    await asyncio.sleep(0.5)
                    continue
                if result.get("status") == "blocked":
                    await self._wait_for_signal(workflow_id, seen)
                    continue
                return node["id"], result

        try:
            pending = list(binding["schedule"])
            while pending:
                ready = [n for n in pending if set(n["depends_on"]) <= set(done)]
                pending = [n for n in pending if n not in ready]
                results = await asyncio.gather(*(run_node(n) for n in ready))
                done.update(results)
                if any(result["status"] not in ("completed", "skipped") for _, result in results):
                    await self.finish({"tenant_id": tenant, "run_id": run_id, "state": "failed"})
                    return
            await self.finish({"tenant_id": tenant, "run_id": run_id, "state": "succeeded"})
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a broken flow must still settle its run
            log.exception("local workflow %s stopped unexpectedly", workflow_id)
            with contextlib.suppress(Exception):
                await self.finish({"tenant_id": tenant, "run_id": run_id, "state": "failed"})
