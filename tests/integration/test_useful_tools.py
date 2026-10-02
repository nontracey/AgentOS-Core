"""Useful default tools: authorization, exact data analysis and durable delivery."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agentos.adapters.tools.data_tools import csv_profile, json_query
from agentos.apps.composition import build_system
from agentos.apps.config import Settings
from agentos.kernel.errors import AgentOSError
from agentos.kernel.states import RunState
from agentos.kernel.types import CallContext, utc_now
from tests.conftest import add_subject
from tests.integration.test_n3a_template import auth, client, upload, wait_state


def test_ordinary_file_delivery_approval_download_and_replay(make_system):
    script = [{'tool_calls': [{'id': 'write', 'name': 'fs.write_text',
                              'arguments': {'path': 'out/summary.md', 'content': '# Synthetic report\nComplete.'}}]},
              {'content': 'Report saved.'}]
    with client(make_system, script=script) as (c, h, token):
        request = {'payload': {'goal': 'Create a synthetic report'}, 'idempotency_key': 'delivery'}
        rid = c.post('/v1/tasks', headers=auth(token), json=request).json()['run_id']
        assert wait_state(c, token, rid, {'awaiting_approval', 'failed'}) == 'awaiting_approval'
        detail = c.get(f'/v1/tasks/{rid}', headers=auth(token)).json()
        assert detail['artifacts'] == []
        approval = detail['approvals'][0]['id']
        assert c.post(f'/v1/approvals/{approval}/decision', headers=auth(token),
                      json={'decision': 'approve'}).status_code == 200
        assert wait_state(c, token, rid, {'succeeded', 'failed'}) == 'succeeded'
        assets = c.get(f'/v1/tasks/{rid}', headers=auth(token)).json()['artifacts']
        assert len(assets) == 1 and assets[0]['name'] == 'summary.md'
        assert 'path' not in assets[0]
        assert c.get(f"/v1/assets/{assets[0]['id']}/download", headers=auth(token)).text.startswith('# Synthetic')
        assert c.post('/v1/tasks', headers=auth(token), json=request).json()['run_id'] == rid
        assert len(c.get(f'/v1/tasks/{rid}', headers=auth(token)).json()['artifacts']) == 1
        peer = add_subject(h, 'peer', ['read', 'write'])
        assert c.get(f"/v1/assets/{assets[0]['id']}/download", headers=auth(peer)).status_code in {401, 403}


def test_material_discovery_data_tools_and_output_identity(make_system):
    with client(make_system, script=[{'content': 'Synthetic'}]) as (c, h, token):
        mid = c.post('/v1/materials', headers=auth(token), json={'name': 'costs.csv',
            'content_text': 'name,cost\nalpha,12.50\nbeta,7.50', 'content_type': 'text/csv'}).json()['asset_id']
        other = upload(c, token, 'secret', 'unselected.txt')
        rid = c.post('/v1/tasks', headers=auth(token), json={
            'payload': {'goal': 'Inspect authorized table'}, 'material_asset_ids': [mid]}).json()['run_id']
        wait_state(c, token, rid, {'succeeded'})
        h.store.transition_run('default', rid, {RunState.SUCCEEDED}, RunState.RUNNING)
        actor = asyncio.run(h.deps.identity.authenticate({'token': token}))
        ctx = CallContext(run_id=rid, step_id='s1', call_id='c1', actor=actor, idempotency_key='output-one',
                          fence=1, deadline_at=utc_now())
        catalog = asyncio.run(h.deps.tools.call('material.list', {}, ctx)).value['materials']
        assert [m['asset_id'] for m in catalog] == [mid]
        result = asyncio.run(h.deps.tools.call('data.csv_profile', {'asset_id': mid}, ctx)).value
        assert result['columns'][1]['numeric']['sum'] == '20.00'
        assert result['row_count'] == 2
        with pytest.raises(AgentOSError):
            asyncio.run(h.deps.tools.call('data.csv_profile', {'asset_id': other}, ctx))
        args = {'name': 'summary.txt', 'content': 'Total 20.00'}
        first = asyncio.run(h.deps.tools.call('artifact.create', args, ctx)).value
        second = asyncio.run(h.deps.tools.call('artifact.create', args, ctx)).value
        assert first['asset_ref'] == second['asset_ref'] and second['replayed']
        with pytest.raises(AgentOSError):
            asyncio.run(h.deps.tools.call('artifact.create', {**args, 'content': 'changed'}, ctx))
        next_ctx = ctx.model_copy(update={"idempotency_key": "output-two"})
        next_result = asyncio.run(h.deps.tools.call('artifact.create', {**args, 'content': 'Second'}, next_ctx)).value
        assert next_result['asset_ref'] != first['asset_ref']
        assert h.assets.read(actor, first['asset_ref']) == b'Total 20.00'
        denied = asyncio.run(h.deps.tools.call('artifact.create', {**args, 'name': '../escape.md'}, ctx))
        assert denied.status == 'failed'
        assert h.store.list_drafts('default', 'owner', 'workflow') == []


def test_production_catalog_hides_examples_but_legacy_workflows_resolve(tmp_path: Path):
    h = build_system(Settings(data_dir=tmp_path, example_tools=False, knowledge_backend='disabled'))
    token = h.boot()
    actor = asyncio.run(h.deps.identity.authenticate({'token': token}))
    names = {s.name for s in asyncio.run(h.deps.tools.list_tools(actor))}
    assert {'material.list', 'data.csv_profile', 'data.json_query', 'artifact.create'} <= names
    assert not {'math.add', 'log.append', 'flaky.timeout'} & names
    assert asyncio.run(h.deps.tools.resolve(actor, 'math.add')).name == 'math.add'


@pytest.mark.parametrize('text', ['a,a\n1,2', 'a,b\n"unterminated', '', 'a\n' + '\n'.join('x' for _ in range(2001))])
def test_csv_invalid_inputs(text: str):
    with pytest.raises(AgentOSError):
        csv_profile(text)


def test_structured_analysis_does_not_silently_coerce_or_truncate():
    broken = csv_profile('a,b\n1\n2,3')
    assert broken['valid'] is False and broken['invalid_rows'] == [2]
    precise = csv_profile('value\n123456789012345678901234567890.12\n0.01')
    assert precise['columns'][0]['numeric']['sum'] == '123456789012345678901234567890.13'
    mixed = csv_profile('name,value\nalpha,3\nbeta,unknown')
    assert 'numeric' not in mixed['columns'][1]
    assert json_query('{"a":[{"value":42}]}', ['a', 0, 'value'])['value'] == 42
    for text, path in [('NaN', []), ('1e999', []), ('{"a":1}', ['missing']),
                       ('[1]', [False]), (json.dumps('x' * 33000), [])]:
        with pytest.raises(AgentOSError):
            json_query(text, path)


def test_autonomous_report_revision_keeps_parent_and_old_bytes(make_system):
    script = [{'tool_calls': [{'id': 'output', 'name': 'artifact.create',
                              'arguments': {'name': 'report.md', 'content': '# Synthetic report'}}]},
              {'content': 'Report saved.'}]
    with client(make_system, script=script) as (c, h, token):
        def deliver(payload):
            rid = c.post('/v1/tasks', headers=auth(token), json={'payload': payload}).json()['run_id']
            assert wait_state(c, token, rid, {'awaiting_approval', 'failed'}) == 'awaiting_approval'
            detail = c.get(f'/v1/tasks/{rid}', headers=auth(token)).json()
            assert c.post(f"/v1/approvals/{detail['approvals'][0]['id']}/decision", headers=auth(token),
                          json={'decision': 'approve'}).status_code == 200
            assert wait_state(c, token, rid, {'succeeded', 'failed'}) == 'succeeded'
            return c.get(f'/v1/tasks/{rid}', headers=auth(token)).json()['artifacts'][0]
        first = deliver({'goal': 'Write a report'})
        # Continuations retain the preceding assistant answer in model context.
        h.loop._model._script.insert(1, script[0])
        second = deliver({'goal': 'Revise the selected report', 'parent_asset_id': first['id']})
        assert first['version'] == 1 and second['version'] == 2
        assert second['parent_asset_id'] == first['id']
        assert second['revision_reason'] == 'Revise the selected report'
        assert first['id'] != second['id']
        assert c.get(f"/v1/assets/{first['id']}/download", headers=auth(token)).text == '# Synthetic report'
