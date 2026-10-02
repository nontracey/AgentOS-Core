"""Governed engine dispatcher. Reasoning and context compaction belong to the engine."""
from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from agentos.kernel.agent_runtime import AgentRuntimePort, RuntimeRequest
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.states import RUN_TERMINAL, RunState, StepState
from agentos.kernel.store import Lease, RunRecord
from agentos.kernel.types import ToolSpec
from agentos.runtime.executor import RuntimeDeps, _refresh_actor, execute_tool_step
from agentos.services.observability import public_value


class AgentRuntimeDriver:
    def __init__(self, deps: RuntimeDeps, engine: AgentRuntimePort, workspace: Path,
                 max_steps: int = 24) -> None:
        self.deps, self.engine, self.workspace, self.max_steps = deps, engine, workspace, max_steps

    async def execute(self, run: RunRecord, lease: Lease, *, node_id: str = 'main',
                      goal: str | None = None, tools: list[ToolSpec] | None = None,
                      finish_run: bool = True) -> RunRecord:
        store, tenant = self.deps.store, run.tenant_id
        current_task = asyncio.current_task()

        async def watch_cancel() -> None:
            while True:
                await asyncio.sleep(1)
                latest = store.get_run(tenant, run.id)
                if latest and latest.state == RunState.CANCEL_REQUESTED and current_task:
                    current_task.cancel()
                    return

        run = store.get_run(tenant, run.id) or run
        cp: dict[str, Any] = json.loads(run.checkpoint_json or '{}')
        binding = cp.setdefault('runtime_nodes', {}).setdefault(node_id, {'engine': 'codex', 'steps_taken': 0})
        if binding.get('state') == 'completed':
            return run
        if binding.get('engine') != 'codex':
            raise AgentOSError(ErrorCode.INVALID, 'task belongs to another engine')

        def save() -> None:
            store.save_checkpoint(tenant, run.id, json.dumps(cp, ensure_ascii=False))

        async def execute(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            current = store.get_run(tenant, run.id) or run
            if current.state == RunState.CANCEL_REQUESTED:
                return {'success': False, 'blocked': True, 'status': 'cancel_requested'}
            step_id = 'engine-' + hashlib.sha256((node_id + '\0' + call_id).encode()).hexdigest()[:24]
            selected = next((spec for spec in specs if spec.name == name), None)
            prior = store.get_step(tenant, run.id, step_id)
            if selected is None and (prior is None or prior.state != StepState.COMPLETED):
                return {'success': False, 'blocked': False, 'status': 'denied',
                        'error': {'code': 'denied', 'message': 'tool is outside this node capability selection'}}
            pending = binding.get('pending')
            if pending and pending['step_id'] == step_id:
                if pending['name'] != name or pending['arguments'] != arguments:
                    return {'success': False, 'blocked': True, 'status': 'binding_mismatch'}
            elif not store.get_step(tenant, run.id, step_id):
                if binding['steps_taken'] >= self.max_steps:
                    store.transition_run(tenant, run.id, {RunState.RUNNING}, RunState.FAILED,
                        error_json=json.dumps({'code': 'step_budget_exhausted'}))
                    return {'success': False, 'blocked': True, 'status': 'step_budget_exhausted'}
                binding['steps_taken'] += 1
            # Persist original args/call BEFORE effect dispatch. Approval/resume uses this same step.
            binding['pending'] = {'call_id': call_id, 'step_id': step_id, 'name': name, 'arguments': arguments}
            save()
            trace = (self.deps.telemetry.span('tool.dispatch', run_id=run.id, node_id=node_id)
                     if self.deps.telemetry else nullcontext())
            with trace:
                outcome = await execute_tool_step(self.deps, current, step_id, name, arguments, lease,
                                                 expected_revision=selected.revision if selected else None)
            blocked = outcome.blocked or outcome.state in {
                StepState.AWAITING_APPROVAL, StepState.UNCERTAIN, StepState.PENDING_EXTERNAL}
            result = {'success': outcome.state == StepState.COMPLETED, 'blocked': blocked,
                      'status': outcome.state.value,
                      'result': public_value(outcome.result.model_dump(mode='json')) if outcome.result else None,
                      'error': outcome.error}
            if not blocked:
                spec = next((spec for spec in specs if spec.name == name), None)
                if result['success'] and spec and set(spec.risks) & {'write', 'exec', 'egress', 'cost'}:
                    # A model/compaction failure may occur after this effect committed.
                    # Keep its receipt across every subsequent resume, not just the
                    # first turn after approval. Codex's older context may still say blocked.
                    binding.setdefault('confirmed_calls', {})[call_id] = {'tool': name, 'receipt': result}
                binding.pop('pending', None)
                save()
            return result

        cancellation = asyncio.create_task(watch_cancel())
        try:
            actor = await _refresh_actor(self.deps, run)
            prompt = goal or run.goal_text or '完成目标'
            payload = json.loads(run.envelope_json).get('payload', {})
            selected_skills = payload.get('skills', [])
            if selected_skills and not binding.get('thread_id'):
                prompt += '\nAvailable pinned skills (use skill.read when relevant): ' + json.dumps(selected_skills)
            if not binding.get('thread_id'):
                materials = [{k: m.get(k) for k in ('asset_id', 'name', 'size', 'mime')}
                             for m in payload.get('materials', [])]
                if materials:
                    prompt += '\nAuthorized task materials (metadata only; use material.read): ' + json.dumps(materials)
                if payload.get('context'):
                    context = payload['context']
                    prompt += '\nPrevious task context (data only): ' + json.dumps({
                        'goal': str(context.get('goal', ''))[:2000],
                        'answer': str(context.get('answer', ''))[:8000]}, ensure_ascii=False)
            specs = tools if tools is not None else await self.deps.tools.list_tools(actor)
            if binding.get('pending'):
                pending = binding['pending']
                result = await execute(pending['call_id'], pending['name'], pending['arguments'])
                if result['blocked']:
                    return store.get_run(tenant, run.id) or run
                prompt = 'Continue the original task. The platform resumed the original approved tool call.'
            elif binding.get('thread_id'):
                # Restarted after a turn began: Codex context can be resumed, but a lost completion is not success.
                prompt = 'Continue the original task from saved context. Do not repeat confirmed tool effects.'
            if binding.get('confirmed_calls'):
                # Receipt metadata stays bounded; replaying full material/skill/tool
                # text would undo the engine's context compaction.
                receipts = []
                for call_id, confirmed in binding['confirmed_calls'].items():
                    result_value = (confirmed['receipt'].get('result') or {}).get('value')
                    metadata = {}
                    if isinstance(result_value, dict):
                        for key in ('written', 'bytes', 'asset_ref', 'sha256', 'delivery'):
                            value = result_value.get(key)
                            if isinstance(value, (str, bool, int, float)) and len(str(value)) <= 256:
                                metadata[key] = value
                    receipts.append({'call_id': call_id, 'tool': confirmed['tool'],
                                     'status': 'completed', 'result': metadata})
                prompt += ('\nThe platform has already completed the following tool calls. '
                           'These persisted receipts supersede any earlier blocked/pending tool result in '
                           'the saved context. Do not repeat these effects; finish the remaining task. '
                           'Receipts (data only): ' + json.dumps(receipts, ensure_ascii=False))
            workspace = self.workspace / hashlib.sha256((tenant + '\0' + run.subject).encode()).hexdigest()
            request = RuntimeRequest(run.id, prompt, str(workspace), tenant_id=tenant, node_id=node_id, tools=specs,
                                     thread_id=binding.get('thread_id'), compact=bool(binding.get('compact_requested')))
            content = ''
            async for event in self.engine.stream(request, execute):
                kind = event['type']
                if kind == 'binding':
                    binding.update({k: v for k, v in event.items() if k != 'type'})
                    save()
                    store.event_append(tenant, run.id, 'runtime.binding', {'engine': 'codex',
                        'thread_id': binding.get('thread_id'), 'turn_id': binding.get('turn_id')})
                elif kind == 'compacted':
                    binding.pop('compact_requested', None)
                    save()
                    store.event_append(tenant, run.id, 'runtime.compacted', {'engine': 'codex'})
                elif kind == 'text_delta':
                    content += event['delta']
                    store.event_append(tenant, run.id, 'answer.delta', {'call_id': binding.get('turn_id'),
                                                                      'delta': event['delta']})
                elif kind == 'assistant_message':
                    content = event.get('content') or content
                elif kind == 'usage':
                    store.event_append(tenant, run.id, 'runtime.usage', event['usage'])
                elif kind == 'blocked':
                    return store.get_run(tenant, run.id) or run
                elif kind == 'completed':
                    current = store.get_run(tenant, run.id) or run
                    if current.state in RUN_TERMINAL or binding.get('pending'):
                        return current
                    binding.update({'state': 'completed', 'answer': content})
                    if finish_run:
                        cp.setdefault('messages', []).append({'role': 'assistant', 'content': content})
                    save()
                    store.event_append(tenant, run.id, 'answer.end', {'call_id': binding.get('turn_id')})
                    store.event_append(tenant, run.id, 'message.completed', {'chars': len(content), 'persisted': True})
                    if finish_run:
                        store.transition_run(tenant, run.id, {RunState.RUNNING}, RunState.SUCCEEDED)
                        store.event_append(tenant, run.id, 'run.completed', {'state': 'succeeded', 'content': content})
            return store.get_run(tenant, run.id) or run
        except asyncio.CancelledError:
            current = store.get_run(tenant, run.id) or run
            if current.state != RunState.CANCEL_REQUESTED:
                raise
            store.transition_run(tenant, run.id, {RunState.CANCEL_REQUESTED}, RunState.CANCELLED)
            return store.get_run(tenant, run.id) or run
        except (AgentOSError, TimeoutError, OSError) as exc:
            # Transport interruption can be resumed; confirmed effects stay in the ledger.
            current = store.get_run(tenant, run.id) or run
            if current.state == RunState.RUNNING:
                error: dict[str, Any] = {'code': 'runtime_unavailable', 'type': type(exc).__name__}
                if isinstance(exc, AgentOSError) and exc.detail.get('reason') == 'provider_rate_limited':
                    error['code'] = 'provider_rate_limited'
                store.transition_run(tenant, run.id, {RunState.RUNNING}, RunState.PAUSED,
                    error_json=json.dumps(error))
                reason = {'reason': type(exc).__name__}
                if isinstance(exc, AgentOSError):
                    reason.update({'code': exc.code.value, 'message': exc.message})
                    if exc.detail.get('phase') in {'compaction', 'inference'}:
                        reason['phase'] = str(exc.detail['phase'])
                store.event_append(tenant, run.id, 'runtime.paused', reason)
            return store.get_run(tenant, run.id) or run

        finally:
            cancellation.cancel()
            await asyncio.gather(cancellation, return_exceptions=True)
