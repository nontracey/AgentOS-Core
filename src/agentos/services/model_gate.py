"""Run-bound admission for every engine model request, including compaction.

This is a governance adapter, not a model protocol implementation. Wire payloads
are forwarded unchanged except the server's output bound. Company/LiteLLM/native
Responses endpoints can be selected without changing the execution engine.
"""
from __future__ import annotations

import asyncio
import json
import math
import secrets
import time
from dataclasses import dataclass
from typing import Any

from agentos.kernel.agent_runtime import RuntimeRequest
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.states import RunState
from agentos.runtime.executor import RuntimeDeps, _refresh_actor


@dataclass
class ModelAdmission:
    tenant: str
    run_id: str
    call_id: str
    upper_tokens: int


class ModelGate:
    def __init__(self, deps: RuntimeDeps, *, gateway_url: str, upstream_url: str, api_key: str,
                 model: str, egress: str, token_budget: int, input_rate: int, output_rate: int,
                 max_output: int = 4096) -> None:
        self.deps, self.gateway_url, self.upstream_url = deps, gateway_url, upstream_url
        self.api_key, self.model, self.egress = api_key, model, egress
        self.token_budget, self.input_rate, self.output_rate = token_budget, input_rate, output_rate
        self.max_output = max_output
        self.bindings: dict[str, tuple[str, str, float, set[str]]] = {}
        self.inflight: dict[str, ModelAdmission] = {}
        self.lock = asyncio.Lock()

    async def grant(self, request: RuntimeRequest) -> tuple[str, str]:
        run = self.deps.store.get_run(request.tenant_id, request.run_id)
        if not run:
            raise AgentOSError(ErrorCode.DENIED, 'model session requires owned run')
        await _refresh_actor(self.deps, run)
        token = secrets.token_urlsafe(32)
        names = {'agentos_' + __import__('re').sub(r'[^a-zA-Z0-9_]', '_', t.name) for t in request.tools}
        now = time.monotonic()
        self.bindings = {k: v for k, v in self.bindings.items() if v[2] > now}
        self.bindings[token] = (run.tenant_id, run.id, now + 3600, names)
        return self.gateway_url.rstrip('/') + '/internal/model/v1', token

    def revoke(self, token: str) -> None:
        self.bindings.pop(token, None)

    async def admit(self, token: str, body: dict[str, Any]) -> ModelAdmission:
        binding = self.bindings.get(token)
        if not binding:
            raise AgentOSError(ErrorCode.DENIED, 'unknown model session')
        tenant, run_id, expiry, names = binding
        if expiry <= time.monotonic():
            self.revoke(token)
            raise AgentOSError(ErrorCode.DENIED, 'model session expired')
        run = self.deps.store.get_run(tenant, run_id)
        if not run or run.state != RunState.RUNNING:
            raise AgentOSError(ErrorCode.DENIED, 'model session no longer executable')
        await _refresh_actor(self.deps, run)
        data_class = json.loads(run.envelope_json).get('payload', {}).get('data_class', 'approved_private')
        if data_class == 'local_only' and self.egress != 'local':
            raise AgentOSError(ErrorCode.DENIED, 'local_only data cannot leave configured local provider')
        if body.get('model') != self.model:
            raise AgentOSError(ErrorCode.DENIED, 'unapproved model')
        tools = body.get('tools') or []
        if not isinstance(tools, list):
            raise AgentOSError(ErrorCode.INVALID, 'invalid model tools')
        for tool in tools:
            if not isinstance(tool, dict) or tool.get('type') != 'function' or tool.get('name', '') not in names:
                raise AgentOSError(ErrorCode.DENIED, 'native or unregistered runtime tool surface')
        try:
            output_bound = int(body.get('max_output_tokens') or self.max_output)
        except (TypeError, ValueError) as exc:
            raise AgentOSError(ErrorCode.INVALID, 'invalid model output bound') from exc
        if output_bound < 1:
            raise AgentOSError(ErrorCode.INVALID, 'invalid model output bound')
        body['max_output_tokens'] = min(output_bound, self.max_output)
        # UTF-8 bytes provide a conservative bound for input; include the serialized tools/instructions.
        upper_input = len(json.dumps(body, ensure_ascii=False).encode()) + 512
        upper_tokens = upper_input + body['max_output_tokens']
        async with self.lock:
            used = 0
            cursor = 0
            while batch := self.deps.store.event_list(tenant, run_id, cursor, limit=500):
                for event in batch:
                    if event['kind'] == 'model.usage':
                        value = event.get('payload', {})
                        if isinstance(value, str):
                            value = json.loads(value)
                        used += int(value.get('input_tokens', 0)) + int(value.get('output_tokens', 0))
                cursor = batch[-1]['seq']
            reserved = sum(a.upper_tokens for a in self.inflight.values() if a.run_id == run_id and a.tenant == tenant)
            if used + reserved + upper_tokens > self.token_budget:
                raise AgentOSError(ErrorCode.BUDGET_EXCEEDED, 'model token budget exhausted')
            call_id = run_id + ':engine-model:' + secrets.token_hex(12)
            cost = math.ceil((upper_input * self.input_rate + body['max_output_tokens'] * self.output_rate) / 1000)
            self.deps.budget.reserve(tenant, run_id, call_id, cost)
            admission = ModelAdmission(tenant, run_id, call_id, upper_tokens)
            self.inflight[call_id] = admission
        self.deps.store.event_append(tenant, run_id, 'model.request', {'call_id': call_id,
            'engine': 'codex', 'model': self.model, 'egress': self.egress, 'upper_tokens': upper_tokens})
        return admission

    def settle(self, admission: ModelAdmission, usage: dict[str, Any] | None) -> None:
        # Unknown delivery/usage retains conservative charges; do not pretend a failed stream was free.
        input_tokens = int(usage.get('input_tokens', 0)) if usage else admission.upper_tokens
        output_tokens = int(usage.get('output_tokens', 0)) if usage else 0
        actual = math.ceil((input_tokens * self.input_rate + output_tokens * self.output_rate) / 1000)
        self.deps.budget.settle(admission.tenant, admission.call_id, actual)
        self.deps.store.event_append(admission.tenant, admission.run_id, 'model.usage', {
            'call_id': admission.call_id, 'input_tokens': input_tokens, 'output_tokens': output_tokens,
            'estimated': usage is None, 'model': self.model})
        self.inflight.pop(admission.call_id, None)
