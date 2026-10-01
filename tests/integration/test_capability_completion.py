"""Synthetic integration services; real persistence, authorization and dispatch contracts."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentos.apps.composition import build_system
from agentos.apps.config import Settings
from agentos.apps.gateway import create_app
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.states import RunState
from agentos.kernel.types import CallContext, utc_now
from tests.conftest import add_subject


def headers(token: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + token}


def test_drafts_isolate_subject_tenant_cas_and_restart(tmp_path: Path) -> None:
    h = build_system(Settings(data_dir=tmp_path))
    token = h.boot()
    other = add_subject(h, "other", ["read", "write"])
    reader = add_subject(h, "reader", ["read"])
    payload = {"name": "Synthetic draft", "schema": "{unfinished", "nodes": [],
               "edges": [], "position": {"x": 73, "y": 91}}
    with TestClient(create_app(system=h)) as client:
        path = '/v1/workflow-drafts/draft-one'
        assert client.post(path, headers=headers(token), json={"payload": payload,
            "expected_revision": 0}).json()['revision'] == 1
        assert client.get(path, headers=headers(other)).status_code == 422
        assert client.get('/v1/workflow-drafts', headers=headers(other)).json() == []
        assert client.post(path, headers=headers(reader), json={"payload": payload,
            "expected_revision": 0}).status_code == 403
        assert client.post(path, headers=headers(token), json={"payload": {},
            "expected_revision": 0}).status_code == 409
        assert client.post(path+'/delete', headers=headers(token), json={"expected_revision": 2}).status_code == 409
        assert client.get(path, headers=headers(token)).json()['payload'] == payload
        assert h.store.list_drafts('different-tenant', 'owner', 'workflow') == []
    rebuilt = build_system(Settings(data_dir=tmp_path))
    actor = asyncio.run(rebuilt.deps.identity.authenticate({'token': token}))
    assert rebuilt.drafts.get(actor, 'workflow', 'draft-one')['payload'] == payload
    assert rebuilt.drafts.save(actor, 'workflow', 'draft-one', {'name': 'Updated'}, 1)['revision'] == 2
    rebuilt.drafts.delete(actor, 'workflow', 'draft-one', 2)
    assert rebuilt.drafts.list(actor, 'workflow') == []
    with pytest.raises(AgentOSError):
        rebuilt.drafts.save(actor, 'workflow', 'draft-one', {'name': 'stale'}, 2)
    with pytest.raises(AgentOSError):
        rebuilt.drafts.save(actor, 'workflow', 'draft-one', {'name': 'reuse'}, 0)


def test_connection_proposal_offline_review_no_overwrite_and_member_boundary(tmp_path: Path, monkeypatch) -> None:
    h = build_system(Settings(data_dir=tmp_path))
    token = h.boot()
    member = add_subject(h, 'member', ['read', 'write'])
    config = {'server_id': 'assistant', 'url': 'https://service.example.invalid/mcp', 'kind': 'agent',
              'env': {'Authorization': 'env:SYNTHETIC_MCP_CREDENTIAL'}}
    calls = []

    async def discover(*args):
        calls.append(args)
        raise AgentOSError(ErrorCode.UNAVAILABLE, 'synthetic offline')

    monkeypatch.setattr(h.tool_hub._inner, 'discover', discover)
    with TestClient(create_app(system=h)) as client:
        p = client.post('/v1/mcp/proposals', headers=headers(member), json=config).json()
        assert p['requires_admin_review'] is True and not calls
        assert client.post('/v1/mcp/proposals', headers=headers(member), json=config).json()['id'] == p['id']
        assert h.store.get_mcp_server('default', 'assistant') is None
        path = '/v1/mcp/proposals/'+p['id']+'/apply'
        assert client.post(path, headers=headers(member), json={'reviewed_hash': p['hash']}).status_code == 401
        assert client.post(path, headers=headers(token), json={'reviewed_hash': 'changed'}).status_code == 409
        assert client.post(path, headers=headers(token), json={'reviewed_hash': p['hash']}).json()['enabled'] is False
        assert client.post(path, headers=headers(token), json={'reviewed_hash': p['hash']}).status_code == 200
        assert not calls
        row = h.store.get_mcp_server('default', 'assistant')
        assert row['kind'] == 'agent' and row['enabled'] == 0
        p2 = client.post('/v1/mcp/proposals', headers=headers(token), json=config).json()
        assert client.post('/v1/mcp/proposals/'+p2['id']+'/apply', headers=headers(token),
                           json={'reviewed_hash': p2['hash']}).status_code == 409
        assert client.post('/v1/mcp/servers/assistant/enabled', headers=headers(token),
                           json={'enabled': True}).status_code == 503
        assert h.store.get_mcp_server('default', 'assistant')['enabled'] == 0
        assert client.get('/v1/mcp/proposals', headers=headers(member)).status_code == 401
        assert client.get('/v1/mcp/proposals', headers=headers(token)).json()[0]['payload']['config']['kind'] == 'agent'


@pytest.mark.parametrize('config', [
    {'server_id': 'bad', 'url': 'https://user:secret@example.invalid/mcp'},
    {'server_id': 'bad', 'url': 'https://example.invalid/mcp?token=secret'},
    {'server_id': 'bad', 'url': 'https://example.invalid/mcp', 'env': {'Authorization': 'literal-credential'}},
    {'server_id': 'bad', 'command': 'unreviewed shell string'},
    {'server_id': 'node', 'url': 'https://example.invalid/mcp'},
])
def test_connection_credentials_and_ambiguous_commands_rejected(tmp_path: Path, config: dict) -> None:
    h = build_system(Settings(data_dir=tmp_path))
    actor = asyncio.run(h.deps.identity.authenticate({'token': h.boot()}))
    with pytest.raises(AgentOSError) as exc:
        h.connections.propose(actor, config)
    assert exc.value.code == ErrorCode.INVALID
    assert h.drafts.list(actor, 'mcp') == []


async def test_workflow_capability_launch_budget_replay_terminal_and_no_recursion(tmp_path: Path) -> None:
    h = build_system(Settings(data_dir=tmp_path, run_budget_microusd=100))
    token = h.boot()
    actor = await h.deps.identity.authenticate({'token': token})
    d = {'id': 'reusable', 'name': 'Synthetic workflow', 'tasks': [
        {'task_id': 't1', 'role_id': 'worker', 'depends_on': [], 'acceptance_checks': ['contains:done']}]}
    h.workflows.publish(actor, d)
    launch = 'workflow.reusable.v1'
    spec = await h.deps.tools.resolve(actor, launch)
    assert h.deps.policy.check(actor, spec, {'inputs': {}}).outcome == 'needs_approval'
    reader = actor.model_copy(update={'scopes': ['read']})
    assert launch not in {t.name for t in await h.deps.tools.list_tools(reader)}
    receipt = await h.ingress.accept({'idempotency_key': 'parent', 'payload': {'goal': 'synthetic'}},
                                   channel='http', credentials={'token': token})
    h.store.transition_run('default', receipt.run_id, {RunState.CREATED}, RunState.RUNNING)
    h.deps.budget.reserve('default', receipt.run_id, 'model-used', 10)
    h.deps.budget.settle('default', 'model-used', 10)
    ctx = CallContext(run_id=receipt.run_id, step_id='launch', call_id='launch', actor=actor,
        idempotency_key='launch-stable', fence=1, deadline_at=utc_now()+timedelta(minutes=1))
    first = await h.deps.tools.call(launch, {'inputs': {}}, ctx)
    assert first.status == 'completed' and first.value['launch_only'] is True
    child = first.value['internal_run_id']
    assert h.store.get_run('default', child).budget_microusd == 45
    assert h.deps.budget.totals('default', receipt.run_id) == {'budget': 100, 'reserved': 45, 'settled': 10}
    repeated = await h.deps.tools.call(launch, {'inputs': {}}, ctx)
    assert repeated.value['internal_run_id'] == child and repeated.value['replayed']
    h.store.transition_run('default', child, {RunState.CREATED}, RunState.RUNNING)
    with pytest.raises(AgentOSError):
        await h.deps.tools.call(launch, {'inputs': {}}, ctx.model_copy(update={
            'run_id': child, 'idempotency_key': 'recursive'}))
    h.deps.budget.reserve('default', child, 'child-used', 17)
    h.deps.budget.settle('default', 'child-used', 17)
    h.store.transition_run('default', child, {RunState.RUNNING}, RunState.SUCCEEDED)
    assert h.deps.budget.totals('default', receipt.run_id) == {'budget': 100, 'reserved': 0, 'settled': 27}
    status = await h.deps.tools.call('workflow.status', {'wf_run_id': first.value['wf_run_id']}, ctx)
    assert status.value['state'] == 'succeeded'
    assert len(h.store.list_workflow_runs('default')) == 1


async def test_agent_mcp_discovery_reuses_governed_tools_and_env_refs(tmp_path: Path, monkeypatch) -> None:
    h = build_system(Settings(data_dir=tmp_path))
    actor = await h.deps.identity.authenticate({'token': h.boot()})
    p = h.connections.propose(actor, {'server_id': 'external', 'command': ['synthetic-agent'], 'kind': 'agent',
        'env': {'API_KEY': 'env:SYNTHETIC_AGENT_KEY'}, 'risks': ['read']})
    h.connections.apply(actor, p['id'], p['hash'])
    hub = h.tool_hub._inner
    row = h.store.get_mcp_server('default', 'external')
    with pytest.raises(AgentOSError, match='environment'):
        hub._stdio_env(row)
    monkeypatch.setenv('SYNTHETIC_AGENT_KEY', 'synthetic-test-value')
    assert hub._stdio_env(row)['API_KEY'] == 'synthetic-test-value'
    h.store.update_mcp_server_discovery('default', 'external', protocol_version='synthetic',
        tools_json=json.dumps([{'name': 'delegate', 'description': 'Help with analysis',
                               'input_schema': {'type': 'object'}}]))
    h.store.set_mcp_server_enabled('default', 'external', True)
    import time
    hub._discovery[('default', 'external')] = (time.monotonic(), 'refreshed')
    spec = await h.deps.tools.resolve(actor, 'external:delegate')
    assert spec.description.startswith('[External Agent via MCP]')
    await hub.set_enabled(actor, 'external', False)
    assert 'external:delegate' not in {s.name for s in await h.deps.tools.list_tools(actor)}


def test_inbound_mcp_proposal_and_workflow_discovery_share_identity(tmp_path: Path) -> None:
    h = build_system(Settings(data_dir=tmp_path, knowledge_backend='disabled'))
    token = h.boot()
    with TestClient(create_app(system=h), base_url='http://127.0.0.1:8800') as client:
        hdr = {**headers(token), 'Accept': 'application/json, text/event-stream'}
        tools = client.post('/mcp/', headers=hdr, json={'jsonrpc': '2.0', 'id': 1,
            'method': 'tools/list'}).json()['result']['tools']
        assert {'capabilities_list', 'workflow_start', 'workflow_definitions_list',
                'mcp_connection_propose'} <= {t['name'] for t in tools}
        response = client.post('/mcp/', headers=hdr, json={'jsonrpc': '2.0', 'id': 2,
            'method': 'tools/call', 'params': {'name': 'mcp_connection_propose', 'arguments': {'config': {
                'server_id': 'mcp-assistant', 'url': 'https://example.invalid/mcp'}}}}).json()['result']
        assert not response.get('isError')
        assert len(h.store.list_connection_drafts('default')) == 1
        assert h.store.list_mcp_servers('default') == []


async def test_mapping_validation_before_acceptance_and_dependency_boundary(tmp_path: Path) -> None:
    h = build_system(Settings(data_dir=tmp_path))
    actor = await h.deps.identity.authenticate({'token': h.boot()})
    definition = {'id': 'mapped', 'input_schema': {'type': 'object', 'properties': {'amount': {'type': 'number'}}},
        'nodes': [{'id': 'sum', 'kind': 'tool', 'config': {'tool': 'math.add',
            'arguments': {'a': {'$ref': 'inputs.amount'}, 'b': 5}}}]}
    bound = await h.deps.tools.bind_graph(actor, definition)
    h.workflows.publish(actor, bound)
    with pytest.raises(AgentOSError, match='mapped workflow input'):
        h.workflows.start(actor, 'mapped', 1, {}, 'missing')
    assert h.store.list_runs('default') == []
    definition['nodes'][0]['config']['arguments']['a'] = {'$ref': 'nodes.other.sum'}
    with pytest.raises(AgentOSError, match='invalid workflow graph'):
        await h.deps.tools.bind_graph(actor, definition)
    definition['input_schema'] = {'type': 'unsupported'}
    definition['nodes'][0]['config']['arguments']['a'] = 3
    with pytest.raises(AgentOSError, match='input schema'):
        h.workflows.publish(actor, definition)


async def test_autonomous_workflow_launch_waits_for_approval_and_receipt_is_not_completion(tmp_path: Path) -> None:
    h = build_system(Settings(data_dir=tmp_path), model_script=[
        {'when_user_contains': 'autonomous pick', 'script': [
            {'tool_calls': [{'id': 'choose', 'name': 'workflow.delegate.v1', 'arguments': {'inputs': {}}}]},
            {'content': 'Child workflow launched; completion must be checked separately.'}]},
        {'when_user_contains': 't1', 'script': [{'content': 'done'}]},
    ])
    token = h.boot()
    actor = await h.deps.identity.authenticate({'token': token})
    h.workflows.publish(actor, {'id': 'delegate', 'tasks': [
        {'task_id': 't1', 'role_id': 'worker', 'depends_on': [], 'acceptance_checks': ['contains:done']}]})
    receipt = await h.ingress.accept({'idempotency_key': 'autonomous',
        'payload': {'goal': '[[task:autonomous pick]] choose an authorized flow'}},
        channel='http', credentials={'token': token})
    assert await h.worker.drive_until_settled(receipt.run_id) == 'awaiting_approval'
    assert h.store.list_workflow_runs('default') == []
    approval = h.deps.approvals.list_pending('default')[0]
    h.deps.approvals.decide(actor, approval.id, 'approve')
    assert await h.worker.drive_until_settled(receipt.run_id) == 'succeeded'
    children = h.store.list_workflow_runs('default')
    assert len(children) == 1
    child = h.store.get_run('default', children[0]['internal_run_id'])
    assert child.state == RunState.CREATED
    completed = next(s for s in h.store.list_steps('default', receipt.run_id) if s.tool_name == 'workflow.delegate.v1')
    result = json.loads(completed.result_json)
    assert result['value']['launch_only'] is True
    assert await h.worker.drive_until_settled(receipt.run_id) == 'succeeded'
    assert len(h.store.list_workflow_runs('default')) == 1
    assert await h.worker.drive_until_settled(child.id) == 'succeeded'
