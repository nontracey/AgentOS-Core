"""Deployment backend selection; accepted runs retain an immutable backend binding."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.store import StoreProtocol
from agentos.kernel.types import AuthContext, KnowledgeHit, Ref


class KnowledgeBackends:
    def __init__(self, store: StoreProtocol, backends: dict[str, Any], default: str, path: Path) -> None:
        self._store, self._backends, self._default, self._path = store, backends, default, path
        self._lock = threading.Lock()
        self._selected: dict[str, str] = json.loads(path.read_text()) if path.exists() else {}

    def current(self, actor: AuthContext | None = None) -> Any:
        tenant = actor.tenant_id if actor else "default"
        key = self._selected.get(tenant, self._default)
        backend = self._backends.get(key)
        if backend is None:
            raise AgentOSError(ErrorCode.UNAVAILABLE, "selected knowledge backend is not configured")
        return backend

    @property
    def backend_id(self) -> str:
        return str(self.current().backend_id)

    @property
    def backend_version(self) -> str:
        return str(self.current().backend_version)

    @property
    def sync_service(self) -> Any:
        # Existing builtin references continue to resolve even when new work switches backend.
        return self._backends["builtin-sqlite"].sync_service

    def capabilities(self, actor: AuthContext | None = None) -> dict[str, Any]:
        return {**self.current(actor).capabilities(), "choices": [
            {"backend": key, "version": backend.backend_version} for key, backend in self._backends.items()
        ]}

    def binding(self, actor: AuthContext) -> dict[str, str]:
        backend = self.current(actor)
        return {"backend": backend.backend_id, "version": backend.backend_version}

    def for_run(self, actor: AuthContext, run_id: str) -> Any:
        run = self._store.get_run(actor.tenant_id, run_id)
        if run is None:
            raise AgentOSError(ErrorCode.INVALID, "knowledge run missing")
        binding = json.loads(run.envelope_json).get("payload", {}).get("knowledge_binding")
        # Legacy pre-integration runs used the builtin backend; changing deployment defaults must not rebind them.
        key = binding["backend"] if binding else "builtin-sqlite"
        backend = self._backends.get(key)
        if backend is None or (binding and backend.backend_version != binding["version"]):
            raise AgentOSError(ErrorCode.UNAVAILABLE, "the task's pinned knowledge backend is unavailable")
        return backend

    def for_binding(self, binding: dict[str, str]) -> Any:
        backend = self._backends.get(binding.get("backend", ""))
        if backend is None or backend.backend_version != binding.get("version"):
            raise AgentOSError(ErrorCode.UNAVAILABLE, "pinned knowledge backend is unavailable")
        return backend

    async def select(self, actor: AuthContext, key: str) -> dict[str, Any]:
        backend = self._backends.get(key)
        if backend is None:
            raise AgentOSError(ErrorCode.INVALID, "backend is not configured")
        if hasattr(backend, "probe"):
            await backend.probe()
        with self._lock:
            selected = {**self._selected, actor.tenant_id: key}
            temporary = self._path.with_suffix(".tmp")
            temporary.write_text(json.dumps(selected), encoding="utf-8")
            os.replace(temporary, self._path)
            self._selected = selected
        self._store.audit_append(actor.tenant_id, "knowledge.backend_selected", subject=actor.subject,
                                 run_id=None, detail={"backend": key, "version": backend.backend_version})
        return self.capabilities(actor)

    async def reconcile(self, actor: AuthContext, source_id: str) -> dict[str, Any]:
        backend = self.current(actor)
        if not hasattr(backend, "reconcile"):
            raise AgentOSError(ErrorCode.UNSUPPORTED, "backend has no pending remote ingestion")
        result: dict[str, Any] = await backend.reconcile(actor, source_id)
        return result

    async def ingest(self, actor: AuthContext, source_id: str, text: str, acl: list[str] | None = None) -> str:
        result: str = await self.current(actor).ingest(actor, source_id, text, acl)
        return result

    async def search(self, actor: AuthContext, query: str, limit: int = 5) -> list[KnowledgeHit]:
        result: list[KnowledgeHit] = await self.current(actor).search(actor, query, limit)
        return result

    async def resolve_ref(self, actor: AuthContext, ref: Ref) -> dict[str, Any]:
        result: dict[str, Any] = await self.current(actor).resolve_ref(actor, ref)
        return result

    async def delete(self, actor: AuthContext, source_id: str) -> None:
        await self.current(actor).delete(actor, source_id)
