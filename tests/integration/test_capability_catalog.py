"""CAP-01: synthetic services, real discovery transports, governance and version boundaries."""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agentos.apps.composition import build_system
from agentos.apps.config import Settings
from agentos.apps.gateway import create_app
from agentos.kernel.errors import AgentOSError
from agentos.kernel.types import CallContext, utc_now
from agentos.runtime.agent_driver import AgentRuntimeDriver
from tests.conftest import add_subject


def test_disabled_knowledge_core_and_inbound_mcp(tmp_path: Path) -> None:
    h = build_system(Settings(data_dir=tmp_path, knowledge_backend="disabled"), model_script=[{"content": "done"}])
    token = h.boot()
    with TestClient(create_app(system=h), base_url='http://127.0.0.1:8800') as client:
        headers = {"Authorization": "Bearer " + token}
        assert client.get('/healthz').status_code == 200
        assert client.get('/ui/').status_code == 200
        assert client.get('/v1/capabilities', headers=headers).json()['features']['knowledge'] is False
        caps = client.get('/v1/knowledge/capabilities', headers=headers).json()
        assert caps['enabled'] is False and caps['choices'] == []
        tools = client.get('/v1/workflows/catalog', headers=headers).json()['tools']
        assert 'knw.search' not in {t['name'] for t in tools}
        assert client.get('/v1/knowledge/search?q=x', headers=headers).status_code == 501
        assert client.post('/v1/knowledge/backend', headers=headers,
                           json={'backend': 'builtin-sqlite'}).status_code == 501
        assert client.post('/v1/knowledge/ingest', headers=headers,
                           json={'source_id': 'synthetic', 'text': 'test'}).status_code == 501
        response = client.post('/mcp/', headers={**headers, 'Accept': 'application/json, text/event-stream'},
                               json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        assert response.status_code == 200
        assert 'knowledge_search' not in {t['name'] for t in response.json()['result']['tools']}
        accepted = client.post('/v1/tasks', headers=headers, json={'payload': {'goal': 'synthetic'}})
        assert accepted.status_code == 202
        run = h.store.get_run('default', accepted.json()['run_id'])
        assert run and 'knowledge_binding' not in json.loads(run.envelope_json)['payload']


def test_catalog_shared_scope_filtered_and_no_discovery_permission(tmp_path: Path) -> None:
    h = build_system(Settings(data_dir=tmp_path))
    token = h.boot()
    read_token = add_subject(h, 'reader', ['read'])
    with TestClient(create_app(system=h), base_url='http://127.0.0.1:8800') as client:
        headers = {'Authorization': 'Bearer ' + read_token}
        tools = client.get('/v1/capabilities/catalog', headers=headers).json()['tools']
        assert 'fs.write_text' not in {t['name'] for t in tools}
        assert tools == client.get('/v1/workflows/catalog', headers=headers).json()['tools']
        spec = next(t for t in tools if t['name'] == 'math.add')
        assert spec['input_schema']['required'] == ['a', 'b']
        assert spec['version'] and len(spec['revision']) == 64
        assert spec['authorization'] == 'policy_at_dispatch'
        assert spec['required_scopes'] == ['read'] and spec['discovery_status'] == 'local'
        actor = asyncio.run(h.deps.identity.authenticate({'token': token}))
        writes = asyncio.run(h.deps.tools.resolve(actor, 'fs.write_text'))
        assert h.deps.policy.check(actor, writes, {'path': 'a.txt', 'content': 'x'}).outcome == 'needs_approval'
        reader = asyncio.run(h.deps.identity.authenticate({'token': read_token}))
        with pytest.raises(AgentOSError, match='outside caller scope'):
            asyncio.run(h.deps.tools.resolve(reader, 'fs.write_text'))
        guest = reader.model_copy(update={'roles': ['guest']})
        assert asyncio.run(h.deps.tools.list_tools(guest)) == []


async def test_mcp_refresh_failure_isolation_and_disabled_direct_call(tmp_path: Path, monkeypatch) -> None:
    h = build_system(Settings(data_dir=tmp_path))
    actor = await h.deps.identity.authenticate({'token': h.boot()})
    hub = h.tool_hub._inner
    server = Path(__file__).parents[2] / 'examples/mcp_demo_server.py'
    await hub.add_server(actor, 'demo', command=f'{sys.executable} {server}', risks=['read'])
    assert 'demo:echo' in {t.name for t in await h.deps.tools.list_tools(actor)}
    calls = 0

    async def changed(row):
        nonlocal calls
        calls += 1
        return 'synthetic-protocol', [SimpleNamespace(name='new-tool', description='new',
            input_schema={'type': 'object', 'properties': {'text': {'type': 'string'}}})]

    monkeypatch.setattr(hub, '_stdio_discover', changed)
    hub._discovery.clear()
    assert 'demo:new-tool' in {t.name for t in await h.deps.tools.list_tools(actor)}
    await h.deps.tools.list_tools(actor)
    assert calls == 1  # TTL does not spawn a subprocess for every model/UI read.

    async def offline(row):
        raise RuntimeError('provider-private-diagnostic')

    monkeypatch.setattr(hub, '_stdio_discover', offline)
    hub._discovery.clear()
    described = await h.deps.tools.describe(actor)
    assert next(t for t in described if t['name'] == 'demo:new-tool')['discovery_status'] == 'unavailable'
    assert 'demo:new-tool' not in {t.name for t in await h.deps.tools.list_tools(actor)}
    assert 'math.add' in {t.name for t in await h.deps.tools.list_tools(actor)}
    assert 'provider-private-diagnostic' not in json.dumps(described)
    await hub.set_enabled(actor, 'demo', False)
    ctx = CallContext(run_id='r', step_id='s', call_id='c', actor=actor, idempotency_key='k', fence=1,
                      deadline_at=utc_now() + timedelta(minutes=1))
    with pytest.raises(AgentOSError, match='disabled'):
        await hub.call('demo:new-tool', {}, ctx)
    assert 'demo:new-tool' not in {t.name for t in await h.deps.tools.list_tools(actor)}


async def test_probe_never_reenables_disabled_connection(tmp_path: Path, monkeypatch) -> None:
    h = build_system(Settings(data_dir=tmp_path))
    actor = await h.deps.identity.authenticate({'token': h.boot()})
    hub = h.tool_hub._inner

    async def empty(row):
        return 'synthetic', []

    monkeypatch.setattr(hub, '_http_discover', empty)
    await hub.add_server(actor, 'disabled', url='http://127.0.0.1:9/mcp')
    await hub.set_enabled(actor, 'disabled', False)
    await hub.probe(actor, 'disabled')
    assert h.store.get_mcp_server(actor.tenant_id, 'disabled')['enabled'] == 0


def test_published_contract_fixed_restart_and_drift_no_effect(tmp_path: Path) -> None:
    async def run():
        h = build_system(Settings(data_dir=tmp_path))
        token = h.boot()
        actor = await h.deps.identity.authenticate({'token': token})
        definition = {'id': 'pinned', 'nodes': [{'id': 'sum', 'kind': 'tool',
            'config': {'tool': 'math.add', 'arguments': {'a': 2, 'b': 3},
                       'capability_bindings': {'math.add': 'forged'}}}]}
        bound = await h.deps.tools.bind_graph(actor, definition)
        spec = await h.deps.tools.resolve(actor, 'math.add')
        assert bound['nodes'][0]['config']['capability_bindings'] == {'math.add': spec.revision}
        published = h.workflows.publish(actor, bound)
        rebuilt = build_system(Settings(data_dir=tmp_path))
        local = rebuilt.tool_hub._inner._local
        local._tools['math.add'][0].version = '2.0.0'
        receipt = rebuilt.workflows.start(actor, 'pinned', published['version'], {}, 'drift')
        result = await rebuilt.workflow_driver.node({'tenant_id': 'default',
            'run_id': receipt['internal_run_id'], 'node_id': 'sum'})
        assert result['status'] == 'failed'
        assert 'republish' in rebuilt.store.get_run('default', receipt['internal_run_id']).error_json
        assert not any(e['kind'] == 'tool.called' for e in rebuilt.store.event_list(
            'default', receipt['internal_run_id']))
        assert rebuilt.workflows.export(actor, 'pinned', 1)['definition'] == bound
    asyncio.run(run())


def test_optional_temporal_startup_failure_preserves_core(tmp_path: Path) -> None:
    h = build_system(Settings(data_dir=tmp_path, temporal_address='127.0.0.1:9'))

    class Offline:
        async def open(self):
            raise RuntimeError('synthetic unavailable')
        async def close(self):
            pass

    h.workflow_engine = Offline()
    token = h.boot()
    with TestClient(create_app(system=h), base_url='http://127.0.0.1:8800') as client:
        assert client.get('/healthz').status_code == 200
        caps = client.get('/v1/workflows/catalog', headers={'Authorization': 'Bearer ' + token}).json()
        assert caps['engine_status'] == 'unavailable' and caps['engine_enabled'] is False
        assert client.get('/ui/').status_code == 200


async def test_agent_runtime_receives_same_schema_and_dispatches_discovered_tool(tmp_path: Path) -> None:
    h = build_system(Settings(data_dir=tmp_path, knowledge_backend='disabled'))
    token = h.boot()
    actor = await h.deps.identity.authenticate({'token': token})
    hub = h.tool_hub._inner
    server = Path(__file__).parents[2] / 'examples/mcp_demo_server.py'
    await hub.add_server(actor, 'demo', command=f'{sys.executable} {server}', risks=['read'])

    class Engine:
        async def stream(self, request, execute):
            tool = next(t for t in request.tools if t.name == 'demo:add')
            assert tool.input_schema['required'] == ['a', 'b']
            assert 'knw.search' not in {t.name for t in request.tools}
            result = await execute('catalog-call', 'demo:add', {'a': 2, 'b': 3})
            assert not result['blocked']
            yield {'type': 'assistant_message', 'content': '5'}
            yield {'type': 'completed'}

    h.loop.use_engine(AgentRuntimeDriver(h.deps, Engine(), h.settings.sandbox_dir))
    receipt = await h.ingress.accept({'payload': {'goal': 'synthetic sum'}},
                                    channel='http', credentials={'token': token})
    assert await h.worker.drive_until_settled(receipt.run_id) == 'succeeded'
    assert len(h.store.list_steps('default', receipt.run_id)) == 1


async def test_workflow_agent_cannot_call_tool_outside_selected_capabilities(tmp_path: Path) -> None:
    h = build_system(Settings(data_dir=tmp_path))
    actor = await h.deps.identity.authenticate({'token': h.boot()})

    class Engine:
        async def stream(self, request, handler):
            assert {s.name for s in request.tools} == {'math.add'}
            denied = await handler('forged', 'fs.write_text', {'path': 'forged.txt', 'content': 'x'})
            assert denied['status'] == 'denied'
            result = await handler('sum', 'math.add', {'a': 2, 'b': 3})
            assert result['success']
            yield {'type': 'assistant_message', 'content': '5'}
            yield {'type': 'completed'}

    h.workflow_driver.agent = AgentRuntimeDriver(h.deps, Engine(), h.settings.sandbox_dir)
    graph = await h.deps.tools.bind_graph(actor, {'id': 'selection', 'nodes': [{'id': 'ai', 'kind': 'agent',
        'config': {'tools': ['math.add']}}]})
    published = h.workflows.publish(actor, graph)
    receipt = h.workflows.start(actor, 'selection', published['version'], {}, 'selection')
    result = await h.workflow_driver.node({'tenant_id': 'default',
        'run_id': receipt['internal_run_id'], 'node_id': 'ai'})
    assert result['status'] == 'completed'
    assert not (h.settings.sandbox_dir / 'forged.txt').exists()
    assert len(h.store.list_steps('default', receipt['internal_run_id'])) == 1


async def test_confirmed_effect_replays_after_contract_change_without_new_call(tmp_path: Path) -> None:
    h = build_system(Settings(data_dir=tmp_path))
    actor = await h.deps.identity.authenticate({'token': h.boot()})
    graph = await h.deps.tools.bind_graph(actor, {'id': 'replay', 'nodes': [{'id': 'sum', 'kind': 'tool',
        'config': {'tool': 'math.add', 'arguments': {'a': 2, 'b': 3}}}]})
    published = h.workflows.publish(actor, graph)
    receipt = h.workflows.start(actor, 'replay', published['version'], {}, 'replay')
    binding = {'tenant_id': 'default', 'run_id': receipt['internal_run_id'], 'node_id': 'sum'}
    assert (await h.workflow_driver.node(binding))['status'] == 'completed'
    # Simulate losing only the node completion checkpoint, keeping the authoritative effect ledger.
    h.store.save_checkpoint('default', receipt['internal_run_id'], '{}')
    local = h.tool_hub._inner._local
    local._tools['math.add'][0].version = '2.0.0'

    async def forbidden(args, ctx):
        raise AssertionError('confirmed effect was repeated')

    local._math_add = forbidden
    assert (await h.workflow_driver.node(binding))['status'] == 'completed'
    assert len(h.store.list_steps('default', receipt['internal_run_id'])) == 1


def test_http_publish_writes_binding_and_rejects_malformed_graph(tmp_path: Path) -> None:
    h = build_system(Settings(data_dir=tmp_path))
    token = h.boot()
    with TestClient(create_app(system=h)) as client:
        headers = {'Authorization': 'Bearer ' + token}
        graph = {'nodes': [{'id': 'sum', 'kind': 'tool',
            'config': {'tool': 'math.add', 'arguments': {'a': 2, 'b': 3},
                       'capability_bindings': {'math.add': 'forged'}}}]}
        response = client.post('/v1/workflows/bound/publish', headers=headers, json=graph)
        assert response.status_code == 201
        saved = client.get('/v1/workflows/bound/versions/1', headers=headers).json()['definition']
        revision = saved['nodes'][0]['config']['capability_bindings']['math.add']
        assert len(revision) == 64 and revision != 'forged'
        assert client.post('/v1/workflows/bound/start', headers=headers,
                           json={'version': 1, 'inputs': {}, 'idempotency_key': 'no-engine'}).status_code == 503
        assert h.store.list_workflow_runs('default') == []
        assert client.post('/v1/workflows/bad/publish', headers=headers,
                           json={'nodes': [{'id': 'bad', 'kind': 'alien'}]}).status_code == 422


def test_explicit_external_knowledge_never_silently_selects_builtin(tmp_path: Path) -> None:
    h = build_system(Settings(data_dir=tmp_path, knowledge_backend='weknora',
        knowledge_weknora_url='http://127.0.0.1:9', knowledge_weknora_version='0.8.2'))
    assert h.knowledge.backend_id == 'weknora'


async def test_dispatched_contract_drift_preserves_unknown_effect(tmp_path: Path) -> None:
    from agentos.kernel.states import StepState
    from agentos.kernel.store import StepRecord
    from agentos.runtime.executor import args_hash

    h = build_system(Settings(data_dir=tmp_path))
    actor = await h.deps.identity.authenticate({'token': h.boot()})
    graph = await h.deps.tools.bind_graph(actor, {'id': 'unknown-effect', 'nodes': [{'id': 'sum', 'kind': 'tool',
        'config': {'tool': 'math.add', 'arguments': {'a': 2, 'b': 3}}}]})
    published = h.workflows.publish(actor, graph)
    receipt = h.workflows.start(actor, 'unknown-effect', published['version'], {}, 'unknown')
    h.store.create_step(StepRecord(tenant_id='default', run_id=receipt['internal_run_id'], id='flow-sum',
        call_id='synthetic-call', tool_name='math.add', args_hash=args_hash({'a': 2, 'b': 3}),
        state=StepState.DISPATCHED))
    h.tool_hub._inner._local._tools['math.add'][0].version = '2.0.0'
    result = await h.workflow_driver.node({'tenant_id': 'default',
        'run_id': receipt['internal_run_id'], 'node_id': 'sum'})
    assert result['status'] == 'blocked'
    assert h.store.get_step('default', receipt['internal_run_id'], 'flow-sum').state == StepState.UNCERTAIN
    assert h.store.get_run('default', receipt['internal_run_id']).state.value == 'paused'
