"""MCP assistants propose; authenticated administrators review and apply offline.

No process, network discovery, overwrite or permission grant occurs on proposal.
Credentials are environment references only, never copied into review drafts.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
from typing import Any
from urllib.parse import urlsplit

from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import AuthContext
from agentos.services.drafts import DraftService


class ConnectionService:
    def __init__(self, drafts: DraftService) -> None:
        self.drafts = drafts

    @staticmethod
    def validate(config: dict[str, Any]) -> dict[str, Any]:
        if set(config) - {"server_id", "command", "url", "env", "risks", "kind"}:
            raise AgentOSError(ErrorCode.INVALID, "unknown connection fields")
        id = config.get("server_id")
        if not isinstance(id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", id) or id == "node":
            raise AgentOSError(ErrorCode.INVALID, "invalid connection identifier")
        command, url = config.get("command"), config.get("url")
        if bool(command) == bool(url):
            raise AgentOSError(ErrorCode.INVALID, "provide exactly one command or URL")
        if command and (not isinstance(command, list) or not command
                or not all(isinstance(v, str) and v and len(v) <= 1000 for v in command)):
            raise AgentOSError(ErrorCode.INVALID, "command must be a nonempty argument list")
        if url:
            if not isinstance(url, str):
                raise AgentOSError(ErrorCode.INVALID, "invalid connection URL")
            try:
                parts = urlsplit(url)
                if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username
                        or parts.password or parts.query or parts.fragment):
                    raise ValueError
                _ = parts.port
            except ValueError as exc:
                raise AgentOSError(ErrorCode.INVALID, "URL must not embed credentials or query parameters") from exc
        env = config.get("env", {})
        if (not isinstance(env, dict) or len(env) > 32
                or not all(isinstance(k, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", k)
                           and isinstance(v, str) and re.fullmatch(r"env:[A-Za-z_][A-Za-z0-9_]{0,127}", v)
                           for k, v in env.items())):
            raise AgentOSError(ErrorCode.INVALID, "credentials must use env:VARIABLE references")
        risks = config.get("risks") or ["write", "egress"]
        if not isinstance(risks, list) or not all(r in {"read", "write", "delete", "exec", "egress"} for r in risks):
            raise AgentOSError(ErrorCode.INVALID, "invalid connection risks")
        kind = config.get("kind", "tool")
        if kind not in {"tool", "agent"}:
            raise AgentOSError(ErrorCode.INVALID, "connection kind must be tool or agent")
        return {"server_id": id, "command": command, "url": url, "env": env,
                "risks": sorted(set(risks)), "kind": kind}

    @staticmethod
    def digest(config: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def propose(self, actor: AuthContext, config: dict[str, Any]) -> dict[str, Any]:
        config = self.validate(config)
        digest = self.digest(config)
        payload = {"config": config, "hash": digest}
        id = "connection-" + hashlib.sha256((actor.subject + "\0" + digest).encode()).hexdigest()[:24]
        try:
            saved = self.drafts.save(actor, "mcp", id, payload, 0)
        except AgentOSError as exc:
            if exc.code != ErrorCode.CONFLICT:
                raise
            existing = self.drafts.get(actor, "mcp", id)
            if existing["payload"] != payload:
                raise AgentOSError(ErrorCode.CONFLICT, "connection proposal differs from existing draft") from exc
            saved = {"id": id, "revision": existing["revision"]}
        self.drafts.store.audit_append(actor.tenant_id, "mcp.connection_proposed", subject=actor.subject,
            run_id=None, detail={"proposal_id": id, "hash": payload["hash"]})
        server = self.drafts.store.get_mcp_server(actor.tenant_id, config["server_id"])
        applied = bool(server and server.get("proposal_id") == id)
        return {**saved, **payload, "applied": applied, "requires_admin_review": not applied}

    def apply(self, actor: AuthContext, id: str, reviewed_hash: str) -> dict[str, Any]:
        self.drafts.access.require_admin(actor, "mcp.register")
        if not actor.has_scope("write"):
            raise AgentOSError(ErrorCode.DENIED, "connection apply requires write scope")
        proposal = next((r for r in self.drafts.store.list_connection_drafts(actor.tenant_id) if r["id"] == id), None)
        if not proposal:
            raise AgentOSError(ErrorCode.INVALID, "connection proposal not found")
        config = self.validate(json.loads(proposal["payload_json"])["config"])
        if reviewed_hash != self.digest(config):
            raise AgentOSError(ErrorCode.CONFLICT, "reviewed configuration hash differs")
        # Atomic create-only registry insertion makes receipt-loss retries safe.
        store = self.drafts.store
        record = {"tenant_id": actor.tenant_id, "id": config["server_id"],
                  "transport": "stdio" if config["command"] else "http",
                  "command": shlex.join(config["command"]) if config["command"] else None,
                  "url": config["url"], "env_json": json.dumps(config["env"]),
                  "risks_json": json.dumps(config["risks"]), "enabled": 0,
                  "kind": config["kind"], "proposal_id": id}
        if not store.insert_mcp_proposal(record):
            row = store.get_mcp_server(actor.tenant_id, config["server_id"])
            if not row or row.get("proposal_id") != id:
                raise AgentOSError(ErrorCode.CONFLICT, "connection exists; no existing configuration was overwritten")
            if any(row.get(k) != record[k] for k in ("transport", "command", "url", "env_json", "risks_json", "kind")):
                raise AgentOSError(ErrorCode.CONFLICT, "connection changed after proposal application")
        store.audit_append(actor.tenant_id, "mcp.connection_applied", subject=actor.subject,
            run_id=None, detail={"proposal_id": id, "hash": reviewed_hash, "enabled": False})
        current = store.get_mcp_server(actor.tenant_id, config["server_id"])
        return {"server": config["server_id"], "applied": True, "enabled": bool(current and current["enabled"]),
                "next": "Probe and enable after reviewing discovered tools and risks."}
