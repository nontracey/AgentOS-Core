"""Temporal owns flow scheduling, waits and activity retries; AgentOS owns effects."""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.worker import Worker


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
        self.client = await Client.connect(self.address, namespace=self.namespace)

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
