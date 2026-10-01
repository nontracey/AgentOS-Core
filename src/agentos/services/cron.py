"""Cron 定时入口（FR-ING-01 四类入口的最后一块：CLI/HTTP/Webhook/Cron）。

以受限服务身份按周期构造 Envelope，走 **同一 accept 路径**（身份/幂等/预算/
注入防护零旁路）。幂等键 = `cron:{job_id}:{窗口序号}`——同窗口重复触发安全重放，
跨窗口产生新 run（定时任务的自然语义）。

- 以 owner 令牌作为服务身份（M0 单人形态；企业形态换服务账号，接口不变）。
- max_runs 硬上限：测试与安全护栏，防止失控调度无限产生 run。
- 审计：cron.fired（含 job_id/窗口）、cron.registered/unregistered。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any

from agentos.services.ingress import IngressService


@dataclass
class CronJob:
    job_id: str
    goal: str
    interval_s: float
    enabled: bool = True
    max_runs: int = 0  # 0 = 无上限
    created_at: float = field(default_factory=time.monotonic)


class CronScheduler:
    """最小 cron 调度器：注册/注销/列出 + 后台循环驱动。"""

    def __init__(self, ingress: IngressService, audit: Any,
                 service_credentials: dict[str, Any],
                 clock: Any = time.monotonic) -> None:
        self._ingress = ingress
        self._audit = audit
        self._creds = service_credentials
        self._clock = clock  # 可注入假时钟（测试/回放）
        self._jobs: dict[str, CronJob] = {}
        self._last_fired: dict[str, int] = {}  # job_id -> 上次窗口序号
        self._fire_counts: dict[str, int] = {}

    def register(self, job_id: str, goal: str, interval_s: float,
                 max_runs: int = 0) -> CronJob:
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        job = CronJob(job_id=job_id, goal=goal, interval_s=interval_s,
                      max_runs=max_runs, created_at=self._clock())
        self._jobs[job_id] = job
        self._audit.record("default", "cron.registered", subject=None,
                           detail={"job_id": job_id, "interval_s": interval_s})
        return job

    def unregister(self, job_id: str) -> bool:
        removed = self._jobs.pop(job_id, None) is not None
        if removed:
            self._audit.record("default", "cron.unregistered", subject=None,
                               detail={"job_id": job_id})
        return removed

    def list_jobs(self) -> list[dict[str, Any]]:
        return [{"job_id": j.job_id, "goal": j.goal, "interval_s": j.interval_s,
                 "enabled": j.enabled, "fire_count": self._fire_counts.get(j.job_id, 0)}
                for j in self._jobs.values()]

    async def _fire(self, job: CronJob, window: int) -> None:
        key = f"cron:{job.job_id}:{window}"
        await self._ingress.accept(
            {"payload": {"goal": job.goal}, "idempotency_key": key},
            channel="cron", credentials=self._creds,
        )
        self._fire_counts[job.job_id] = self._fire_counts.get(job.job_id, 0) + 1
        self._audit.record("default", "cron.fired", subject=None,
                           detail={"job_id": job.job_id, "window": window})

    async def tick(self, now: float | None = None) -> int:
        """单次调度推进（可测）：对每个到期 job 触发一次；返回触发数。"""
        now = self._clock() if now is None else now
        fired = 0
        for job in list(self._jobs.values()):
            if not job.enabled:
                continue
            if job.max_runs and self._fire_counts.get(job.job_id, 0) >= job.max_runs:
                continue
            window = int((now - job.created_at) / job.interval_s)
            if window <= self._last_fired.get(job.job_id, -1):
                continue
            self._last_fired[job.job_id] = window
            try:
                await self._fire(job, window)
                fired += 1
            except Exception as exc:  # noqa: BLE001 - 单次触发失败不拖垮调度循环
                self._audit.record("default", "cron.error", subject=None,
                                   detail={"job_id": job.job_id,
                                           "error": str(exc)[:160]})
        return fired

    async def run_forever(self, stop: asyncio.Event, *, poll_s: float = 1.0) -> None:
        while not stop.is_set():
            await self.tick()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll_s)
