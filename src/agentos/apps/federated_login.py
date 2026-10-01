"""Authlib authorization-code + PKCE; upstream tokens never enter browser storage."""
from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from agentos.adapters.identity.federation import FederatedIdentity
from agentos.kernel.errors import AgentOSError, ErrorCode

COOKIE = 'agentos_enterprise'


def mount(app: FastAPI, federation: FederatedIdentity, directory: Path) -> None:
    key_path = directory / 'oidc-session.key'
    if not key_path.exists():
        fd = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(secrets.token_urlsafe(48))
    secure = any(not lane.allow_http for lane in federation.lanes.values() if lane.kind == "oidc")
    app.add_middleware(SessionMiddleware, secret_key=key_path.read_text(), max_age=600,
                       session_cookie='agentos_oidc_state', same_site='lax', https_only=secure)
    oauth = OAuth()
    for lane in federation.lanes.values():
        if lane.kind == 'oidc' and lane.client_id:
            oauth.register(lane.id, client_id=lane.client_id,
                client_secret=os.environ.get(lane.client_secret_env, '') if lane.client_secret_env else None,
                server_metadata_url=lane.discovery_url or lane.issuer.rstrip('/') + '/.well-known/openid-configuration',
                client_kwargs={'scope': lane.login_scope, 'code_challenge_method': 'S256', 'trust_env': False})
    router = APIRouter()

    @router.get('/v1/auth/providers')
    async def providers() -> list[dict[str, str]]:
        return [{'id': lane.id, 'label': lane.id} for lane in federation.lanes.values()
                if lane.kind == 'oidc' and lane.client_id]

    @router.get('/v1/auth/oidc/{provider}/login')
    async def login(provider: str, request: Request) -> Any:
        lane = federation.lanes.get(provider)
        client = oauth.create_client(provider) if lane else None  # type: ignore[no-untyped-call]
        if client is None or lane is None:
            raise AgentOSError(ErrorCode.DENIED, 'identity provider not enabled for browser login')
        metadata = await client.load_server_metadata()
        if metadata.get('issuer') != lane.issuer:
            raise AgentOSError(ErrorCode.DENIED, 'OIDC discovery issuer mismatch')
        for name in ('authorization_endpoint', 'token_endpoint', 'jwks_uri'):
            uri = str(metadata.get(name) or '')
            if not uri.startswith('https://') and not (lane.allow_http and uri.startswith('http://')):
                raise AgentOSError(ErrorCode.DENIED, 'OIDC endpoint requires HTTPS')
        callback = request.url_for('enterprise_callback', provider=provider)
        return await client.authorize_redirect(request, str(callback), code_verifier=secrets.token_urlsafe(48))

    @router.get('/v1/auth/oidc/{provider}/callback', name='enterprise_callback')
    async def callback(provider: str, request: Request) -> RedirectResponse:
        lane = federation.lanes.get(provider)
        client = oauth.create_client(provider) if lane else None  # type: ignore[no-untyped-call]
        if client is None or lane is None:
            raise AgentOSError(ErrorCode.DENIED, 'identity provider disabled')
        try:
            token = await client.authorize_access_token(request, claims_options={'iss': {'essential': True,
                                                                                      'value': lane.issuer}})
            if not token.get('id_token') or not token.get('userinfo', {}).get('sub'):
                raise ValueError('OIDC identity required')
            credential = await federation.issue_browser_session(provider, str(token['access_token']))
            actor = await federation.authenticate({'token': credential})
            # ID token and API access token must describe the same stable issuer subject.
            import hashlib
            expected = 'external:' + hashlib.sha256(
                (lane.issuer + '\0' + token['userinfo']['sub']).encode()).hexdigest()
            if actor.subject != expected:
                federation.revoke(credential)
                raise ValueError('OIDC subject mismatch')
        except Exception as exc:
            raise AgentOSError(ErrorCode.DENIED, 'enterprise login failed') from exc
        request.session.clear()
        response = RedirectResponse('/ui/', status_code=303)
        response.set_cookie(COOKIE, credential, httponly=True, secure=secure, samesite='lax', max_age=300, path='/')
        response.headers['Cache-Control'] = 'no-store'
        return response

    @router.get('/v1/auth/browser-session')
    async def browser_session(request: Request) -> dict[str, Any]:
        actor = await federation.authenticate({'token': request.cookies.get(COOKIE, '')})
        return {'subject': actor.subject, 'tenant_id': actor.tenant_id,
                'role': 'admin' if 'admin' in actor.roles else 'member'}

    @router.post('/v1/auth/browser-logout')
    async def browser_logout(request: Request) -> RedirectResponse:
        federation.revoke(request.cookies.get(COOKIE, ''))
        response = RedirectResponse('/ui/', status_code=303)
        response.delete_cookie(COOKIE)
        return response

    app.include_router(router)

    @app.middleware('http')
    async def browser_identity(request: Request, call_next: Any) -> Any:
        credential = request.cookies.get(COOKIE)
        authorization = request.headers.get('authorization', '')
        if credential and authorization in ('', 'Bearer sso-cookie'):
            if request.method not in ('GET', 'HEAD', 'OPTIONS'):
                expected_origin = str(request.base_url).rstrip('/')
                if request.headers.get('origin') != expected_origin:
                    return JSONResponse({'code': 'denied', 'message': 'same-origin request required'},
                                        status_code=403)
            headers = [(k, v) for k, v in request.scope['headers'] if k != b'authorization']
            request.scope['headers'] = [*headers, (b'authorization', ('Bearer ' + credential).encode())]
        return await call_next(request)
