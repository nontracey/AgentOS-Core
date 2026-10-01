"""Worker：作业认领与驱动收敛（M0 单进程内联驱动 + 可选后台循环）。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time

from agentos.kernel.states import RUN_TERMINAL, RunState
from agentos.runtime.loop import DEFAULT_TENANT, AgentLoop


class Worker:
    def __init__(self, loop: AgentLoop, *, poll_interval_s: float = 0.05, owner: str = "worker") -> None:
        self._loop = loop
        self._poll = poll_interval_s
        self._owner = owner  # 唯一化：同名 owner 会让租约互斥失效（P0-1 修复）

    async def drive_until_settled(
        self, run_id: str, *, deadline_s: float = 30, owner: str | None = None, tenant_id: str = DEFAULT_TENANT
    ) -> str:
        """驱动 run 直到终态或阻塞（awaiting_approval/paused/awaiting_input）。

        返回最终状态字符串。取消请求在下一个循环边界生效（本地 ≤5s 量级）。
        """
        deadline = time.monotonic() + deadline_s
        state = ""
        while time.monotonic() < deadline:
            run = await self._loop.drive_run(run_id, owner=owner or self._owner, tenant_id=tenant_id)
            if run is None:
                return "missing"
            state = run.state.value if isinstance(run.state, RunState) else str(run.state)
            if run.state in RUN_TERMINAL:
                return state
            if run.state in {
                RunState.AWAITING_APPROVAL,
                RunState.AWAITING_INPUT,
                RunState.AWAITING_RESOURCES,
                RunState.PAUSED,
            }:
                return state
            await asyncio.sleep(self._poll)
        return state

    async def run_forever(self, stop: asyncio.Event) -> None:
        """后台作业循环：认领 FIFO 就绪作业并驱动对应 run。"""
        store = self._loop._deps.store  # noqa: SLF001 - worker 属于 runtime 内部协作
        cursor = 0
        while not stop.is_set():
            tenants = await asyncio.to_thread(store.list_job_tenants)
            job = None
            for offset in range(len(tenants)):
                index = (cursor + offset) % len(tenants)
                job = await asyncio.to_thread(store.claim_job, tenants[index], self._owner, 30)
                if job is not None:
                    cursor = index + 1
                    break
            if job is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self._poll)
                continue
            try:
                state = await self.drive_until_settled(job.run_id, deadline_s=60, tenant_id=job.tenant_id)
                # 正常返回也要终结作业：终态=done，阻塞/暂停=blocked（否则过期后被反复重认领）
                final = "done" if state in {"succeeded", "failed", "cancelled"} else "blocked"
                await asyncio.to_thread(
                    store.complete_job, job.tenant_id, job.id, self._owner, job.fence, final
                )
            except Exception as e:
                # 兜底：驱动器异常不得把 run 永久留在 RUNNING（队列耗尽根因，P1-2）
                run = store.get_run(job.tenant_id, job.run_id)
                if run is not None and run.state not in RUN_TERMINAL:
                    store.transition_run(
                        job.tenant_id, job.run_id, set(RunState) - RUN_TERMINAL, RunState.FAILED,
                        error_json=json.dumps(
                        {"code": "worker_error", "message": f"drive crashed: {e!r}"},
                        default=str,
                    ),
                    )
                await asyncio.to_thread(
                    store.complete_job, job.tenant_id, job.id, self._owner, job.fence, "blocked"
                )
