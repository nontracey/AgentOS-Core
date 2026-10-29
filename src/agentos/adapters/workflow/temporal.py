"""Temporal owns flow scheduling, waits and activity retries; AgentOS owns effects."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.runtime import LogForwardingConfig, LoggingConfig, Runtime, TelemetryConfig, TelemetryFilter
from temporalio.worker import Worker

_CORE_LOGGER = "agentos.temporal"


class RepeatLimiter(logging.Filter):
    """Let a recurring message through once per window and report how many copies were held back.

    When the Temporal server is unreachable, pollers and failed workflow tasks retry forever and
    each attempt logs; without this a long outage fills the disk with identical lines.
    """

    def __init__(self, window_s: float = 300.0, max_keys: int = 256) -> None:
        super().__init__()
        self.window_s = window_s
        self.max_keys = max_keys
        self._seen: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(record: logging.LogRecord) -> str:
        message = record.getMessage()
        return record.name + "|" + re.sub(r"[0-9a-fA-F-]{8,}|\d+", "#", message)[:240]

    def filter(self, record: logging.LogRecord) -> bool:
        key = self._key(record)
        now = time.monotonic()
        with self._lock:
            entry = self._seen.get(key)
            if entry is not None and now - entry[0] < self.window_s:
                self._seen[key] = (entry[0], entry[1] + 1)
                return False
            held = entry[1] if entry is not None else 0
            if entry is None and len(self._seen) >= self.max_keys:
                oldest = min(self._seen, key=lambda k: self._seen[k][0])
                del self._seen[oldest]
            self._seen[key] = (now, 0)
        if held:
            record.msg = f"{record.getMessage()} [{held} similar messages suppressed in the last {self.window_s:.0f}s]"
            record.args = None
        return True


_LIMITER = RepeatLimiter()
_RUNTIME: Runtime | None = None


def quiet_runtime() -> Runtime:
    """One Temporal runtime whose core logs go through Python logging with repeat limiting."""
    global _RUNTIME
    if _RUNTIME is None:
        core = logging.getLogger(_CORE_LOGGER)
        if _LIMITER not in core.filters:
            core.addFilter(_LIMITER)
        # Worker heartbeats only feed server-side worker listings; during an outage they add a
        # second stream of retry warnings, so they stay off for the personal single-worker setup.
        _RUNTIME = Runtime(telemetry=TelemetryConfig(logging=LoggingConfig(
            filter=TelemetryFilter(core_level="WARN", other_level="ERROR"),
            forwarding=LogForwardingConfig(logger=core, append_target_to_name=False))),
            worker_heartbeat_interval=None)
    names = [n for n in list(logging.Logger.manager.loggerDict) if n.split(".")[0] == "temporalio"]
    for name in names:
        sdk_logger = logging.getLogger(name)
        if _LIMITER not in sdk_logger.filters:
            sdk_logger.addFilter(_LIMITER)
    return _RUNTIME


@workflow.defn
class IntegrationFlow:
    def __init__(self) -> None:
        self.revision = 0

    @workflow.signal
    def wake(self) -> None:
        self.revision += 1

    @workflow.run
    async def run(self, binding: dict[str, Any]) -> str:
        from temporalio.exceptions import ActivityError

        try:
            return await self._run(binding)
        except ActivityError:
            await workflow.execute_activity('agentos_flow_finish',
                {'tenant_id': binding['tenant_id'], 'run_id': binding['run_id'], 'state': 'failed'},
                start_to_close_timeout=timedelta(seconds=30))
            return 'failed'

    async def _run(self, binding: dict[str, Any]) -> str:
        pending = list(binding['schedule'])
        done: dict[str, dict[str, Any]] = {}

        async def run_node(node: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            if any(done[k].get('status') == 'skipped' for k in node['depends_on']):
                return node['id'], {'status': 'skipped'}
            if any(done[k].get('status') == 'skipped' or done[k].get('branch') != v
                   for k, v in node['when'].items()):
                return node['id'], {'status': 'skipped'}
            if node['kind'] == 'wait':
                await workflow.sleep(node['seconds'])
            while True:
                revision = self.revision
                result = await workflow.execute_activity('agentos_flow_node',
                    {'tenant_id': binding['tenant_id'], 'run_id': binding['run_id'], 'node_id': node['id']},
                    start_to_close_timeout=timedelta(seconds=node.get('activity_timeout_s', 600)),
                    retry_policy=RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=2)))
                if result['status'] == 'resource_busy':
                    await workflow.sleep(.5)
                    continue
                if result['status'] == 'blocked':
                    def awakened(revision: int = revision) -> bool:
                        return self.revision > revision
                    await workflow.wait_condition(awakened)
                    continue
                return node['id'], result

        while pending:
            ready = [n for n in pending if set(n['depends_on']) <= set(done)]
            pending = [n for n in pending if n not in ready]
            # Independent timers/branches are scheduled together. Run leases serialize
            # shared effect/checkpoint writers on the personal execution node.
            results = await asyncio.gather(*(run_node(n) for n in ready))
            done.update(results)
            if any(result['status'] not in ('completed', 'skipped') for _, result in results):
                await workflow.execute_activity('agentos_flow_finish',
                    {'tenant_id': binding['tenant_id'], 'run_id': binding['run_id'], 'state': 'failed'},
                    start_to_close_timeout=timedelta(seconds=30))
                return 'failed'
        await workflow.execute_activity('agentos_flow_finish',
            {'tenant_id': binding['tenant_id'], 'run_id': binding['run_id'], 'state': 'succeeded'},
            start_to_close_timeout=timedelta(seconds=30))
        return 'succeeded'


class TemporalWorkflowAdapter:
    def __init__(self, address: str, namespace: str, queue: str,
                 node: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
                 finish: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self.address, self.namespace, self.queue = address, namespace, queue
        self.node, self.finish = node, finish
        self.client: Client | None = None
        self.worker: Worker | None = None

    async def open(self) -> None:
        self.client = await Client.connect(self.address, namespace=self.namespace, runtime=quiet_runtime())

        @activity.defn(name='agentos_flow_node')
        async def dispatch(binding: dict[str, Any]) -> dict[str, Any]:
            return await self.node(binding)

        @activity.defn(name='agentos_flow_finish')
        async def complete(binding: dict[str, Any]) -> None:
            await self.finish(binding)

        self.worker = Worker(self.client, task_queue=self.queue, workflows=[IntegrationFlow],
                             activities=[dispatch, complete], max_concurrent_activities=1,
                             sticky_queue_schedule_to_start_timeout=timedelta(seconds=5))
        await self.worker.__aenter__()

    async def close(self) -> None:
        if self.worker:
            await self.worker.__aexit__(None, None, None)

    async def start(self, workflow_id: str, binding: dict[str, Any]) -> None:
        if self.client is None:
            raise RuntimeError('Temporal adapter not started')
        from temporalio.exceptions import WorkflowAlreadyStartedError
        with contextlib.suppress(WorkflowAlreadyStartedError):
            await self.client.start_workflow(IntegrationFlow.run, binding, id=workflow_id, task_queue=self.queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)

    async def signal(self, workflow_id: str) -> None:
        assert self.client
        await self.client.get_workflow_handle(workflow_id).signal(IntegrationFlow.wake)

    async def cancel(self, workflow_id: str) -> None:
        assert self.client
        await self.client.get_workflow_handle(workflow_id).cancel()
