"""Scripted/stub regressions for approval resume and remote delivery; not GPU evidence."""
from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from agentos.apps.composition import SystemHandle
from agentos.apps.gateway import create_app
from tests.conftest import add_subject, owner_token
from tests.integration.test_n3a_template import auth, wait_state


def test_approval_resumes_saved_round_without_another_model_request(
    make_system: Callable[..., SystemHandle],
) -> None:
    calls = [
        {'id': 'read', 'name': 'math.add', 'arguments': {'a': 1, 'b': 2}},
        {'id': 'write1', 'name': 'fs.write_text', 'arguments': {'path': 'one.txt', 'content': 'one'}},
        {'id': 'write2', 'name': 'fs.write_text', 'arguments': {'path': 'two.txt', 'content': 'two'}},
    ]
    handle = make_system(model_script=[{'tool_calls': calls}, {'content': 'done'}])
    with TestClient(create_app(system=handle)) as c:
        token = owner_token(handle)
        rid = c.post('/v1/tasks', headers=auth(token), json={'payload': {'goal': 'save two'}}).json()['run_id']
        for expected in (2, 3):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                d = c.get(f'/v1/tasks/{rid}', headers=auth(token)).json()
                if len(d['steps']) == expected and d['approvals']:
                    break
                time.sleep(.02)
            assert wait_state(c, token, rid, {'awaiting_approval', 'failed'}) == 'awaiting_approval'
            d = c.get(f'/v1/tasks/{rid}', headers=auth(token)).json()
            assert len(d['steps']) == expected
            trace = c.get(f'/v1/tasks/{rid}/trace', headers=auth(token)).json()['events']
            assert sum(e['kind'] == 'model.request' for e in trace) == 1
            assert len(d['approvals']) == 1
            assert c.post(f'/v1/approvals/{d["approvals"][0]["id"]}/decision',
                          headers=auth(token), json={'decision': 'approve'}).status_code == 200
        assert wait_state(c, token, rid, {'succeeded', 'failed'}) == 'succeeded'
        trace = c.get(f'/v1/tasks/{rid}/trace', headers=auth(token)).json()['events']
        assert sum(e['kind'] == 'model.request' for e in trace) == 2
        assert (handle.settings.sandbox_dir / 'one.txt').read_text() == 'one'
        assert (handle.settings.sandbox_dir / 'two.txt').read_text() == 'two'


@pytest.mark.parametrize('corrupt', [False, True])
def test_remote_receipt_delivery_scoped_deduplicated_and_hash_checked(
    make_system: Callable[..., SystemHandle], monkeypatch: pytest.MonkeyPatch, corrupt: bool,
) -> None:
    data = b'\x89PNG\r\n\x1a\nstub-media'
    sha = hashlib.sha256(data).hexdigest()
    cap = {'name': 'image.generate', 'version': '1', 'risks': ['read'],
           'input_schema': {'type': 'object'}, 'description': 'stub media'}
    requests: list[str] = []

    def get(url: str, **kw: Any) -> httpx.Response:
        assert kw['trust_env'] is False
        requests.append(url)
        return (httpx.Response(200, json=[cap]) if url.endswith('/tools') else
                httpx.Response(200, content=b'corrupt' if corrupt else data))

    monkeypatch.setattr(httpx, 'get', get)
    executions = []

    def post(*args: Any, **kw: Any) -> httpx.Response:
        executions.append(kw['json'])
        return httpx.Response(200, json={'status': 'completed',
                             'value': {'asset_ref': 'test.png', 'sha256': sha}})

    monkeypatch.setattr(httpx, 'post', post)
    handle = make_system(model_script=[{'tool_calls': [{
        'id': 'c1', 'name': 'node_demo_image_generate', 'arguments': {'prompt': 'test'},
    }]}, {'content': 'done'}])
    with TestClient(create_app(system=handle)) as c:
        token = owner_token(handle)
        c.post('/v1/nodes/pair', headers=auth(token), json={
            'node_id': 'demo', 'endpoint': 'http://stub.invalid', 'os': 'windows',
        })
        rid = c.post('/v1/tasks', headers=auth(token), json={'payload': {'goal': 'media'}}).json()['run_id']
        assert wait_state(c, token, rid, {'succeeded', 'failed', 'paused'}) == 'succeeded'
        d = c.get(f'/v1/tasks/{rid}', headers=auth(token)).json()
        assert len(executions) == 1  # 交付失败不能重跑生成。
        trace = c.get(f'/v1/tasks/{rid}/trace', headers=auth(token)).json()['events']
        if corrupt:
            assert not d['artifacts']
            assert any(e['kind'] == 'asset.delivery_failed' for e in trace)
            return
        assert len(d['artifacts']) == 1
        asset = d['artifacts'][0]
        assert asset['run_id'] == rid and asset['source_node'] == 'demo'
        assert asset['mime'] == 'image/png' and 'path' not in asset
        assert c.get(f'/v1/assets/{asset["id"]}/download', headers=auth(token)).content == data
        before = len(requests)
        r = c.post('/v1/nodes/demo/assets/fetch', headers=auth(token), json={
            'remote_ref': 'test.png', 'sha256': sha, 'run_id': rid, 'mime': 'image/png',
        })
        assert r.status_code == 200 and r.json()['asset_id'] == asset['id']
        assert len(requests) == before  # 重复补传复用登记，不重新下载。
        other = add_subject(handle, 'other', ['read', 'write', 'approve', 'exec'])
        assert c.get(f'/v1/assets/{asset["id"]}', headers=auth(other)).status_code in {401, 403}
        assert c.post('/v1/nodes/demo/assets/fetch', headers=auth(token), json={
            'remote_ref': '../escape', 'sha256': sha, 'run_id': rid,
        }).status_code == 422
        assert c.post('/v1/nodes/demo/assets/fetch', headers=auth(token), json={
            'remote_ref': 'test.png', 'sha256': sha, 'run_id': 'unknown',
        }).status_code == 401
