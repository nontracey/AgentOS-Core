"""Execution evidence: real ledger, scoped reads, safe prompts and cursor pagination.

Scripted model and stub node transport are mechanism evidence, not Windows acceptance.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from agentos.apps.composition import SystemHandle
from agentos.apps.gateway import create_app
from agentos.kernel.types import ChatMessage
from agentos.services.observability import prompt_evidence, public_json, public_value
from tests.conftest import WRITE_FLOW_SCRIPT, add_subject, owner_token
from tests.integration.test_n3a_template import P1_REPORT, auth, run_template, upload, wait_state


def test_trace_records_calls_policy_and_keeps_private_checkpoint_internal(
    make_system: Callable[..., SystemHandle],
) -> None:
    handle = make_system(model_script=WRITE_FLOW_SCRIPT)
    with TestClient(create_app(system=handle)) as c:
        token = owner_token(handle)
        rid = c.post('/v1/tasks', headers=auth(token), json={
            'payload': {'goal': 'add two numbers then save'}, 'idempotency_key': 'trace1',
        }).json()['run_id']
        assert wait_state(c, token, rid, {'awaiting_approval'}) == 'awaiting_approval'
        detail = c.get(f'/v1/tasks/{rid}', headers=auth(token)).json()
        assert 'checkpoint_json' not in detail['run'] and 'envelope_json' not in detail['run']
        ev = c.get(f'/v1/tasks/{rid}/trace', headers=auth(token)).json()['events']
        prompts = [e for e in ev if e['kind'] == 'model.request']
        assert prompts and prompts[0]['payload']['messages'][-1]['content'] == 'add two numbers then save'
        assert all(m['role'] in {'system', 'user'} for p in prompts for m in p['payload']['messages'])
        prepared = [e['payload'] for e in ev if e['kind'] == 'tool.prepared']
        assert prepared[0]['policy'] == 'allowed'
        assert prepared[-1]['policy'] == 'needs_approval'
        assert prepared[-1]['arguments']['path'] == '[已隐藏]'
        assert prepared[-1]['arguments']['content'] == 'hello agentos'
        assert any(e['kind'] == 'model.tool_selection' for e in ev)
        assert c.get(f'/v1/tasks/{rid}/trace').status_code == 401
        other = add_subject(handle, 'other', ['read', 'write'])
        assert c.get(f'/v1/tasks/{rid}/trace', headers=auth(other)).status_code == 404
        # A replay of the same accepted intent does not record new model/tool work.
        count = len(ev)
        c.post('/v1/tasks', headers=auth(token), json={
            'payload': {'goal': 'add two numbers then save'}, 'idempotency_key': 'trace1',
        })
        assert len(c.get(f'/v1/tasks/{rid}/trace', headers=auth(token)).json()['events']) == count


def test_template_prompt_and_safe_receipts(make_system: Callable[..., SystemHandle]) -> None:
    handle = make_system(model_script=[{'content': P1_REPORT}])
    with TestClient(create_app(system=handle)) as c:
        token = owner_token(handle)
        asset = upload(c, token, '项目甲：3 个任务，2 个完成，1 个缺输入。')
        rid = run_template(c, token, '写进展报告', [asset], 'tpl_trace')
        detail = c.get(f'/v1/tasks/{rid}', headers=auth(token)).json()
        assert detail['plan']['source'] == 'template'
        trace = c.get(f'/v1/tasks/{rid}/trace', headers=auth(token)).json()['events']
        request = next(e['payload'] for e in trace if e['kind'] == 'model.request')
        assert '材料 S1' in request['messages'][-1]['content']
        assert '3 个任务' in request['messages'][-1]['content']
        assert all('path' not in json.loads(s['result_json'] or '{}').get('value', {}) or
                   json.loads(s['result_json'])['value']['path'] == '[已隐藏]'
                   for s in detail['steps'])
        assert any(e['kind'] == 'report.citation_check' for e in trace)


def test_trace_cursor_keeps_all_events_and_metrics(make_system: Callable[..., SystemHandle]) -> None:
    handle = make_system(model_script=[{'content': 'done'}])
    with TestClient(create_app(system=handle)) as c:
        token = owner_token(handle)
        rid = c.post('/v1/tasks', headers=auth(token), json={'payload': {'goal': 'test'}}).json()['run_id']
        wait_state(c, token, rid, {'succeeded'})
        for n in range(230):
            handle.store.event_append('default', rid, 'probe', {
                'n': n, 'input_tokens': 7, 'output_tokens': 2, 'endpoint': 'http://private.invalid',
            })
        first = c.get(f'/v1/tasks/{rid}/trace?limit=200', headers=auth(token)).json()
        second = c.get(f'/v1/tasks/{rid}/trace?cursor={first["cursor"]}', headers=auth(token)).json()
        combined = first['events'] + second['events']
        assert len(first['events']) == 200
        probes = [e['payload'] for e in combined if e['kind'] == 'probe']
        assert [p['n'] for p in probes] == list(range(230))
        assert probes[0]['input_tokens'] == 7 and probes[0]['endpoint'] == '[已隐藏]'


def test_node_dispatch_evidence_has_actual_mapping_no_endpoint(
    make_system: Callable[..., SystemHandle], monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = [{'tool_calls': [{'id': 'c1', 'name': 'node_demo_image_generate', 'arguments': {'prompt': 'test'}}]},
              {'content': 'done'}]
    handle = make_system(model_script=script)
    cap = {'name': 'image.generate', 'version': '1', 'risks': ['read'],
           'input_schema': {'type': 'object'}, 'description': 'stub image tool'}
    monkeypatch.setattr(httpx, 'get', lambda *a, **kw: httpx.Response(200, json=[cap]))
    monkeypatch.setattr(httpx, 'post', lambda *a, **kw: httpx.Response(200, json={
        'status': 'completed', 'value': {'ok': True},
    }))
    with TestClient(create_app(system=handle)) as c:
        token = owner_token(handle)
        c.post('/v1/nodes/pair', headers=auth(token), json={
            'node_id': 'demo', 'endpoint': 'http://stub.invalid', 'os': 'Windows',
        })
        rid = c.post('/v1/tasks', headers=auth(token), json={'payload': {'goal': 'use stub image'}}).json()['run_id']
        assert wait_state(c, token, rid, {'succeeded', 'failed'}) == 'succeeded'
        trace = c.get(f'/v1/tasks/{rid}/trace', headers=auth(token)).json()['events']
        route = next(e['payload'] for e in trace if e['kind'] == 'node.dispatched')
        assert route['node_id'] == 'demo' and route['os'] == 'Windows' and route['tool'] == 'image.generate'
        assert 'endpoint' not in route
        caps = c.get('/v1/capabilities', headers=auth(token)).json()
        assert caps['model']['health'] == 'not_probed'
        assert 'endpoint' not in caps['nodes'][0] and 'token_plain' not in caps['nodes'][0]


def test_public_evidence_omits_reasoning_and_redacts_credentials() -> None:
    p = prompt_evidence([
        ChatMessage(role='user', content='secret=placeholder\nhttp://internal.invalid\nC:\\data\\file'),
        ChatMessage(role='assistant', content='private deliberation'),
        ChatMessage(role='tool', content='raw tool body', tool_call_id='c1'),
    ], 'call1')
    assert len(p['messages']) == 1
    assert 'placeholder' not in p['messages'][0]['content']
    assert 'internal.invalid' not in p['messages'][0]['content']
    clean: Any = public_value({'input_tokens': 3, 'access_token': 'placeholder', 'nested': {'path': 'relative/file'}})
    assert clean['input_tokens'] == 3 and clean['access_token'] == '[已隐藏]'
    assert clean['nested']['path'] == '[已隐藏]'
    assert '已截断' in public_value('x' * 16001)
    assert 'internal.invalid' not in public_json('cannot connect http://internal.invalid')
    assert json.loads(public_json('old non-JSON message'))['message'] == 'old non-JSON message'
