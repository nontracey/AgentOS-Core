"""Node Agent：轻量 FastAPI 服务，部署在 Windows/Pi/任意节点上。

接收 gateway 派发的工具执行请求，在本地执行并返回结果。
通过 token 认证（与 gateway 共享 secret）；每次派发有独立 call_id 和超时。

安全语义（步骤 1 修复 + 步骤 2 Asset）：
- /tools 与 /execute 都运行在事件循环内，工具调用直接 await，禁止 asyncio.run。
- AuthContext 按需生成并按 TTL 续期：长任务/多节点下不再因启动时固定 1 小时而整节点失效。
- fence 与 deadline 由派发方（gateway）随请求传入，Node Agent 只承接不放宽；
  fence=0 仅作为旧客户端的向后兼容默认值。
- /assets 上传下载：文件全部锁定在节点沙箱 assets 目录；上传按 X-Asset-Sha256
  校验，下载按登记 hash 返回；两端都要求节点令牌。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from agentos.adapters.tools.local import LocalToolAdapter
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import AuthContext, CallContext, ToolResult, utc_now

AUTH_TTL = timedelta(hours=24)
DEFAULT_DEADLINE = timedelta(hours=1)
_SAFE_REF = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_MAX_ASSET_BYTES = 512 * 1024 * 1024
# 效果未知类错误（FR-ORC-08）：回执必须标 uncertain 转对账，不得标 failed 伪装
# 确定性失败——曾把 TIMEOUT/UNAVAILABLE 都回 failed，gateway 侧丢失对账语义。
_UNCERTAIN_CODES = {ErrorCode.TIMEOUT, ErrorCode.UNAVAILABLE, ErrorCode.UNCERTAIN}


class ExecuteRequest(BaseModel):
    tool_name: str
    args: dict[str, Any]
    call_id: str
    step_id: str
    run_id: str
    idempotency_key: str
    fence: int = Field(default=0, ge=0)
    deadline_seconds: float = Field(default=DEFAULT_DEADLINE.total_seconds(), gt=0)


class NodeAgentApp:
    """Node Agent：接收远程派发，本地执行工具，返回结果。"""

    def __init__(
        self, *, token: str, sandbox_dir: Path, tenant_id: str = "default",
        extra_tools: list[Any] | None = None,
    ) -> None:
        self._token = token
        self._tenant_id = tenant_id
        self._local = LocalToolAdapter(sandbox_dir, isolated_subjects=False)
        self._extra = list(extra_tools or [])
        self._assets_dir = sandbox_dir / "assets"
        self._actor: AuthContext | None = None

    def _auth(self) -> AuthContext:
        """返回未过期的节点身份；过期即重建（服务长跑不失效）。"""
        if self._actor is None or self._actor.is_expired:
            self._actor = AuthContext(
                subject="node-agent", tenant_id=self._tenant_id,
                scopes=["read", "write"], policy_version="node-1",
                expires_at=utc_now() + AUTH_TTL,
            )
        return self._actor

    def _route(self, tool_name: str) -> Any:
        """命名空间路由：extra 适配器声明的能力优先（comfyui.* / stt.transcribe），
        其余 → 本地沙箱工具。"""
        for adapter in self._extra:
            if adapter.has_tool(tool_name):
                return adapter
        return self._local

    def _verify(self, authorization: str | None) -> None:
        if not authorization or not hmac.compare_digest(
            authorization.removeprefix("Bearer ").strip(), self._token
        ):
            raise HTTPException(status_code=401, detail="invalid node token")

    async def execute(self, req: ExecuteRequest) -> dict[str, Any]:
        ctx = CallContext(
            run_id=req.run_id, step_id=req.step_id, call_id=req.call_id,
            actor=self._auth(), idempotency_key=req.idempotency_key,
            fence=req.fence, deadline_at=utc_now() + timedelta(seconds=req.deadline_seconds),
        )
        adapter = self._route(req.tool_name)
        try:
            if adapter is self._local:
                result: ToolResult = await self._local.call(req.tool_name, req.args, ctx)
            else:
                value = await adapter.call(req.tool_name, req.args, ctx)
                result = ToolResult(call_id=req.call_id, **value) if isinstance(value, dict) else value
        except AgentOSError as exc:
            # 效果未知（超时/连接丢失等）→ uncertain（对账语义）；确定性错误 → failed。
            # 与 Reconciler 的 manual-recovery 分类一致：节点工具错误默认不盲重试。
            status = "uncertain" if exc.code in _UNCERTAIN_CODES else "failed"
            return ToolResult(
                call_id=req.call_id, status=status,
                error_code=exc.code.value,
                value={"message": exc.message},
            ).model_dump(mode="json")
        return result.model_dump(mode="json")

    async def get_tools(self) -> list[dict[str, Any]]:
        specs = await self._local.list_tools(self._auth())
        for adapter in self._extra:
            specs.extend(adapter.list_tools(self._auth()))
        return [s.model_dump(mode="json") for s in specs]

    def health(self) -> dict[str, str]:
        return {"status": "ok", "node": "agent"}

    # ------------------------------------------------------------- Asset
    def _asset_path(self, ref: str) -> Path:
        if not _SAFE_REF.match(ref):
            raise HTTPException(status_code=422, detail="invalid asset ref")
        path = (self._assets_dir / ref).resolve()
        if not path.is_relative_to(self._assets_dir.resolve()):
            raise HTTPException(status_code=422, detail="asset path escapes sandbox")
        return path

    def asset_put(self, body: bytes, filename: str, expect_sha: str) -> dict[str, Any]:
        if len(body) > _MAX_ASSET_BYTES:
            raise HTTPException(status_code=413, detail="asset too large")
        digest = hashlib.sha256(body).hexdigest()
        if expect_sha and not hmac.compare_digest(expect_sha, digest):
            raise HTTPException(status_code=422, detail="sha256 mismatch")
        asset_id = f"{uuid.uuid4().hex[:12]}_{_SAFE_REF.sub('_', filename)[:60]}"
        path = self._asset_path(asset_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return {"asset_id": asset_id, "sha256": digest, "size": len(body)}

    def asset_get(self, ref: str) -> tuple[bytes, str]:
        path = self._asset_path(ref)
        if not path.exists():
            raise HTTPException(status_code=404, detail="asset not found")
        data = path.read_bytes()
        return data, hashlib.sha256(data).hexdigest()


def create_node_app(
    *, token: str, sandbox_dir: Path, tenant_id: str = "default",
    extra_tools: list[Any] | None = None,
) -> FastAPI:
    agent = NodeAgentApp(token=token, sandbox_dir=sandbox_dir, tenant_id=tenant_id,
                         extra_tools=extra_tools)
    app = FastAPI(title="AgentOS Node Agent")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return agent.health()

    @app.get("/tools")
    async def list_tools(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        agent._verify(authorization)
        return await agent.get_tools()

    @app.post("/execute")
    async def execute(
        req: ExecuteRequest, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        agent._verify(authorization)
        return await agent.execute(req)

    @app.post("/assets")
    async def asset_upload(
        request: Request,
        authorization: str | None = Header(default=None),
        x_asset_filename: str = Header(default="asset.bin"),
        x_asset_sha256: str = Header(default=""),
    ) -> dict[str, Any]:
        agent._verify(authorization)
        body = await request.body()
        return agent.asset_put(body, x_asset_filename, x_asset_sha256)

    @app.get("/assets/{ref}")
    async def asset_download(
        ref: str, authorization: str | None = Header(default=None)
    ) -> Any:
        agent._verify(authorization)
        data, digest = agent.asset_get(ref)
        from fastapi.responses import Response

        return Response(
            content=data, media_type="application/octet-stream",
            headers={"X-Asset-Sha256": digest},
        )

    return app
