"""Structured provider failures and governed recovery of the original Codex thread."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from agentos.adapters.runtime.codex import _turn_failure
from agentos.apps.composition import build_system
from agentos.apps.config import Settings
from agentos.apps.gateway import create_app
from agentos.kernel.states import RunState, StepState
from agentos.runtime.agent_driver import AgentRuntimeDriver
from tests.conftest import add_subject


@pytest.mark.parametrize('info', [
    'rateLimitExceeded', 'usageLimitExceeded',
    {'httpConnectionFailed': {'httpStatusCode': 429}},
    {'responseStreamConnectionFailed': {'httpStatusCode': 429}},
    {'responseTooManyFailedAttempts': {'httpStatusCode': 429}},
])
def test_provider_rate_limit_uses_structured_error_and_never_raw_text(info):
    error = _turn_failure({'error': {'codexErrorInfo': info,
                                    'message': 'synthetic-private-body',
                                    'additionalDetails': 'synthetic-private-header'}}, 'compaction')
    assert error.detail['reason'] == 'provider_rate_limited'
    assert error.detail['phase'] == 'compaction'
    assert 'synthetic-private' not in json.dumps(error.to_payload())


@pytest.mark.parametrize('info', [None, 'badRequest',
    {'httpConnectionFailed': {'httpStatusCode': '429'}},
    {'httpConnectionFailed': {'httpStatusCode': 400}},
    {'unexpected': {'httpStatusCode': 429}},
])
def test_other_failures_are_not_misclassified_as_rate_limits(info):
    error = _turn_failure({'error': {'codexErrorInfo': info, 'message': 'private'}}, 'inference')
    assert 'reason' not in error.detail
    assert error.message == 'Codex inference failed'


def test_compaction_rate_pause_resumes_original_thread_effect_and_checkpoint(tmp_path):
    class Engine:
        limited = 2

        async def stream(self, request, handler):
            yield {'type': 'binding', 'thread_id': 'synthetic-thread'}
            if not request.thread_id:
                assert (await handler('read-call', 'math.add', {'a': 2, 'b': 3}))['success']
                result = await handler('original-call', 'fs.write_text', {'path': 'proof.txt', 'content': 'once'})
                assert result['blocked']
                yield {'type': 'blocked'}
                return
            assert 'persisted receipts supersede' in request.prompt
            assert 'fs.write_text' in request.prompt
            assert '"written": true' in request.prompt
            assert 'math.add' not in request.prompt
            assert len(request.prompt) < 4096
            if self.limited == 2:
                assert request.compact
                self.limited -= 1
                raise _turn_failure({'error': {'codexErrorInfo': 'rateLimitExceeded'}}, 'compaction')
            if request.compact:
                yield {'type': 'compacted'}
            if self.limited == 1:
                self.limited -= 1
                raise _turn_failure({'error': {'codexErrorInfo': 'rateLimitExceeded'}}, 'inference')
            yield {'type': 'assistant_message', 'content': 'finished'}
            yield {'type': 'completed'}

    async def run():
        settings = Settings(data_dir=tmp_path)
        handle = build_system(settings)
        token = handle.boot()
        engine = Engine()
        handle.loop.use_engine(AgentRuntimeDriver(handle.deps, engine, settings.sandbox_dir))
        receipt = await handle.ingress.accept({'payload': {'goal': 'synthetic proof'}},
                                             channel='http', credentials={'token': token})
        rid = receipt.run_id
        assert await handle.worker.drive_until_settled(rid) == 'awaiting_approval'
        cp = json.loads(handle.store.get_run('default', rid).checkpoint_json)
        cp['runtime_nodes']['main']['compact_requested'] = True
        handle.store.save_checkpoint('default', rid, json.dumps(cp))
        actor = await handle.deps.identity.authenticate({'token': token})
        handle.deps.approvals.decide(actor, handle.deps.approvals.list_pending('default')[0].id, 'approve')
        assert await handle.worker.drive_until_settled(rid) == 'paused'
        original = next(step for step in handle.store.list_steps('default', rid) if step.tool_name == 'fs.write_text')
        assert original.state == StepState.COMPLETED
        assert (settings.sandbox_dir / 'proof.txt').read_text() == 'once'
        assert json.loads(handle.store.get_run('default', rid).error_json)['code'] == 'provider_rate_limited'
        assert json.loads(handle.store.get_run('default', rid).checkpoint_json)['runtime_nodes']['main'][
            'compact_requested']
        cp = json.loads(handle.store.get_run('default', rid).checkpoint_json)
        confirmed = cp['runtime_nodes']['main']['confirmed_calls']
        assert len(confirmed) == 1
        confirmed['original-call']['receipt']['result']['value']['text'] = 'synthetic-large-body' * 10000
        handle.store.save_checkpoint('default', rid, json.dumps(cp))
        other = add_subject(handle, 'other-member', ['read', 'write'])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(system=handle)),
                                     base_url='http://localhost') as client:
            endpoint = f'/v1/tasks/{rid}/resume'
            assert (await client.post(endpoint)).status_code == 401
            assert (await client.post(endpoint, headers={'Authorization': 'Bearer ' + other})).status_code == 404
            headers = {'Authorization': 'Bearer ' + token}
            assert (await client.post(endpoint, headers=headers)).status_code == 202
            assert (await client.post(endpoint, headers=headers)).status_code == 409
            assert handle.store.get_run('default', rid).error_json is None
            assert await handle.worker.drive_until_settled(rid) == 'paused'
        # A second failure after compaction must keep the success receipt across
        # a process rebuild, even though no pending effect/compaction remains.
        handle = build_system(settings)
        handle.loop.use_engine(AgentRuntimeDriver(handle.deps, engine, settings.sandbox_dir))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(system=handle)),
                                     base_url='http://localhost') as client:
            assert (await client.post(endpoint, headers=headers)).status_code == 202
            assert await handle.worker.drive_until_settled(rid) == 'succeeded'
            assert (await client.post(endpoint, headers=headers)).status_code == 409
        steps = handle.store.list_steps('default', rid)
        writes = [step for step in steps if step.tool_name == 'fs.write_text']
        assert len(steps) == 2 and len(writes) == 1
        assert writes[0].id == original.id and writes[0].attempt == original.attempt
        cp = json.loads(handle.store.get_run('default', rid).checkpoint_json)
        assert not cp['runtime_nodes']['main'].get('compact_requested')
        assert cp['runtime_nodes']['main']['thread_id'] == 'synthetic-thread'
        events = handle.store.event_list('default', rid, limit=1000)
        assert sum(e['kind'] == 'runtime.resume_requested' for e in events) == 2
        assert any(e['kind'] == 'runtime.compacted' for e in events)

    asyncio.run(run())


@pytest.mark.parametrize('state', [StepState.UNCERTAIN, StepState.DISPATCHED,
                                   StepState.PENDING_EXTERNAL, StepState.AWAITING_APPROVAL])
def test_runtime_resume_cannot_bypass_unresolved_effect_or_approval(tmp_path, state):
    async def run():
        handle = build_system(Settings(data_dir=tmp_path))
        token = handle.boot()
        receipt = await handle.ingress.accept({'payload': {'goal': 'synthetic pending'}},
                                             channel='http', credentials={'token': token})
        rid = receipt.run_id
        handle.store.transition_run('default', rid, {RunState.CREATED}, RunState.PAUSED,
                                    error_json=json.dumps({'code': 'provider_rate_limited'}))
        handle.store.save_checkpoint('default', rid, json.dumps({'runtime_nodes': {'main': {'engine': 'codex'}}}))
        step = handle.store.prepare_once('default', rid, 'unknown-step', 'fs.write_text', 'hash', 1)
        handle.store.transition_step('default', rid, step.id, {StepState.PREPARED}, state)
        run = handle.store.get_run('default', rid)
        assert not handle.store.resume_runtime('default', rid, run.version, 'owner')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(system=handle)),
                                     base_url='http://localhost') as client:
            response = await client.post(f'/v1/tasks/{rid}/resume', headers={'Authorization': 'Bearer ' + token})
            assert response.status_code == 409
        assert handle.store.get_run('default', rid).state == RunState.PAUSED
        assert not any(e['kind'] == 'runtime.resume_requested'
                       for e in handle.store.event_list('default', rid, limit=1000))

    asyncio.run(run())


def test_read_only_owner_cannot_resume_a_paused_runtime(tmp_path):
    async def run():
        settings = Settings(data_dir=tmp_path)
        handle = build_system(settings)
        handle.boot()
        member = add_subject(handle, 'limited-member', ['read', 'write'])
        receipt = await handle.ingress.accept({'payload': {'goal': 'synthetic permission change'}},
                                             channel='http', credentials={'token': member})
        rid = receipt.run_id
        handle.store.transition_run('default', rid, {RunState.CREATED}, RunState.PAUSED,
                                    error_json=json.dumps({'code': 'provider_rate_limited'}))
        handle.store.save_checkpoint('default', rid, json.dumps({'runtime_nodes': {'main': {'engine': 'codex'}}}))
        identity = json.loads(settings.identity_file.read_text())
        identity['subjects']['limited-member']['scopes'] = ['read']
        settings.identity_file.write_text(json.dumps(identity))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(system=handle)),
                                     base_url='http://localhost') as client:
            response = await client.post(f'/v1/tasks/{rid}/resume', headers={'Authorization': 'Bearer ' + member})
            assert response.status_code == 403
        assert handle.store.get_run('default', rid).state == RunState.PAUSED
        assert not any(e['kind'] == 'runtime.resume_requested'
                       for e in handle.store.event_list('default', rid, limit=1000))

    asyncio.run(run())
