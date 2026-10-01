"""Thin activity boundary: validated graph routing and governed node execution."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from typing import Any, Protocol

from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.states import RUN_TERMINAL, RunState, StepState
from agentos.kernel.store import RunRecord
from agentos.kernel.workflow_graph import FlowGraph
from agentos.runtime.agent_driver import AgentRuntimeDriver
from agentos.runtime.executor import RuntimeDeps, _refresh_actor, execute_tool_step


class WorkflowEngine(Protocol):
    async def start(self, workflow_id: str, binding: dict[str, Any]) -> None: ...
    async def signal(self, workflow_id: str) -> None: ...
    async def cancel(self, workflow_id: str) -> None: ...


CustomNode = Callable[[dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]]


class WorkflowDriver:
    def __init__(self, deps: RuntimeDeps, agent: AgentRuntimeDriver | None) -> None:
        self.deps, self.agent = deps, agent
        self.engine: WorkflowEngine | None = None
        self.custom: dict[str, CustomNode] = {}
        self.templates: dict[str, Any] = {}

    @staticmethod
    def handle(run: RunRecord) -> str:
        return 'agentos-' + hashlib.sha256((run.tenant_id + '\0' + run.id).encode()).hexdigest()

    @staticmethod
    def graph(payload: dict[str, Any]) -> FlowGraph:
        if payload.get('workflow_graph'):
            return FlowGraph.model_validate(payload['workflow_graph'])
        return FlowGraph(id='builtin-template', nodes=[{'id': 'business', 'kind': 'template',
            'config': {'template': payload.get('template')}}])

    async def dispatch(self, run: RunRecord) -> RunRecord:
        if self.engine is None:
            raise AgentOSError(ErrorCode.UNAVAILABLE, 'custom flows require a configured Temporal engine')
        await _refresh_actor(self.deps, run)
        payload = json.loads(run.envelope_json)['payload']
        graph = self.graph(payload)
        cp = json.loads(run.checkpoint_json or '{}')
        handle = self.handle(run)
        if cp.get('workflow_engine'):
            await self.engine.signal(handle)
        else:
            # Stable workflow ID is the external idempotency key if the receipt is lost.
            await self.engine.start(handle, {'tenant_id': run.tenant_id, 'run_id': run.id,
                                            'schedule': graph.schedule()})
            cp['workflow_engine'] = {'engine': 'temporal', 'handle': handle}
            self.deps.store.save_checkpoint(run.tenant_id, run.id, json.dumps(cp))
        self.deps.store.transition_run(run.tenant_id, run.id, {RunState.RUNNING}, RunState.AWAITING_RESOURCES)
        return self.deps.store.get_run(run.tenant_id, run.id) or run

    async def node(self, binding: dict[str, Any]) -> dict[str, Any]:
        trace = (self.deps.telemetry.span('workflow.node', run_id=binding['run_id'], node_id=binding['node_id'])
                 if self.deps.telemetry else nullcontext())
        with trace:
            return await self._node(binding)

    async def _node(self, binding: dict[str, Any]) -> dict[str, Any]:
        store = self.deps.store
        tenant, run_id, node_id = binding['tenant_id'], binding['run_id'], binding['node_id']
        run = store.get_run(tenant, run_id)
        if run is None or run.state in RUN_TERMINAL or run.state == RunState.CANCEL_REQUESTED:
            return {'status': 'failed'}
        owner = 'temporal-' + uuid.uuid4().hex
        lease = store.claim_run_lease(tenant, run_id, owner, 120)
        if lease is None:
            return {'status': 'resource_busy'}

        task = asyncio.current_task()

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(30)
                if not store.renew_run_lease(tenant, run_id, owner, lease.fence, 120):
                    if task:
                        task.cancel()
                    return
        renewing = asyncio.create_task(heartbeat())
        try:
            actor = await _refresh_actor(self.deps, run)
            if any(step.state == StepState.UNCERTAIN for step in store.list_steps(tenant, run_id)):
                return {'status': 'blocked'}
            store.transition_run(tenant, run_id, {RunState.AWAITING_RESOURCES, RunState.AWAITING_APPROVAL,
                                                RunState.PAUSED, RunState.CREATED}, RunState.RUNNING)
            run = store.get_run(tenant, run_id) or run
            payload = json.loads(run.envelope_json)['payload']
            graph = self.graph(payload)
            node = next(n for n in graph.nodes if n.id == node_id)
            cp = json.loads(run.checkpoint_json or '{}')
            results = cp.setdefault('workflow_nodes', {})
            if node_id in results and results[node_id].get('state') == 'completed':
                return {'status': 'completed', 'branch': results[node_id].get('branch')}
            context = {'inputs': payload.get('inputs', {}),
                       'nodes': {k: v.get('output') for k, v in results.items() if v.get('state') == 'completed'}}

            def resolve(value: Any) -> Any:
                if isinstance(value, dict) and set(value) == {'$ref'}:
                    current: Any = context
                    path = str(value['$ref']).split('.')
                    if path[0] == 'nodes' and len(path) > 1 and path[1] not in node.depends_on:
                        raise AgentOSError(ErrorCode.DENIED, 'node can only read declared dependency outputs')
                    for key in path:
                        if not isinstance(current, dict) or key not in current:
                            raise AgentOSError(ErrorCode.INVALID, 'node input reference not found')
                        current = current[key]
                    return current
                if isinstance(value, dict):
                    return {k: resolve(v) for k, v in value.items()}
                if isinstance(value, list):
                    return [resolve(v) for v in value]
                return value
            output: Any
            branch: bool | None = None
            if node.kind == 'template':
                template = node.config['template']
                runner = self.templates.get(template)
                if runner is None or payload.get('template') != template:
                    raise AgentOSError(ErrorCode.DENIED, 'template inputs require normalized task ingress')
                result = await runner.execute(run, lease, finish_run=False)
                cp = json.loads(result.checkpoint_json or '{}')
                if result.state in RUN_TERMINAL:
                    return {'status': 'failed'}
                if not cp.get('delivery'):
                    return {'status': 'blocked'}
                output = cp['delivery']
            elif node.kind == 'agent':
                if self.agent is None:
                    raise AgentOSError(ErrorCode.UNAVAILABLE, 'agent node requires Codex runtime')
                # Roles/ToolPort are a server-side intersection, never an authority from node text.
                available = await self.deps.tools.list_tools(actor)
                requested = set(node.config.get('tools', []))
                specs = [s for s in available if s.name in requested]
                if requested - {s.name for s in specs}:
                    raise AgentOSError(ErrorCode.DENIED, 'node requested an unavailable tool')
                prompt = str(node.config.get('prompt', 'Complete this node.')) + '\nAuthorized inputs: ' + json.dumps(
                    resolve(node.config.get('inputs', {'$ref': 'inputs'})), ensure_ascii=False)
                result = await self.agent.execute(run, lease, node_id='flow-' + node_id,
                                                 goal=prompt, tools=specs, finish_run=False)
                cp = json.loads(result.checkpoint_json or '{}')
                state = cp.get('runtime_nodes', {}).get('flow-' + node_id, {})
                if state.get('state') != 'completed':
                    return {'status': 'blocked'}
                output = state['answer']
            elif node.kind == 'tool':
                outcome = await execute_tool_step(self.deps, run, 'flow-' + node_id,
                    node.config['tool'], resolve(node.config.get('arguments', {})), lease)
                if outcome.state in {StepState.UNCERTAIN, StepState.AWAITING_APPROVAL, StepState.PENDING_EXTERNAL}:
                    return {'status': 'blocked'}
                if outcome.state != StepState.COMPLETED:
                    return {'status': 'failed'}
                output = outcome.result.value if outcome.result else {}
            elif node.kind == 'condition':
                actual = resolve({'$ref': node.config['path']})
                expected, op = node.config.get('value'), node.config['operator']
                if op == 'eq':
                    branch = actual == expected
                elif op == 'ne':
                    branch = actual != expected
                elif op == 'exists':
                    branch = actual is not None
                else:
                    branch = expected in actual if isinstance(actual, (str, list, dict)) else False
                output = branch
            elif node.kind == 'custom':
                implementation = self.custom.get(node.config['adapter'])
                if implementation is None:
                    raise AgentOSError(ErrorCode.UNSUPPORTED, 'custom adapter is not registered server-side')
                output = await implementation(resolve(node.config.get('inputs', {})), context)
            else:
                output = {'wait_completed': True}  # Durable timer already owned by Temporal.
            results = cp.setdefault('workflow_nodes', {})
            results[node_id] = {'state': 'completed', 'output': output, 'branch': branch}
            store.save_checkpoint(tenant, run_id, json.dumps(cp, ensure_ascii=False))
            store.event_append(tenant, run_id, 'workflow.node_completed', {'node_id': node_id, 'kind': node.kind})
            store.transition_run(tenant, run_id, {RunState.RUNNING}, RunState.AWAITING_RESOURCES)
            return {'status': 'completed', 'branch': branch}
        except AgentOSError as exc:
            store.event_append(tenant, run_id, 'workflow.node_failed', {'node_id': node_id, 'code': exc.code.value})
            return {'status': 'failed'}
        finally:
            renewing.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renewing
            store.release_run_lease(tenant, run_id, owner)

    async def finish(self, binding: dict[str, Any]) -> None:
        tenant, run_id = binding['tenant_id'], binding['run_id']
        run = self.deps.store.get_run(tenant, run_id)
        if run is None or run.state in RUN_TERMINAL:
            return
        if run.state == RunState.CANCEL_REQUESTED:
            state = RunState.CANCELLED
        else:
            self.deps.store.transition_run(tenant, run_id,
                {RunState.AWAITING_RESOURCES, RunState.PAUSED, RunState.AWAITING_APPROVAL}, RunState.RUNNING)
            state = RunState(binding['state'])
        if state == RunState.SUCCEEDED:
            cp = json.loads(run.checkpoint_json or '{}')
            outputs = [n.get('output') for n in cp.get('workflow_nodes', {}).values()]
            answer = next((v for v in reversed(outputs) if isinstance(v, str)), '工作流已完成。')
            cp.setdefault('messages', []).append({'role': 'assistant', 'content': answer})
            self.deps.store.save_checkpoint(tenant, run_id, json.dumps(cp, ensure_ascii=False))
        self.deps.store.transition_run(tenant, run_id, {RunState.RUNNING, RunState.CANCEL_REQUESTED}, state)
        self.deps.store.event_append(tenant, run_id, 'run.completed', {'state': state.value, 'engine': 'temporal'})
