"""节点注册与配对（FR-NODE：配对/撤销/心跳/能力快照）。

安全语义：
- pair 仅 owner 可操作；生成一次性明文节点令牌（只在该次响应返回），
  库存 sha256；节点撤销后旧令牌立即失效（token_hash 校验 + status 门）。
- heartbeat 用节点自身令牌认证（Authorization: Bearer <node_token>）；
  撤销/未配对节点的心跳一律拒绝并审计。
- 能力快照（os/arch/version/capabilities）由节点心跳上报，调度前按此筛选，
  不凭静态标签假定可运行。
"""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any

from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.store import StoreProtocol
from agentos.kernel.types import AuthContext
from agentos.services.audit import AuditService


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class NodeRegistryService:
    """节点生命周期：pair → heartbeat(能力快照) → revoke。"""

    def __init__(self, store: StoreProtocol, audit: AuditService) -> None:
        self._store = store
        self._audit = audit

    def pair(
        self, actor: AuthContext, node_id: str, endpoint: str,
        *, os: str = "", arch: str = "", token: str | None = None,
    ) -> dict[str, Any]:
        if not node_id or not endpoint:
            raise AgentOSError(ErrorCode.INVALID, "node_id and endpoint are required")
        node_token = token or f"node_{secrets.token_urlsafe(24)}"
        self._store.pair_node(
            actor.tenant_id, node_id, endpoint, token_hash(node_token), node_token,
            os=os, arch=arch,
        )
        self._audit.record(
            actor.tenant_id, "node.paired", subject=actor.subject,
            detail={"node_id": node_id, "endpoint": endpoint},
        )
        return {
            "node_id": node_id, "endpoint": endpoint,
            "node_token": node_token,  # 明文只此一次；节点侧保存用于心跳与执行回执
        }

    def verify_node_token(self, tenant_id: str, node_id: str, token: str) -> dict[str, Any]:
        row = self._store.get_node(tenant_id, node_id)
        if row is None or row["status"] != "paired":
            raise AgentOSError(ErrorCode.DENIED, f"node not paired: {node_id}")
        import hmac as _hmac

        if not _hmac.compare_digest(token_hash(token), str(row["token_hash"])):
            raise AgentOSError(ErrorCode.DENIED, "invalid node token")
        return row

    def heartbeat(
        self, tenant_id: str, node_id: str, token: str,
        *, os: str, arch: str, capabilities: list[dict[str, Any]], version: str = "",
    ) -> dict[str, Any]:
        self.verify_node_token(tenant_id, node_id, token)
        ok = self._store.node_heartbeat(
            tenant_id, node_id, os=os, arch=arch,
            capabilities_json=json.dumps(capabilities, ensure_ascii=False),
            version=version,
        )
        row = self._store.get_node(tenant_id, node_id)
        if not ok or row is None:
            raise AgentOSError(ErrorCode.DENIED, f"node not paired: {node_id}")
        return {"node_id": node_id, "status": "paired", "last_heartbeat": row["last_heartbeat"]}

    def revoke(self, actor: AuthContext, node_id: str) -> dict[str, Any]:
        ok = self._store.revoke_node(actor.tenant_id, node_id)
        if not ok:
            raise AgentOSError(ErrorCode.INVALID, f"node not found or already revoked: {node_id}")
        self._audit.record(
            actor.tenant_id, "node.revoked", subject=actor.subject,
            detail={"node_id": node_id},
        )
        return {"node_id": node_id, "status": "revoked"}

    def list(self, actor: AuthContext) -> list[dict[str, Any]]:
        """列表不含令牌材料。"""
        return [
            {k: v for k, v in row.items() if not k.startswith("token")}
            for row in self._store.list_nodes(actor.tenant_id)
        ]
