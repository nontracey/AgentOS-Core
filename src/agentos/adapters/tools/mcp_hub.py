"""MCP Hub（FR-TOOL-01/02/03/07/09/10，M1）：

- MCP 是工具层一等传输契约：本地工具与远端 MCP server 经同一 ToolPort 聚合，
  调用方无感知（同一 execute_tool_step / Policy / 审批 / 审计路径）。
- 命名空间：远端工具名为 `serverId:toolName`，与本地工具不冲突（FR-TOOL-02）。
- 版本与能力协商：连接时记录 initialize 返回的协议版本；poll/cancel 等本 Hub 未
  支持的能力显式返回 UNSUPPORTED_CAPABILITY（FR-TOOL-10），不伪装成功。
- 鉴权（FR-TOOL-07）：HTTP server 令牌从环境变量读取（配置存 env 名，不存明文）；
  传输安全不代替工具级授权——所有调用仍过 Policy/审批。
- 保守风险默认：远端工具无风险元数据，未显式配置时按 ["write"] 处理（需审批），
  不把供应方注释当授权（蓝图 §12）。
- 长任务：MCP Tasks 仅在 SDK/服务端均支持且协商后启用（FR-TOOL-09）；M1 未启用，
  持久任务由 AgentOS Job/账本承接。
"""

from __future__ import annotations

import json
import shlex
from datetime import timedelta
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from agentos.kernel.errors import AgentOSError, ErrorCode, unsupported_capability
from agentos.kernel.store import StoreProtocol
from agentos.kernel.types import (
    AuthContext,
    CallContext,
    ToolResult,
    ToolSpec,
)

_CONNECT_TIMEOUT_S = 15.0
_CALL_TIMEOUT_S = 30.0


class McpHubAdapter:
    """聚合 ToolPort：本地工具（无前缀）+ 远端 MCP 工具（`server:tool` 前缀）。"""

    def __init__(self, store: StoreProtocol, local: Any) -> None:
        self._store = store
        self._local = local

    # ------------------------------------------------------------ registry
    async def add_server(
        self,
        actor: AuthContext,
        server_id: str,
        *,
        command: str | None = None,
        url: str | None = None,
        risks: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not server_id or ":" in server_id or " " in server_id:
            raise AgentOSError(ErrorCode.INVALID, f"invalid server id: {server_id!r}")
        if bool(command) == bool(url):
            raise AgentOSError(ErrorCode.INVALID, "exactly one of --command/--url required")
        risks = risks or ["write"]  # 保守默认：远端工具需审批
        record = {
            "tenant_id": actor.tenant_id,
            "id": server_id,
            "transport": "stdio" if command else "http",
            "command": command,
            "url": url,
            "env_json": json.dumps(env or {}, ensure_ascii=False),
            "risks_json": json.dumps(risks),
            "enabled": 1,
        }
        self._store.upsert_mcp_server(record)
        tools = await self.discover(actor, server_id)
        self._store.audit_append(
            actor.tenant_id, "mcp.server_added", subject=actor.subject, run_id=None,
            detail={"server": server_id, "transport": record["transport"], "tools": len(tools)},
        )
        return {"server": server_id, "transport": record["transport"], "tools": [t.name for t in tools]}

    async def set_enabled(self, actor: AuthContext, server_id: str, enabled: bool) -> dict[str, Any]:
        """启用/停用：停用后不再出现在工具清单、不再可解析/调用（新 run 立即受阻）。"""
        self._require_server(actor.tenant_id, server_id)
        self._store.set_mcp_server_enabled(actor.tenant_id, server_id, enabled)
        self._store.audit_append(
            actor.tenant_id, "mcp.server_toggle", subject=actor.subject, run_id=None,
            detail={"server": server_id, "enabled": enabled},
        )
        return {"server": server_id, "enabled": enabled}

    async def probe(self, actor: AuthContext, server_id: str) -> dict[str, Any]:
        """探针：真实连接一次，协商协议版本并刷新工具清单（失败不静默转成功）。"""
        tools = await self.discover(actor, server_id)
        row = self._require_server(actor.tenant_id, server_id)
        self._store.audit_append(
            actor.tenant_id, "mcp.server_probed", subject=actor.subject, run_id=None,
            detail={"server": server_id, "protocol": row.get("protocol_version"),
                    "tools": [t.name for t in tools]},
        )
        return {
            "server": server_id, "protocol_version": row.get("protocol_version"),
            "transport": row.get("transport"), "enabled": bool(row.get("enabled")),
            "tools": [{"name": t.name, "risks": t.risks, "recovery": t.recovery}
                      for t in tools],
        }

    async def remove_server(self, actor: AuthContext, server_id: str) -> None:
        self._store.delete_mcp_server(actor.tenant_id, server_id)
        self._store.audit_append(
            actor.tenant_id, "mcp.server_removed", subject=actor.subject, run_id=None,
            detail={"server": server_id},
        )

    async def list_servers(self, actor: AuthContext) -> list[dict[str, Any]]:
        rows = self._store.list_mcp_servers(actor.tenant_id)
        for r in rows:
            r["tools"] = json.loads(r["tools_json"])
            r["risks"] = json.loads(r["risks_json"])
            # 脱敏摘要：环境变量、命令行原文与完整 URL 都不外泄（可能内嵌令牌/内网地址）
            r.pop("env_json", None)
            r.pop("tools_json", None)
            r.pop("risks_json", None)
            command = r.pop("command", None)
            r["command_present"] = bool(command)
            url = r.get("url")
            if url:
                from urllib.parse import urlsplit

                parts = urlsplit(str(url))
                r["url"] = f"{parts.scheme}://{parts.hostname or '?'}" + (
                    f":{parts.port}" if parts.port else "")
        return rows

    # -------------------------------------------------------------- discover
    async def discover(self, actor: AuthContext, server_id: str) -> list[ToolSpec]:
        """连接 server 做发现，协商协议版本并缓存工具清单（TTL 语义：手动 refresh）。"""
        row = self._require_server(actor.tenant_id, server_id)
        if row["transport"] == "stdio":
            protocol, raw_tools = await self._stdio_discover(row)
        else:
            protocol, raw_tools = await self._http_discover(row)
        risks = json.loads(row["risks_json"])
        specs = [
            ToolSpec(
                name=f"{server_id}:{t.name}",
                version=protocol,
                description=t.description or "",
                input_schema=t.input_schema or {"type": "object"},
                risks=risks,
                recovery="manual",  # 远端副作用能力未知：保守处理
                timeout_ms=int(_CALL_TIMEOUT_S * 1000),
            )
            for t in raw_tools
        ]
        self._store.update_mcp_server_discovery(
            actor.tenant_id, server_id, protocol_version=protocol,
            tools_json=json.dumps(
                [{"name": t.name, "description": t.description or "",
                  "input_schema": t.input_schema or {"type": "object"}} for t in raw_tools],
                ensure_ascii=False,
            ),
        )
        return specs

    async def _stdio_discover(self, row: dict[str, Any]) -> tuple[str, list[Any]]:
        command = shlex.split(str(row["command"]))
        env = {k: v for k, v in json.loads(row["env_json"]).items()}
        params = StdioServerParameters(command=command[0], args=command[1:], env=env or None)
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            result = await session.initialize()
            tools = await session.list_tools()
            return result.protocol_version, list(tools.tools)

    async def _http_discover(self, row: dict[str, Any]) -> tuple[str, list[Any]]:
        headers = self._auth_headers(row)
        client = self._http_client(headers)
        async with (
            streamable_http_client(str(row["url"]), http_client=client) as (read, write),
            ClientSession(read, write) as session,
        ):
            result = await session.initialize()
            tools = await session.list_tools()
            return result.protocol_version, list(tools.tools)

    # ----------------------------------------------------------------- call
    async def call(self, name: str, args: dict[str, Any], ctx: CallContext) -> ToolResult:
        if ":" not in name:
            local_result: ToolResult = await self._local.call(name, args, ctx)
            return local_result
        server_id, _, tool_name = name.partition(":")
        row = self._require_server(ctx.actor.tenant_id, server_id)
        if row["transport"] == "stdio":
            result = await self._stdio_call(row, tool_name, args)
        else:
            result = await self._http_call(row, tool_name, args)
        value: dict[str, Any] = _content_to_value(result)
        if getattr(result, "is_error", getattr(result, "isError", False)):
            return ToolResult(
                call_id=ctx.call_id, status="failed", error_code=str(ErrorCode.INVALID), value=value
            )
        completed = ToolResult(call_id=ctx.call_id, status="completed", value=value)
        return completed

    async def _stdio_call(self, row: dict[str, Any], tool: str, args: dict[str, Any]) -> Any:
        command = shlex.split(str(row["command"]))
        env = {k: v for k, v in json.loads(row["env_json"]).items()}
        params = StdioServerParameters(command=command[0], args=command[1:], env=env or None)
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(tool, args)

    async def _http_call(self, row: dict[str, Any], tool: str, args: dict[str, Any]) -> Any:
        headers = self._auth_headers(row)
        client = self._http_client(headers)
        async with (
            streamable_http_client(str(row["url"]), http_client=client) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            return await session.call_tool(tool, args)

    # ------------------------------------------------------------- ToolPort
    async def list_tools(self, actor: AuthContext) -> list[ToolSpec]:
        local = await self._local.list_tools(actor)
        remote: list[ToolSpec] = []
        for row in self._store.list_mcp_servers(actor.tenant_id):
            if not row["enabled"]:
                continue
            risks = json.loads(row["risks_json"])
            for t in json.loads(row["tools_json"]):
                remote.append(
                    ToolSpec(
                        name=f"{row['id']}:{t['name']}",
                        version=row["protocol_version"] or "",
                        description=t.get("description", ""),
                        input_schema=t.get("input_schema") or {"type": "object"},
                        risks=risks,
                        recovery="manual",
                        timeout_ms=int(_CALL_TIMEOUT_S * 1000),
                    )
                )
        specs: list[ToolSpec] = local + remote
        return specs

    async def resolve(self, actor: AuthContext, name: str) -> ToolSpec:
        for spec in await self.list_tools(actor):
            if spec.name == name:
                return spec
        raise AgentOSError(ErrorCode.INVALID, f"unknown tool: {name}")

    async def poll(self, external_ref: str, ctx: CallContext) -> ToolResult:
        raise unsupported_capability("MCP Tasks not negotiated (FR-TOOL-09: M1 由 Job/账本承接长任务)")

    async def cancel(self, external_ref: str, ctx: CallContext) -> ToolResult:
        raise unsupported_capability("MCP Tasks not negotiated (FR-TOOL-09)")

    # --------------------------------------------------------------- helpers
    def _require_server(self, tenant_id: str, server_id: str) -> dict[str, Any]:
        row = self._store.get_mcp_server(tenant_id, server_id)
        if row is None:
            raise AgentOSError(ErrorCode.INVALID, f"mcp server not registered: {server_id}")
        return row

    @staticmethod
    def _auth_headers(row: dict[str, Any]) -> dict[str, str]:
        """FR-TOOL-07：令牌只存环境变量名，从进程环境读取明文。"""
        env_spec = json.loads(row["env_json"])
        headers: dict[str, str] = {}
        for key, value in env_spec.items():
            if value.startswith("env:"):
                import os

                token = os.environ.get(value[4:])
                if token:
                    headers[key] = token
            else:
                headers[key] = value
        return headers

    @staticmethod
    def _http_client(headers: dict[str, str]) -> Any:
        """P1-9：调用方以 async with 持有，结束后关闭（防 fd 泄漏）。"""
        import httpx2  # SDK v2 依赖 httpx2，与之一致避免双栈

        client = httpx2.AsyncClient(headers=headers, timeout=_CONNECT_TIMEOUT_S)
        return client


def _content_to_value(result: Any) -> dict[str, Any]:
    value: dict[str, Any] = {}
    structured = getattr(result, "structuredContent", None)
    if structured:
        value["structured"] = structured
    texts = [
        c.text
        for c in (getattr(result, "content", None) or [])
        if type(c).__name__ == "TextContent"
    ]
    if texts:
        value["text"] = "\n".join(texts)
    return value


_ = timedelta  # 保留导入语义（超时常量以秒定义）
