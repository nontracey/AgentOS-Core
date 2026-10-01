"""FR-ING-01 Cron 入口验收：四类入口（CLI/HTTP/Webhook/Cron）同一路径。

- Cron 以服务身份走同一 accept（身份/幂等/预算零旁路）
- 幂等键按窗口：同窗口重放安全（重复触发不产生新 run）
- max_runs 护栏；unregister 后不再触发
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agentos.apps.composition import SystemHandle
from agentos.services.cron import CronScheduler
from tests.conftest import owner_token


async def _setup(system: SystemHandle, clock: Any) -> tuple[CronScheduler, str]:
    token = owner_token(system)
    sched = CronScheduler(system.ingress, system.deps.audit,
                          service_credentials={"token": token}, clock=clock)
    return sched, token


async def test_cron_fires_through_same_accept_path(
    make_system: Callable[..., SystemHandle],
) -> None:
    """定时触发走同一 accept：run 创建、channel=cron、审计有 cron.fired。"""
    system = make_system(model_script=[{"content": "cron done"}])
    system.boot()
    clock = {"now": 10.0}
    sched, _ = await _setup(system, lambda: clock["now"])
    sched.register("heartbeat", "每小时巡检一次", interval_s=0.3, max_runs=2)

    # 推进两个窗口
    clock["now"] = 10.1
    await sched.tick()
    clock["now"] = 10.5
    await sched.tick()
    assert sched._fire_counts["heartbeat"] == 2  # noqa: SLF001 - 断言内部计数

    runs = system.store.list_runs("default")
    assert len(runs) >= 2
    ev_kinds = {e["kind"] for r in runs
                for e in system.store.event_list("default", r.id, limit=10)}
    # 入口审计 + job 队列存在（同一路径的证据）
    audit_kinds = {r["kind"] for r in system.store.raw_connection().execute(
        "SELECT kind FROM audit_log WHERE kind LIKE 'cron%'").fetchall()}
    assert "cron.fired" in audit_kinds and "cron.registered" in audit_kinds
    assert ev_kinds  # run 有事件产生


async def test_cron_same_window_idempotent(
    make_system: Callable[..., SystemHandle],
) -> None:
    """同窗口重复 tick → 幂等重放，不产生新 run。"""
    system = make_system(model_script=[{"content": "cron done"}])
    system.boot()
    clock = {"now": 100.0}
    sched, _ = await _setup(system, lambda: clock["now"])
    sched.register("daily", "每日备份检查", interval_s=10.0)
    clock["now"] = 101.0
    await sched.tick()           # 窗口 0：新建 run
    clock["now"] = 102.0
    await sched.tick()           # 仍在窗口 0：重放（不新建）
    runs_after_one_window = len(system.store.list_runs("default"))
    clock["now"] = 115.0
    await sched.tick()           # 窗口 1：新 run
    runs_after_two_windows = len(system.store.list_runs("default"))
    assert runs_after_one_window == 1
    assert runs_after_two_windows == 2


async def test_cron_max_runs_guard_and_unregister(
    make_system: Callable[..., SystemHandle],
) -> None:
    """max_runs 护栏到量即停；unregister 后不再触发。"""
    system = make_system(model_script=[{"content": "cron done"}])
    system.boot()
    clock = {"now": 50.0}
    sched, _ = await _setup(system, lambda: clock["now"])
    sched.register("bounded", "限定次数任务", interval_s=0.5, max_runs=1)
    clock["now"] = 51.0
    await sched.tick()
    clock["now"] = 52.0
    await sched.tick()   # 超过 max_runs：不触发
    clock["now"] = 53.0
    await sched.tick()
    assert sched._fire_counts["bounded"] == 1

    sched.register("gone", "将被注销", interval_s=0.5)
    assert sched.unregister("gone") is True
    clock["now"] = 54.0
    await sched.tick()
    assert sched._fire_counts.get("gone", 0) == 0
    jobs = sched.list_jobs()
    assert all(j["job_id"] != "gone" for j in jobs)
