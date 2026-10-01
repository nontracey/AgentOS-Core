"""Node ToolHub：把三机节点以 `node:<node_id>:<tool>` 命名空间接入 ToolPort。

- list_tools：内层（local+MCP）+ 各 paired 节点缓存的能力快照；
  refresh(actor) 实时拉取节点 /tools 更新缓存（调度前探针用）。
- call：解析 node: 前缀 → 未配对/已撤销拒绝并审计 → HTTP POST {endpoint}/execute
  （Bearer 节点令牌 + 派发参数含 fence/deadline/幂等键）。
  错误分类：连接失败=UNAVAILABLE（无副作用证据）；超时=TIMEOUT（效果未知，
  reconciler 转 uncertain 对账，禁止盲重试）——与 FR-ORC-08 一致。
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import re
from typing import Any, cast

import httpx

from agentos.adapters.tools.mcp_hub import McpHubAdapter
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.ports import ToolPort
from agentos.kernel.types import AuthContext, CallContext, ToolResult, ToolSpec, utc_now
from agentos.services.assets import AssetService
from agentos.services.audit import AuditService

_NAMESPACE = "node:"
_CALL_TIMEOUT_S = 2400.0
# OpenAI function-calling 工具名规范为 [A-Za-z0-9_-]；Ollama/qwen 模板会把
# 冒号等"非法字符"剥掉导致模型输出的名字错位——对外一律用合规名，内部反查。
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]")


def sanitize_tool_name(name: str) -> str:
    return _SAFE_NAME.sub("_", name)


class NodeHubAdapter:
    """装饰器式聚合：node: 前缀自己处理，其余透传内层 ToolPort。"""

    def __init__(
        self, inner: ToolPort | McpHubAdapter, store: Any, audit: AuditService,
        *, call_timeout_s: float = _CALL_TIMEOUT_S,
        assets: AssetService | None = None,
    ) -> None:
        self._inner = inner
        self._store = store
        self._audit = audit
        self._timeout = call_timeout_s
        self._assets = assets
        self._name_map: dict[str, tuple[str, str]] = {}  # 合规名 -> (node_id, 真实工具名)

    # ------------------------------------------------------------- 能力发现
    async def refresh_node_tools(self, actor: AuthContext, node_id: str) -> list[ToolSpec]:
        """实时探针节点 /tools 并更新能力缓存；返回该节点 ToolSpec。"""
        row = self._store.get_node(actor.tenant_id, node_id)
        if row is None or row["status"] != "paired":
            raise AgentOSError(ErrorCode.DENIED, f"node not paired: {node_id}")
        url = str(row["endpoint"]).rstrip("/") + "/tools"
        try:
            # trust_env=False：节点走私网直连，禁用本机/系统代理（Clash 等）
            resp = await asyncio.to_thread(
                httpx.get, url,
                headers={"Authorization": f"Bearer {row['token_plain']}"},
                timeout=30.0, trust_env=False,
            )
        except httpx.HTTPError as exc:
            raise AgentOSError(ErrorCode.UNAVAILABLE, f"node unreachable: {node_id}: {exc}") from exc
        if resp.status_code == 401:
            raise AgentOSError(ErrorCode.DENIED, f"node token rejected: {node_id}")
        if resp.status_code != 200:
            raise AgentOSError(
                ErrorCode.UNAVAILABLE, f"node /tools failed: HTTP {resp.status_code}"
            )
        tools = resp.json()
        self._store.node_heartbeat(
            actor.tenant_id, node_id, os=str(row["os"]), arch=str(row["arch"]),
            capabilities_json=json.dumps(tools, ensure_ascii=False),
            version=str(row.get("version") or ""),
        )
        specs: list[ToolSpec] = []
        for t in tools:
            spec = self._spec_from_capability(node_id, t)
            # 合规名 -> (node_id, 节点侧真实工具名)；call 时反查派发
            self._name_map[spec.name] = (node_id, str(t.get("name") or ""))
            specs.append(spec)
        return specs

    def _spec_from_capability(self, node_id: str, cap: dict[str, Any]) -> ToolSpec:
        name = str(cap.get("name") or "")
        return ToolSpec(
            name=sanitize_tool_name(f"{_NAMESPACE}{node_id}:{name}"),
            version=str(cap.get("version") or ""),
            description=f"[node {node_id}] {cap.get('description') or ''}",
            input_schema=cap.get("input_schema") or {"type": "object"},
            risks=list(cap.get("risks") or ["read"]),
            recovery="manual",  # 远程副作用：默认保守，需人工/对账兜底
            timeout_ms=int(self._timeout * 1000),
        )

    # ------------------------------------------------------------- ToolPort
    async def list_tools(self, actor: AuthContext) -> list[ToolSpec]:
        specs = await self._inner.list_tools(actor)
        for row in self._store.list_nodes(actor.tenant_id):
            if row["status"] != "paired":
                continue
            node_id = str(row["node_id"])
            # 实时刷新能力快照（派发前能力确认）；节点断线退回上次缓存——
            # 模型永远看到最新能力，且不会因单节点失联丢掉全部工具。
            try:
                specs.extend(await self.refresh_node_tools(actor, node_id))
            except AgentOSError:
                for cap in json.loads(row["capabilities_json"] or "[]"):
                    spec = self._spec_from_capability(node_id, cap)
                    # 缓存回退同样必须回填合规名映射：否则断线期间派发查不到
                    # (node_id, tool_name)，合规名落进内层报 unknown tool，
                    # "断线退回缓存"形同虚设（F1 断连演练发现的真 bug）。
                    self._name_map[spec.name] = (node_id, str(cap.get("name") or ""))
                    specs.append(spec)
        return specs

    async def resolve(self, actor: AuthContext, name: str) -> ToolSpec:
        for spec in await self.list_tools(actor):
            if spec.name == name:
                return spec
        # 快照未命中：按合规名反查节点并实时探针（派发前能力确认），再试一次
        mapped = self._name_map.get(name)
        if mapped is not None:
            node_id = mapped[0]
            try:
                refreshed = await self.refresh_node_tools(actor, node_id)
            except AgentOSError:
                refreshed = []
            for spec in refreshed:
                if spec.name == name:
                    return spec
        raise AgentOSError(ErrorCode.INVALID, f"unknown tool: {name}")

    async def _ensure_name_map(self, actor: AuthContext) -> None:
        """惰性构建合规名映射（节点 pair 后未 refresh 心跳的场景）。"""
        if self._name_map:
            return
        for row in self._store.list_nodes(actor.tenant_id):
            if row["status"] != "paired":
                continue
            try:
                await self.refresh_node_tools(actor, str(row["node_id"]))
            except AgentOSError:
                continue

    async def underlying_recovery(self, actor: AuthContext, name: str) -> str | None:
        """节点能力快照中底层工具自声明的 recovery（对账重放安全性判据）。

        gateway 侧节点 spec 一律 recovery="manual"（防节点谎报的保守默认，
        见 _spec_from_capability）；但对账重放是否安全取决于底层工具真实契约：
        仅 pure / idempotency_key（同幂等键外部去重保证效果恰好一次）返回原值，
        其余返回 None——由调用方决定是否拒绝重放。
        """
        if name.startswith(_NAMESPACE):
            node_id, _, tool_name = name.removeprefix(_NAMESPACE).partition(":")
        else:
            await self._ensure_name_map(actor)
            mapped = self._name_map.get(name)
            if mapped is None:
                return None
            node_id, tool_name = mapped
        row = self._store.get_node(actor.tenant_id, node_id)
        if row is None:
            return None
        try:
            caps = json.loads(row["capabilities_json"] or "[]")
        except json.JSONDecodeError:
            return None
        for cap in caps:
            if str(cap.get("name") or "") == tool_name:
                rec = str(cap.get("recovery") or "")
                return rec if rec in {"pure", "idempotency_key"} else None
        return None

    async def call(self, name: str, args: dict[str, Any], ctx: CallContext) -> ToolResult:
        if not name.startswith(_NAMESPACE):
            # 模型输出的是合规名（冒号被 sanitize 成 _）：反查真实 (node_id, tool_name)
            mapped = self._name_map.get(name)
            if mapped is None and name.startswith("node_"):
                await self._ensure_name_map(ctx.actor)
                mapped = self._name_map.get(name)
            if mapped is None:
                return await self._inner.call(name, args, ctx)
            node_id, tool_name = mapped
        else:
            rest = name.removeprefix(_NAMESPACE)
            node_id, _, tool_name = rest.partition(":")
        if not node_id or not tool_name:
            raise AgentOSError(ErrorCode.INVALID, f"malformed node tool name: {name}")
        row = self._store.get_node(ctx.actor.tenant_id, node_id)
        if row is None or row["status"] != "paired":
            self._audit.record(
                ctx.actor.tenant_id, "node.dispatch_denied", subject=ctx.actor.subject,
                detail={"node_id": node_id, "reason": "not_paired", "tool": tool_name},
            )
            raise AgentOSError(ErrorCode.DENIED, f"node not paired: {node_id}")

        # ToolPort also permits direct adapter calls without a persisted run.
        # Execution evidence must not create an orphan event or block such calls.
        if self._store.get_run(ctx.actor.tenant_id, ctx.run_id) is not None:
            self._store.event_append(ctx.actor.tenant_id, ctx.run_id, "node.dispatched", {
                "step_id": ctx.step_id, "tool": tool_name, "node_id": node_id,
                "os": str(row["os"]), "basis": "已配对节点的工具名称映射；使用节点能力快照",
            })
        body = {
            "tool_name": tool_name,
            "args": args,
            "call_id": ctx.call_id,
            "step_id": ctx.step_id,
            "run_id": ctx.run_id,
            "idempotency_key": ctx.idempotency_key,
            "fence": ctx.fence,
        }
        # 相对时长避免两机时钟差；与剩余 deadline 对齐（长任务如 ComfyUI 冷启动
        # +生成可能 >120s），上限 900s。
        body["deadline_seconds"] = max(5.0, min(
            (ctx.deadline_at.timestamp() - utc_now().timestamp())
            if ctx.deadline_at else self._timeout, 900.0))
        url = str(row["endpoint"]).rstrip("/") + "/execute"
        # 长任务（如 ComfyUI 冷启动+生成）按 ctx.deadline 剩余时间作为 HTTP 超时，
        # 上限 900s；剩余不足时至少给 5s 完成请求。
        http_timeout = self._timeout
        if ctx.deadline_at:
            remaining = (ctx.deadline_at.timestamp() - utc_now().timestamp())
            http_timeout = max(5.0, min(remaining, 2400.0))
        try:
            # 同步 httpx 放线程池执行：不得阻塞事件循环（否则整个 gateway 冻结）
            resp = await asyncio.to_thread(
                httpx.post,
                url, json=body,
                headers={"Authorization": f"Bearer {row['token_plain']}"},
                timeout=http_timeout, trust_env=False,
            )
        except httpx.TimeoutException as exc:
            raise AgentOSError(
                ErrorCode.TIMEOUT, f"node call timeout: {node_id}:{tool_name}"
            ) from exc  # side_effect_unknown=True → reconciler 转 uncertain
        except httpx.HTTPError as exc:
            raise AgentOSError(
                ErrorCode.UNAVAILABLE, f"node unreachable: {node_id}: {exc}"
            ) from exc
        if resp.status_code == 401:
            self._audit.record(
                ctx.actor.tenant_id, "node.dispatch_denied", subject=ctx.actor.subject,
                detail={"node_id": node_id, "reason": "token_rejected", "tool": tool_name},
            )
            raise AgentOSError(ErrorCode.DENIED, f"node token rejected: {node_id}")
        if resp.status_code != 200:
            raise AgentOSError(
                ErrorCode.UNAVAILABLE, f"node execute failed: HTTP {resp.status_code}"
            )
        payload = resp.json()
        value = payload.get("value", {})
        if (self._assets is not None and payload.get("status") == "completed"
                and isinstance(value, dict) and value.get("asset_ref") and value.get("sha256")
                and self._store.get_run(ctx.actor.tenant_id, ctx.run_id) is not None):
            try:
                asset = await asyncio.to_thread(
                    self._assets.fetch_from_node, ctx.actor,
                    node_endpoint=str(row["endpoint"]), node_token=str(row["token_plain"]),
                    remote_ref=str(value["asset_ref"]), expect_sha256=str(value["sha256"]),
                    mime=mimetypes.guess_type(str(value["asset_ref"]))[0] or "application/octet-stream",
                    run_id=ctx.run_id, source_node=node_id,
                )
                value = {**value, "asset_id": asset["asset_id"], "delivery": "registered"}
                self._store.event_append(ctx.actor.tenant_id, ctx.run_id, "asset.delivered", {
                    "step_id": ctx.step_id, "asset_id": asset["asset_id"], "node_id": node_id,
                    "sha256": asset["sha256"], "size": asset["size"], "mime": asset["mime"],
                })
            except AgentOSError as exc:
                # 已确认生成成功：交付失败不重跑 GPU 副作用；保留引用供单独补传。
                value = {**value, "delivery": "pending", "delivery_error": exc.code.value}
                self._store.event_append(ctx.actor.tenant_id, ctx.run_id, "asset.delivery_failed", {
                    "step_id": ctx.step_id, "node_id": node_id, "code": exc.code.value,
                })
        return ToolResult(
            call_id=ctx.call_id,
            status=payload.get("status", "failed"),
            value=value,
            external_ref=payload.get("external_ref"),
            error_code=payload.get("error_code"),
            refs=[],
        )

    async def poll(self, external_ref: str, ctx: CallContext) -> ToolResult:
        return await self._inner.poll(external_ref, ctx)

    # ---- MCP 管理委托（tool_hub 聚合后 CLI/gateway 仍直达内层能力）----
    async def add_server(
        self, actor: AuthContext, server_id: str, *,
        command: str | None = None, url: str | None = None,
        risks: list[str] | None = None, env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        inner = cast(McpHubAdapter, self._inner)
        return await inner.add_server(
            actor, server_id, command=command, url=url, risks=risks, env=env
        )

    async def list_servers(self, actor: AuthContext) -> list[dict[str, Any]]:
        return await cast(McpHubAdapter, self._inner).list_servers(actor)

    async def probe(self, actor: AuthContext, server_id: str) -> dict[str, Any]:
        """U4-05：真实探针（协商协议版本 + 刷新工具清单），失败不伪造成功。"""
        return await cast(McpHubAdapter, self._inner).probe(actor, server_id)

    async def set_enabled(
        self, actor: AuthContext, server_id: str, enabled: bool
    ) -> dict[str, Any]:
        """U4-05：启用/停用 MCP server，收窄可调用面。"""
        return await cast(McpHubAdapter, self._inner).set_enabled(actor, server_id, enabled)

    async def remove_server(self, actor: AuthContext, server_id: str) -> None:
        await cast(McpHubAdapter, self._inner).remove_server(actor, server_id)

    async def cancel(self, handle: str, ctx: CallContext) -> ToolResult:
        return await self._inner.cancel(handle, ctx)
