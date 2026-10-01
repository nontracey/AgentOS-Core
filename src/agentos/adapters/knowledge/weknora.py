"""WeKnora v0.8.2 official API with persistent platform source/ACL bindings.

The service key grants upstream access, never end-user access. Search sends an
explicit allowed knowledge_ids list BEFORE retrieval, then checks every returned
KB/document/chunk again. Empty scopes never become an unfiltered remote search.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.store import StoreProtocol
from agentos.kernel.types import AuthContext, KnowledgeHit, Ref

BACKEND_ID = 'weknora'
CAPABILITIES = {'ingest': True, 'search': True, 'resolve_ref': True, 'delete': True,
    'scope_limit_before_search': True, 'revalidate_after_search': True,
    'acl': 'persistent tenant/subject source bindings + prefiltered knowledge_ids + response revalidation'}


@dataclass
class WeKnoraConfig:
    base_url: str
    token: str
    pinned_version: str
    kb_id: str = ''
    timeout_s: float = 120
    mapping_file: Path | None = None
    auth_kind: str = 'api_key'
    verification_token: str = ''  # Optional setup-only bearer for version probing; not used for content.

    @classmethod
    def from_settings(cls, settings: Any) -> WeKnoraConfig:
        if not settings.knowledge_weknora_url or not settings.knowledge_weknora_version:
            raise AgentOSError(ErrorCode.INVALID, 'WeKnora requires a URL and pinned version')
        return cls(settings.knowledge_weknora_url.rstrip('/'), settings.knowledge_weknora_token,
                   settings.knowledge_weknora_version, settings.knowledge_weknora_kb_id,
                   mapping_file=settings.data_dir / 'weknora-bindings.db',
                   auth_kind=getattr(settings, 'knowledge_weknora_auth_kind', 'api_key'),
                   verification_token=getattr(settings, 'knowledge_weknora_probe_token', ''))


class WeKnoraKnowledgeAdapter:
    provider = BACKEND_ID

    def __init__(self, config: WeKnoraConfig, *, tenant_id: str = 'default',
                 store: StoreProtocol | None = None) -> None:
        self._config = config
        self._store = store
        self._db: sqlite3.Connection | None = None
        if config.mapping_file:
            config.mapping_file.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(config.mapping_file, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._db.execute('''CREATE TABLE IF NOT EXISTS sources(
                tenant TEXT, source TEXT, owner TEXT, acl TEXT, version TEXT, remote TEXT,
                kb TEXT, status TEXT, PRIMARY KEY(tenant,source))''')
            self._db.commit()
        self._lock = asyncio.Lock()

    @property
    def backend_id(self) -> str:
        return BACKEND_ID

    @property
    def backend_version(self) -> str:
        return self._config.pinned_version

    def capabilities(self) -> dict[str, Any]:
        enabled = self._db is not None and bool(self._config.kb_id)
        return {'backend': BACKEND_ID, 'version': self.backend_version,
                **{k: v if enabled or not isinstance(v, bool) else False for k, v in CAPABILITIES.items()}}

    def _database(self) -> sqlite3.Connection:
        if self.backend_version.lstrip('v') != '0.8.2':
            raise AgentOSError(ErrorCode.DENIED, 'unverified WeKnora protocol version')
        if self._db is None or not self._config.kb_id:
            raise AgentOSError(ErrorCode.DENIED, 'persistent WeKnora source/ACL mapping is required')
        return self._db

    def _headers(self) -> dict[str, str]:
        if self._config.auth_kind == 'bearer':
            return {'Authorization': 'Bearer ' + self._config.token}
        return {'X-API-Key': self._config.token}

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self._config.timeout_s, trust_env=False) as client:
                response = await client.request(method, self._config.base_url + path,
                                                headers=kwargs.pop('headers', self._headers()), **kwargs)
        except httpx.HTTPError as exc:
            raise AgentOSError(ErrorCode.UNAVAILABLE, 'WeKnora transport unavailable') from exc
        if response.status_code == 404:
            raise AgentOSError(ErrorCode.INVALID, 'WeKnora resource not found')
        if response.status_code >= 400:
            raise AgentOSError(ErrorCode.UNAVAILABLE, 'WeKnora request rejected',
                               detail={'http_status': response.status_code})
        try:
            result = response.json()
        except ValueError as exc:
            raise AgentOSError(ErrorCode.UNCERTAIN, 'WeKnora returned invalid JSON') from exc
        if not isinstance(result, dict) or result.get('success') is False:
            raise AgentOSError(ErrorCode.UNAVAILABLE, 'WeKnora operation unsuccessful')
        return result

    async def probe(self) -> dict[str, Any]:
        self._database()
        await self._request('GET', '/health')
        probe_headers = ({'Authorization': 'Bearer ' + self._config.verification_token}
                         if self._config.verification_token else self._headers())
        info = await self._request('GET', '/api/v1/system/info', headers=probe_headers)
        data = info.get('data', info)
        actual = str(data.get('version', ''))
        if actual.lstrip('v') != self.backend_version.lstrip('v'):
            raise AgentOSError(ErrorCode.INVALID, 'WeKnora version differs from deployment pin')
        await self._request('GET', '/api/v1/knowledge-bases/' + quote(self._config.kb_id, safe=''))
        return {'reachable': True, **self.capabilities()}

    def _source(self, actor: AuthContext, source_id: str, *, write: bool = False) -> sqlite3.Row:
        if actor.is_expired or not actor.has_scope('read'):
            raise AgentOSError(ErrorCode.DENIED, 'knowledge identity unavailable')
        row = self._database().execute('SELECT * FROM sources WHERE tenant=? AND source=?',
                                       (actor.tenant_id, source_id)).fetchone()
        if self._store and 'weknora:' + source_id in self._store.list_tombstones(actor.tenant_id):
            raise AgentOSError(ErrorCode.DENIED, 'knowledge source was deleted')
        if row is None or row['status'] != 'active' or actor.subject not in json.loads(row['acl']):
            raise AgentOSError(ErrorCode.DENIED, 'knowledge source is outside current scope')
        if write and (row['owner'] != actor.subject or not actor.has_scope('write')):
            raise AgentOSError(ErrorCode.DENIED, 'knowledge source owner required')
        if row['kb'] != self._config.kb_id:
            raise AgentOSError(ErrorCode.DENIED, 'knowledge source belongs to another KB binding')
        assert isinstance(row, sqlite3.Row)
        return row

    async def ingest(self, actor: AuthContext, source_id: str, text: str,
                     acl: list[str] | None = None) -> str:
        if actor.is_expired or not actor.has_scope('write') or not actor.has_scope('read'):
            raise AgentOSError(ErrorCode.DENIED, 'knowledge ingest requires write scope')
        version = hashlib.sha256(text.strip().encode()).hexdigest()
        allowed = sorted(set(acl or [actor.subject]))
        db = self._database()
        async with self._lock:
            prior = db.execute('SELECT * FROM sources WHERE tenant=? AND source=?',
                               (actor.tenant_id, source_id)).fetchone()
            if prior:
                if (prior['owner'] != actor.subject or prior['version'] != version
                        or json.loads(prior['acl']) != allowed):
                    raise AgentOSError(ErrorCode.CONFLICT, 'knowledge source binding is immutable')
                if prior['status'] == 'active':
                    return version
                raise AgentOSError(ErrorCode.UNCERTAIN, 'ingest delivery requires reconciliation; no blind replay')
            # Persist pending BEFORE the remote POST; upstream has no proven idempotency contract.
            db.execute('INSERT INTO sources VALUES(?,?,?,?,?,?,?,?)',
                (actor.tenant_id, source_id, actor.subject, json.dumps(allowed),
                 version, '', self._config.kb_id, 'pending'))
            db.commit()
            source_key = actor.tenant_id + '\0' + source_id + '\0' + version
            title = 'agentos-' + hashlib.sha256(source_key.encode()).hexdigest()
            try:
                created = await self._request('POST', '/api/v1/knowledge-bases/' + self._config.kb_id
                                             + '/knowledge/manual', json={'title': title, 'content': text,
                                                                          'status': 'publish'})
                remote = str(created.get('data', {}).get('id') or '')
                if not remote:
                    raise AgentOSError(ErrorCode.UNCERTAIN, 'knowledge ingest returned no receipt')
                db.execute('UPDATE sources SET remote=? WHERE tenant=? AND source=?',
                           (remote, actor.tenant_id, source_id))
                db.commit()
                for _ in range(360):
                    status = (await self._request('GET', '/api/v1/knowledge/' + remote)).get('data', {})
                    if status.get('knowledge_base_id') != self._config.kb_id:
                        raise AgentOSError(ErrorCode.DENIED, 'remote ingest KB binding mismatch')
                    if status.get('parse_status') == 'completed':
                        db.execute("UPDATE sources SET status='active' WHERE tenant=? AND source=?",
                                   (actor.tenant_id, source_id))
                        db.commit()
                        return version
                    if status.get('parse_status') in ('failed', 'error'):
                        raise AgentOSError(ErrorCode.UNAVAILABLE, 'remote knowledge parsing failed')
                    await asyncio.sleep(.5)
            except AgentOSError as exc:
                raise AgentOSError(ErrorCode.UNCERTAIN, 'knowledge ingest remains pending; reconcile first') from exc
            raise AgentOSError(ErrorCode.UNCERTAIN, 'knowledge parsing still pending')

    async def reconcile(self, actor: AuthContext, source_id: str) -> dict[str, Any]:
        if actor.is_expired or not actor.has_scope('write'):
            raise AgentOSError(ErrorCode.DENIED, 'knowledge reconciliation requires write scope')
        db = self._database()
        row = db.execute('SELECT * FROM sources WHERE tenant=? AND source=?',
                         (actor.tenant_id, source_id)).fetchone()
        if row is None or row['owner'] != actor.subject:
            raise AgentOSError(ErrorCode.DENIED, 'knowledge source owner required')
        if row['status'] == 'revoked':
            return {'source_id': source_id, 'status': 'revoked'}
        if not row['remote']:
            return {'source_id': source_id, 'status': 'uncertain', 'reason': 'remote receipt missing; no replay'}
        remote = (await self._request('GET', '/api/v1/knowledge/' + row['remote'])).get('data', {})
        if remote.get('knowledge_base_id') != row['kb']:
            raise AgentOSError(ErrorCode.DENIED, 'remote source KB binding differs')
        if remote.get('parse_status') == 'completed':
            db.execute("UPDATE sources SET status='active' WHERE tenant=? AND source=?",
                       (actor.tenant_id, source_id))
            db.commit()
            return {'source_id': source_id, 'status': 'active', 'source_version': row['version']}
        return {'source_id': source_id, 'status': 'pending', 'parse_status': remote.get('parse_status')}

    async def search(self, actor: AuthContext, query: str, limit: int = 5) -> list[KnowledgeHit]:
        if actor.is_expired or not actor.has_scope('read'):
            raise AgentOSError(ErrorCode.DENIED, 'knowledge read scope required')
        rows = self._database().execute("SELECT * FROM sources WHERE tenant=? AND status='active' AND kb=?",
                                       (actor.tenant_id, self._config.kb_id)).fetchall()
        allowed = {row['remote']: row for row in rows if actor.subject in json.loads(row['acl'])}
        if not allowed:
            raise AgentOSError(ErrorCode.INVALID, 'RETRIEVAL_NO_SOURCE')
        response = await self._request('POST', '/api/v1/knowledge-bases/' + self._config.kb_id + '/hybrid-search',
            json={'query_text': query, 'knowledge_ids': list(allowed), 'match_count': max(1, min(limit, 20)),
                  'skip_context_enrichment': True})
        hits = []
        for item in response.get('data', []):
            row = allowed.get(item.get('knowledge_id'))
            if row is None or item.get('knowledge_base_id') != self._config.kb_id:
                raise AgentOSError(ErrorCode.DENIED, 'remote search returned out-of-scope content')
            self._source(actor, row['source'])
            locator, content = str(item.get('id') or ''), str(item.get('content') or '')
            if not locator or not content:
                continue
            hits.append(KnowledgeHit(chunk_id=locator, text=content, source_version=row['version'],
                refs=[Ref(source_id=row['source'], source_version=row['version'],
                          locator=locator, quote=content[:300])]))
        if not hits:
            raise AgentOSError(ErrorCode.INVALID, 'RETRIEVAL_NO_SOURCE')
        return hits

    async def resolve_ref(self, actor: AuthContext, ref: Ref) -> dict[str, Any]:
        row = self._source(actor, ref.source_id)
        if row['version'] != ref.source_version:
            raise AgentOSError(ErrorCode.DENIED, 'source/version binding differs')
        remote = (await self._request('GET', '/api/v1/chunks/by-id/' + quote(ref.locator, safe=''))).get('data', {})
        parent = (await self._request('GET', '/api/v1/knowledge/' + row['remote'])).get('data', {})
        if (remote.get('knowledge_id') != row['remote']
                or parent.get('knowledge_base_id') != row['kb']
                or (remote.get('knowledge_base_id') and remote['knowledge_base_id'] != row['kb'])):
            raise AgentOSError(ErrorCode.DENIED, 'reference belongs to another document/KB')
        self._source(actor, ref.source_id)
        content = str(remote.get('content') or '')
        if ref.quote and ref.quote not in content:
            raise AgentOSError(ErrorCode.DENIED, 'reference quotation no longer matches')
        return {'source_id': ref.source_id, 'source_version': row['version'], 'locator': ref.locator,
                'text': content, 'backend': BACKEND_ID, 'backend_version': self.backend_version}

    async def delete(self, actor: AuthContext, source_id: str) -> None:
        row = self._source(actor, source_id, write=True)
        db = self._database()
        db.execute("UPDATE sources SET status='revoked' WHERE tenant=? AND source=?", (actor.tenant_id, source_id))
        if self._store:
            self._store.add_tombstone(actor.tenant_id, 'weknora:' + source_id)
        db.commit()  # Immediate local revocation, even when the remote async deletion is delayed.
        await self._request('DELETE', '/api/v1/knowledge/' + row['remote'])
        for _ in range(60):
            try:
                await self._request('GET', '/api/v1/knowledge/' + row['remote'])
            except AgentOSError as exc:
                if exc.code == ErrorCode.INVALID:
                    return
                raise
            await asyncio.sleep(.5)
        raise AgentOSError(ErrorCode.UNCERTAIN, 'remote deletion still pending; source already revoked locally')
