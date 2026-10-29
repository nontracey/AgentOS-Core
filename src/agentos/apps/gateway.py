"""HTTP 网关（蓝图 §8 接口表）：身份、幂等、范围校验在受理前完成。

- POST /v1/tasks：202 + run_id/job_id/accepted；受理失败不返回成功；队列超限 429。
- 同键同 payload_hash 重放返回原回执；不同正文 409（FR-ING-03）。
- 资源访问按主体过滤：owner 之外的任务不可见（FR-GOV-01）。
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse

from agentos.apps.composition import SystemHandle, build_system
from agentos.apps.config import Settings
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.states import RunState
from agentos.kernel.store import RunRecord
from agentos.kernel.types import AuthContext, Receipt, Ref, utc_now
from agentos.services.access import role_of
from agentos.services.assets import to_public
from agentos.services.observability import public_json, public_value

_STATUS_MAP: dict[ErrorCode, int] = {
    ErrorCode.DENIED: 401,
    ErrorCode.CONFLICT: 409,
    ErrorCode.INVALID: 422,
    ErrorCode.UNSUPPORTED: 501,
    ErrorCode.BUDGET_EXCEEDED: 429,
    ErrorCode.UNAVAILABLE: 503,
    ErrorCode.TIMEOUT: 504,
    ErrorCode.UNCERTAIN: 500,
}


def _http_error(exc: AgentOSError) -> HTTPException:
    status = _STATUS_MAP.get(exc.code, 500)
    headers = {"Retry-After": "5"} if exc.code in {ErrorCode.BUDGET_EXCEEDED, ErrorCode.UNAVAILABLE} else None
    return HTTPException(status_code=status, detail=exc.to_payload(), headers=headers)


def _display_goal(run: RunRecord) -> str:
    graph = json.loads(run.envelope_json or '{}').get('payload', {}).get('workflow_graph', {})
    if str(graph.get('id', '')).startswith('studio_') and graph.get('name'):
        return str(graph['name'])
    if str(graph.get('id', '')).startswith('aigc_') and graph.get('name'):
        title = str(graph['name'])
        head, sep, stage = title.rpartition(' · ')
        labels = {'generate': '生成候选', 'speak': '本地配音', 'compose': '合成成片'}
        return head + sep + labels[stage] if sep and stage in labels else title
    return run.goal_text


def create_app(
    settings: Settings | None = None,
    *,
    model_script: list[dict[str, Any]] | None = None,
    system: SystemHandle | None = None,
) -> FastAPI:
    handle = system or build_system(settings or Settings.load(), model_script=model_script)
    handle.boot()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        if handle.workflow_engine is not None:
            try:
                await asyncio.wait_for(handle.workflow_engine.open(), timeout=5)
            except Exception:
                # Optional engine failure must not disable UI, HTTP or ordinary tasks.
                handle.workflow_driver.engine = None
        try:
            async with mcp_server.session_manager.run():
                stop = asyncio.Event()
                task = asyncio.create_task(handle.worker.run_forever(stop))
                if handle.studio is not None:
                    await handle.studio.start()
                try:
                    yield
                finally:
                    if handle.studio is not None:
                        await handle.studio.stop()
                    stop.set()
                    try:
                        await asyncio.wait_for(task, 5)
                    except TimeoutError:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
        finally:
            if handle.workflow_engine is not None:
                await handle.workflow_engine.close()

    app = FastAPI(title="AgentOS Gateway", version="0.1.0", lifespan=lifespan)

    if handle.settings.identity_lanes:
        from agentos.adapters.identity.federation import FederatedIdentity
        from agentos.apps.federated_login import mount as mount_login

        backend = handle.deps.identity._backend
        assert isinstance(backend, FederatedIdentity)
        mount_login(app, backend, handle.settings.data_dir)
    if handle.model_gate is not None:
        from agentos.apps.model_proxy import router as model_router

        app.include_router(model_router(handle.model_gate))

    from agentos.apps.mcp_server import mount as mount_mcp

    mcp_server = mount_mcp(app, handle)

    def _credentials(authorization: str | None) -> dict[str, str]:
        if not authorization or not authorization.startswith("Bearer "):
            raise _http_error(AgentOSError(ErrorCode.DENIED, "missing bearer token"))
        return {"token": authorization.removeprefix("Bearer ").strip()}

    async def _actor(authorization: str | None) -> AuthContext:
        """统一身份解析：AuthContext 只由可信身份层签发，客户端自报角色无效。"""
        return await handle.deps.identity.authenticate(_credentials(authorization))

    async def _write_actor(authorization: str | None, action: str) -> AuthContext:
        """写动作统一前置：guest 一律拒绝（发生在任何入队/副作用之前）。"""
        actor = await _actor(authorization)
        handle.access.require_write_allowed(actor, action)
        return actor

    async def _admin_actor(authorization: str | None, action: str) -> AuthContext:
        actor = await _actor(authorization)
        handle.access.require_admin(actor, action)
        return actor

    from agentos.apps.aigc import mount as mount_aigc

    mount_aigc(app, handle, _actor)

    if handle.chat is not None:
        from agentos.apps.chat import mount as mount_chat

        def _register_chat_asset(actor: AuthContext, data: bytes, mime: str, name: str) -> dict[str, Any]:
            return handle.assets.register_local(actor, data, mime=mime, name=name,
                                                meta={"kind": "chat_attachment"}, source_node="upload")

        mount_chat(app, handle.chat, _actor, write_guard=handle.access.require_write_allowed,
                   register_asset=_register_chat_asset)
    if handle.studio is not None:
        from agentos.apps.studio import mount as mount_studio

        mount_studio(app, handle, _actor)

    def _guest_or_404(exc: AgentOSError) -> HTTPException:
        """游客不可访问或不存在的资源统一 404：不泄露资源存在性。"""
        if exc.code is ErrorCode.DENIED:
            return HTTPException(status_code=404, detail={"code": "invalid", "message": "not found"})
        return _http_error(exc)

    @app.exception_handler(AgentOSError)
    async def _agentos_error(_request: object, exc: AgentOSError) -> JSONResponse:
        return JSONResponse(status_code=_STATUS_MAP.get(exc.code, 500), content=exc.to_payload())

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "profile": handle.settings.profile}

    @app.post("/v1/tasks", status_code=202)
    async def submit_task(
        body: dict[str, Any],
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        try:
            receipt: Receipt = await handle.ingress.accept(
                body,
                channel="http",
                credentials=_credentials(authorization),
                thread_id=body.get("thread_id"),
            )
        except AgentOSError as exc:
            raise _http_error(exc) from exc
        return receipt.model_dump()

    @app.post("/v1/tasks/{run_id}/inputs", status_code=202)
    async def supplement_task(
        run_id: str, body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        actor = await _actor(authorization)
        result: dict[str, Any] = handle.collaboration.supplement(actor, run_id, body)
        return result

    @app.post("/v1/tasks/{run_id}/followups", status_code=202)
    async def followup_task(
        run_id: str,
        body: dict[str, Any],
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        goal = str(body.get("message") or "").strip()
        if not goal or len(goal) > 2000:
            raise AgentOSError(ErrorCode.INVALID, "message must contain 1..2000 characters")
        payload = {"goal": goal, "context_run_id": run_id}
        if body.get("parent_asset_id"):
            payload["parent_asset_id"] = str(body["parent_asset_id"])
        actor = await _actor(authorization)
        try:
            receipt = await handle.ingress.accept(
                {
                    "payload": payload,
                    "material_asset_ids": body.get("material_asset_ids") or [],
                    "idempotency_key": body.get("idempotency_key"),
                },
                channel="http",
                credentials={},
                actor_override=actor,
            )
        except AgentOSError as exc:
            if exc.code is ErrorCode.DENIED:
                raise HTTPException(status_code=403, detail=exc.to_payload()) from exc
            raise
        return receipt.model_dump()

    @app.get("/v1/tasks/{run_id}")
    async def get_task(run_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        creds = _credentials(authorization)
        actor = await handle.deps.identity.authenticate(creds)
        if role_of(actor) == "guest":
            # R-D：游客统一 404——不泄露任务存在性；已发布内容走 /v1/demo 投影
            raise HTTPException(status_code=404, detail={"code": "invalid", "message": "run not found"})
        run = handle.store.get_run(actor.tenant_id, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail={"code": "invalid", "message": "run not found"})
        # R-D：统一判定走 AccessService——管理员按部署范围读取跨主体任务；成员仍限本人
        decision = handle.access.check(actor, "task.read", kind="task", resource_id=run_id)
        if not decision.allowed:
            handle.deps.audit.record(actor.tenant_id, "task.cross_subject_denied", subject=actor.subject, run_id=run_id)
            raise HTTPException(status_code=403, detail={"code": "denied", "message": "not your task"})
        is_admin = handle.access.is_admin(actor)
        steps = handle.store.list_steps(actor.tenant_id, run_id)
        # N2b 详情聚合：计划/成果/待审批由服务端权威拼装，前端不自行拼接猜测
        envelope = json.loads(run.envelope_json) if run.envelope_json else {}
        checkpoint = json.loads(run.checkpoint_json or "{}")
        raw_plan = checkpoint.get("active_plan") or envelope.get("payload", {}).get("plan")
        plan: dict[str, Any] | None = None
        goal_txt = str(envelope.get("payload", {}).get("goal", ""))
        if isinstance(raw_plan, dict):
            plan = {
                "plan_id": raw_plan.get("plan_id"),
                "source": "workflow" if goal_txt.startswith("workflow:") else "submitted",
                "version": raw_plan.get("version"),
                "tasks": [
                    {
                        **t,
                        "title": t.get("task_id"),
                        "state": checkpoint.get("collaboration", {})
                        .get("tasks", {})
                        .get(t.get("task_id"), {})
                        .get("state", "pending"),
                    }
                    for t in raw_plan.get("tasks", [])
                ],
            }
        graph = envelope.get("payload", {}).get("workflow_graph")
        if isinstance(graph, dict):
            outputs = checkpoint.get("workflow_nodes", {})
            plan = {"source": "temporal", "plan_id": graph.get("id"),
                    "version": envelope.get("payload", {}).get("workflow_version"), "tasks": []}
            nodes = graph.get("nodes", [])
            skipped_ids = {node["id"] for node in nodes if any(
                outputs.get(k, {}).get("state") == "completed" and outputs[k].get("branch") != expected
                for k, expected in node.get("when", {}).items())}
            while True:
                extended = skipped_ids | {node["id"] for node in nodes
                                          if skipped_ids.intersection(node.get("depends_on", []))}
                if extended == skipped_ids:
                    break
                skipped_ids = extended
            for node in nodes:
                skipped = node["id"] in skipped_ids
                plan["tasks"].append({"task_id": node["id"], "title": node.get("label") or node["id"],
                    "role_id": node["kind"], "depends_on": node.get("depends_on", []),
                    "state": "skipped" if skipped else outputs.get(node["id"], {}).get("state", "pending")})
        if envelope.get("payload", {}).get("template") == "material-report":
            plan = {
                "source": "template",
                "plan_id": "material-report",
                "version": "1",
                "tasks": [
                    {"task_id": key, "title": title}
                    for key, title in [
                        ("materials", "读取授权材料"),
                        ("report_text", "生成报告正文"),
                        ("checked", "结构与引用检查"),
                        ("written", "经审批保存报告"),
                        ("delivery", "登记不可变成果"),
                    ]
                ],
            }
        step_dtos = []
        for step in steps:
            dto = step.model_dump(mode="json")
            for field in ("result_json", "error_json"):
                if dto.get(field):
                    dto[field] = public_json(dto[field])
            step_dtos.append(dto)
        # R-D：管理员在租户范围读取该 run 全部成果（含受控主体产生）；其他主体仍限本人
        artifacts = (
            [to_public(a) for a in handle.store.list_assets(actor.tenant_id) if a.get("run_id") == run_id]
            if is_admin
            else [a for a in handle.assets.list(actor) if a.get("run_id") == run_id]
        )
        pending_approvals = [
            p.model_dump(mode="json")
            for p in handle.deps.approvals.list_pending(actor.tenant_id)
            # Q10：管理员按部署范围看到本 run 待裁审批（含受控入口产生的任务）
            if (p.subject == actor.subject or is_admin) and str(p.call_id).startswith(f"{run_id}:")
        ]
        run_dto = run.model_dump(mode="json", exclude={"envelope_json", "checkpoint_json"})
        run_dto['goal_text'] = _display_goal(run)
        if run_dto.get("error_json"):
            run_dto["error_json"] = public_json(run_dto["error_json"])
        return {
            "run": run_dto,
            "steps": step_dtos,
            "budget": handle.store.budget_totals(actor.tenant_id, run_id),
            "plan": plan,
            "artifacts": artifacts,
            "approvals": pending_approvals,
            "error": json.loads(public_json(run.error_json)) if run.error_json else None,
            "context": envelope.get("payload", {}).get("context"),
            "parent_asset_id": envelope.get("payload", {}).get("parent_asset_id"),
            "supplement": (
                {"round": checkpoint["collaboration"]["round"], "max_rounds": 2}
                if "collaboration" in checkpoint
                else None
            ),
            "refs": checkpoint.get("template", {}).get("refs")
            or [
                ref
                for asset in handle.store.list_assets(actor.tenant_id)
                if asset.get("run_id") == run_id
                for ref in json.loads(asset.get("meta_json") or "{}").get("refs", [])
            ],
            "messages": json.loads(
                public_json(
                    json.dumps(
                        [
                            {"role": m["role"], "content": m.get("content", "")}
                            for m in json.loads(run.checkpoint_json or "{}").get("messages", [])
                            if m.get("role") == "assistant" and not m.get("tool_calls")
                        ],
                        ensure_ascii=False,
                    )
                )
            ),
        }

    @app.post("/v1/tasks/{run_id}/references/resolve")
    async def resolve_task_reference(
        run_id: str,
        body: dict[str, Any],
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor = await _actor(authorization)
        if role_of(actor) == "guest":
            raise HTTPException(status_code=404, detail={"message": "not found"})
        handle.access.require(actor, "task.read", kind="task", resource_id=run_id)
        source_id, locator = str(body.get("source_id") or ""), str(body.get("locator") or "")
        for step in handle.store.list_steps(actor.tenant_id, run_id):
            result = json.loads(step.result_json) if step.result_json else {}
            for ref in result.get("refs", []):
                if ref.get("source_id") == source_id and ref.get("locator") == locator:
                    backend = handle.knowledge.for_run(actor, run_id)
                    resolved: dict[str, Any] = await backend.resolve_ref(actor, Ref.model_validate(ref))
                    return resolved
        try:
            return handle.assets.resolve_run_reference(actor, run_id, source_id, locator)
        except AgentOSError as exc:
            if exc.code is ErrorCode.DENIED:
                raise HTTPException(status_code=403, detail=exc.to_payload()) from exc
            raise

    @app.get("/v1/tasks/{run_id}/events")
    async def get_task_events(
        run_id: str,
        authorization: str | None = Header(default=None),
        cursor: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        creds = _credentials(authorization)
        actor = await handle.deps.identity.authenticate(creds)
        if role_of(actor) == "guest":
            try:
                return handle.demo.guest_events(actor, run_id, cursor=cursor, limit=limit)
            except AgentOSError as exc:
                raise _guest_or_404(exc) from exc
        run = handle.store.get_run(actor.tenant_id, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail={"code": "invalid", "message": "run not found"})
        # R-D：与详情路由同一判定——管理员按部署范围读取跨主体任务的事件；
        # 其他主体跨主体 404（与"不存在"同形，无存在性差异）
        decision = handle.access.check(actor, "task.read", kind="task", resource_id=run_id)
        if not decision.allowed:
            handle.deps.audit.record(actor.tenant_id, "task.cross_subject_denied", subject=actor.subject, run_id=run_id)
            raise HTTPException(status_code=404, detail={"code": "invalid", "message": "run not found"})
        events = handle.store.event_list(actor.tenant_id, run_id, after_seq=cursor, limit=limit)
        next_cursor = events[-1]["seq"] if events else cursor
        return {"events": events, "cursor": next_cursor}

    @app.get("/v1/tasks/{run_id}/trace")
    async def get_task_trace(
        run_id: str,
        authorization: str | None = Header(default=None),
        cursor: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=500),
    ) -> dict[str, Any]:
        """同任务事件的产品视图：保留决策证据，隐藏敏感字段，不暴露 checkpoint。"""
        result = await get_task_events(run_id, authorization, cursor, limit)
        return {"events": [public_value(e) for e in result["events"]], "cursor": result["cursor"]}

    @app.get("/v1/tasks/{run_id}/stream")
    async def stream_task_events(
        run_id: str,
        authorization: str | None = Header(default=None),
        cursor: int = Query(default=0, ge=0),
        follow: int = Query(default=1, ge=0, le=1),
    ) -> Any:
        """任务事件流（U4-04）：鉴权 fetch 的 SSE，支持游标恢复与撤权即时关闭。

        - 每批发送前复核授权：撤权/过期立即发 access.revoked 并关闭连接。
        - 游标 = 事件 seq；断连后用同一 cursor 续接，不丢不重。
        - 游客只收到授权投影（提示词类事件整条剔除）。
        """
        import asyncio

        from fastapi.responses import StreamingResponse

        actor = await _actor(authorization)
        guest = role_of(actor) == "guest"
        if guest:
            try:
                handle.demo.guest_events(actor, run_id, cursor=cursor, limit=1)  # 先行核权
            except AgentOSError as exc:
                # 游客面统一 404：未发布/不存在同形，不经全局 401 映射
                raise _guest_or_404(exc) from exc
        else:
            if handle.store.get_run(actor.tenant_id, run_id) is None:
                raise HTTPException(status_code=404, detail={"code": "invalid", "message": "run not found"})
            try:
                handle.access.require(actor, "task.read", kind="task", resource_id=run_id)
            except AgentOSError as exc:
                # 与 events 路由同形：跨主体/范围拒绝 404（管理员按部署范围放行，不会被拒）
                raise HTTPException(status_code=404, detail={"code": "invalid", "message": "run not found"}) from exc

        async def _sse(event: dict[str, Any]) -> str:
            return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        async def _generate() -> Any:
            nonlocal cursor
            idle_ticks = 0
            while True:
                caught_up = True
                if guest:
                    try:
                        page = handle.demo.guest_events(actor, run_id, cursor=cursor, limit=200)
                    except AgentOSError:
                        yield await _sse(
                            {"kind": "access.revoked", "seq": cursor, "payload": {"reason": "demo access ended"}}
                        )
                        return
                    events = page["events"]
                    # Q4：游标按**原始事件页** cursor 前进——被投影过滤掉的事件也要
                    # 消费掉；原始窗口未取尽前不算追平，不提前发 stream.end
                    page_cursor = int(page.get("cursor") or cursor)
                    caught_up = page_cursor <= cursor
                    cursor = max(cursor, page_cursor)
                    for event in events:
                        yield await _sse(event)
                else:
                    decision = handle.access.check(actor, "task.read", kind="task", resource_id=run_id)
                    if not decision.allowed:
                        yield await _sse(
                            {"kind": "access.revoked", "seq": cursor, "payload": {"reason": decision.reason}}
                        )
                        return
                    raw = handle.store.event_list(actor.tenant_id, run_id, after_seq=cursor, limit=200)
                    events = raw
                    caught_up = not raw
                    for event in raw:
                        cursor = int(event["seq"])
                        yield await _sse(event)
                run = handle.store.get_run(actor.tenant_id, run_id)
                state = run.state.value if run else "failed"
                if caught_up and not events and state in {"succeeded", "failed", "cancelled"}:
                    yield await _sse({"kind": "stream.end", "seq": cursor, "payload": {"state": state}})
                    return
                if caught_up and not events and not follow:
                    yield await _sse(
                        {"kind": "stream.end", "seq": cursor, "payload": {"state": state, "mode": "snapshot"}}
                    )
                    return
                idle_ticks += 1
                if idle_ticks % 50 == 0:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(0.2)

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store, private", "X-Accel-Buffering": "no"},
        )

    @app.get("/v1/capabilities")
    async def ui_capabilities(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await _actor(authorization)
        handle.access.require(actor, "capabilities.read")
        nodes = []
        for row in handle.store.list_nodes(actor.tenant_id):
            caps = json.loads(row.get("capabilities_json") or "[]")
            nodes.append(
                {
                    "id": row["node_id"],
                    "os": row["os"],
                    "status": row["status"],
                    "last_heartbeat": row.get("last_heartbeat"),
                    "tools": [str(c.get("name") or "") for c in caps],
                }
            )
        return {
            "features": {"knowledge": handle.knowledge.enabled,
                         "workflows": handle.workflow_driver.engine is not None},
            "model": {
                "name": "scripted-1" if handle.settings.model_provider == "scripted" else handle.settings.model_name,
                "provider": handle.settings.model_provider,
                "status": "configured",
                "health": "not_probed",
            },
            "fallback": {
                "configured": bool(handle.settings.model_fallback_name),
                "name": handle.settings.model_fallback_name,
            },
            "nodes": public_value(nodes),
            "template": {"id": "material-report", "version": "1"},
        }

    @app.post("/v1/tasks/{run_id}/resume", status_code=202)
    async def resume_runtime_task(run_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await _write_actor(authorization, "task.retry")
        if not actor.has_scope("write"):
            handle.deps.audit.record(actor.tenant_id, "access.denied", subject=actor.subject,
                                    detail={"action": "task.retry", "reason": "actor lacks write scope"})
            raise HTTPException(status_code=403, detail={"code": "denied",
                "message": "当前身份没有恢复任务所需的写权限"})
        decision = handle.access.check(actor, "task.retry", kind="task", resource_id=run_id)
        if not decision.allowed:
            raise HTTPException(status_code=404, detail={"code": "invalid", "message": "run not found"})
        run = handle.store.get_run(actor.tenant_id, run_id)
        assert run is not None
        error = json.loads(run.error_json or "{}")
        nodes = json.loads(run.checkpoint_json or "{}").get("runtime_nodes", {})
        if (run.state != RunState.PAUSED or error.get("code") not in
                {"provider_rate_limited", "runtime_unavailable"}
                or not any(node.get("engine") == "codex" for node in nodes.values())):
            raise HTTPException(status_code=409, detail={"code": "conflict", "message": "runtime not resumable"})
        if not handle.store.resume_runtime(actor.tenant_id, run_id, run.version, actor.subject):
            raise HTTPException(status_code=409, detail={"code": "conflict",
                "message": "task changed or unresolved tool effect; reconcile or approve first"})
        return {"run_id": run_id, "state": "running"}

    @app.post("/v1/tasks/{run_id}/cancel", status_code=202)
    async def cancel_task(run_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await _write_actor(authorization, "task.cancel")
        # Q10/R-D：统一判定——管理员按部署范围可取消受控主体任务，其他主体限本人
        decision = handle.access.check(actor, "task.cancel", kind="task", resource_id=run_id)
        if not decision.allowed:
            handle.deps.audit.record(
                actor.tenant_id,
                "task.cross_subject_denied",
                subject=actor.subject,
                run_id=run_id,
                detail={"action": "task.cancel", "reason": decision.reason},
            )
            raise HTTPException(status_code=404, detail={"code": "invalid", "message": "run not found"})
        updated = handle.store.request_cancel(actor.tenant_id, run_id)
        state = updated.state if updated else RunState.CANCEL_REQUESTED
        return {"run_id": run_id, "state": state.value}

    @app.post("/v1/tasks/{run_id}/reconcile", status_code=202)
    async def reconcile_run(
        run_id: str,
        body: dict[str, Any],
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """对账（FR-ORC-08）：uncertain 步骤先探针外部真实状态，再决定补传或授权重放。

        - 外部已完成 → 直接补传产物为 completed（不重新执行副作用）
        - 外部仍在执行 → 409，禁止此刻重放
        - 无外部效果证据 → 步骤回 dispatched + 重放授权标记，worker 重派发
        - 无探针但底层工具契约声明重放安全（pure/幂等键）→ 同幂等键重放，
          外部按键去重保证效果恰好一次（F1 断连演练的恢复路径）
        - 其余（manual 且无探针）→ 501，等待具备探针或人工处理
        """
        from agentos.kernel.states import StepState
        from agentos.kernel.types import CallContext, ToolResult, utc_now

        actor = await _write_actor(authorization, "task.reconcile")
        # Q10：与取消同一口径——管理员可核对受控主体任务，其他主体跨主体 404
        decision = handle.access.check(actor, "task.reconcile", kind="task", resource_id=run_id)
        if not decision.allowed:
            handle.deps.audit.record(
                actor.tenant_id,
                "task.cross_subject_denied",
                subject=actor.subject,
                run_id=run_id,
                detail={"action": "task.reconcile", "reason": decision.reason},
            )
            raise HTTPException(status_code=404, detail={"code": "invalid", "message": "run not found"})
        step_id = str(body.get("step_id") or "")
        step = handle.store.get_step(actor.tenant_id, run_id, step_id) if step_id else None
        if step is None:
            raise HTTPException(status_code=404, detail={"code": "invalid", "message": "step not found"})
        if step.state != StepState.UNCERTAIN:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "conflict",
                    "message": f"step state is {step.state.value}, only uncertain steps reconcile",
                },
            )
        if not step.tool_name:
            raise HTTPException(
                status_code=501, detail={"code": "unsupported", "message": "step has no tool to reconcile"}
            )
        if not step.tool_name.endswith("_generate") or "_aigc_" in step.tool_name:
            # 无外部探针的工具：仅当底层工具契约声明重放安全（pure/幂等键）
            # 才授权重放——同幂等键由外部去重保证效果恰好一次。gateway 侧节点
            # spec 一律 manual，须查节点能力快照里的底层声明（防谎报）。
            recovery = ""
            try:
                spec = await handle.deps.tools.resolve(actor, step.tool_name)
                recovery = spec.recovery
            except AgentOSError:
                recovery = ""
            if recovery not in {"pure", "idempotency_key"} and hasattr(handle.deps.tools, "underlying_recovery"):
                recovery = await handle.deps.tools.underlying_recovery(actor, step.tool_name) or ""
            if recovery not in {"pure", "idempotency_key"}:
                raise HTTPException(
                    status_code=501, detail={"code": "unsupported", "message": "no external-state probe for this tool"}
                )
            handle.store.transition_step(
                actor.tenant_id,
                run_id,
                step_id,
                {StepState.UNCERTAIN},
                StepState.DISPATCHED,
                error_json=json.dumps(
                    {
                        "code": "uncertain",
                        "message": "reconciled: idempotent replay safe by tool contract",
                        "replay_authorized": True,
                    },
                    ensure_ascii=False,
                ),
            )
            handle.deps.audit.record(
                actor.tenant_id,
                "step.reconcile",
                subject=actor.subject,
                run_id=run_id,
                detail={"step_id": step_id, "tool": step.tool_name, "mode": "idempotent_replay", "recovery": recovery},
            )
            handle.store.event_append(
                actor.tenant_id,
                run_id,
                "step.reconcile",
                {"step_id": step_id, "tool": step.tool_name, "mode": "idempotent_replay", "recovery": recovery},
            )
            handle.store.enqueue_job(actor.tenant_id, run_id)
            return {
                "run_id": run_id,
                "step_id": step_id,
                "reconciled": "replay_authorized",
                "mode": "idempotent_replay",
            }
        probe_name = step.tool_name.rsplit("_", 1)[0] + "_probe"
        idem_tag = f"agentos-{run_id}:{step.call_id}"[:60]
        try:
            # resolve 先做节点能力实时刷新：节点重启新增 probe 工具后 name_map 才会有它
            await handle.deps.tools.resolve(actor, probe_name)
        except AgentOSError as exc:
            raise _http_error(exc) from exc
        probe_ctx = CallContext(
            run_id=run_id,
            step_id=step.id,
            call_id=f"reconcile:{step.call_id}",
            actor=actor,
            idempotency_key=f"reconcile-{run_id}:{step.id}",
            fence=0,
            deadline_at=utc_now() + timedelta(seconds=90),
        )
        try:
            probe = await handle.deps.tools.call(probe_name, {"idem_tag": idem_tag}, probe_ctx)
        except AgentOSError as exc:
            raise _http_error(exc) from exc
        val = probe.value if isinstance(probe.value, dict) else {}
        if val.get("in_flight"):
            raise HTTPException(
                status_code=409,
                detail={"code": "conflict", "message": "external call still in flight; retry reconcile later"},
            )
        handle.deps.audit.record(
            actor.tenant_id,
            "step.reconcile",
            subject=actor.subject,
            run_id=run_id,
            detail={
                "step_id": step_id,
                "tool": step.tool_name,
                "found": bool(val.get("found")),
                "probe_error": val.get("note"),
            },
        )
        handle.store.event_append(
            actor.tenant_id,
            run_id,
            "step.reconcile",
            {
                "step_id": step_id,
                "tool": step.tool_name,
                "found": bool(val.get("found")),
                "probe_note": val.get("note"),
            },
        )
        if val.get("found") and isinstance(val.get("result"), dict):
            inner = val["result"]
            completed = ToolResult(
                call_id=step.call_id,
                status="completed",
                value=inner.get("value") or {},
            )
            handle.store.transition_step(
                actor.tenant_id,
                run_id,
                step_id,
                {StepState.UNCERTAIN},
                StepState.COMPLETED,
                result_json=completed.model_dump_json(),
                error_json='{"code": null, "message": "cleared by reconcile"}',
            )
            handle.store.enqueue_job(actor.tenant_id, run_id)
            return {
                "run_id": run_id,
                "step_id": step_id,
                "reconciled": "completed_from_external",
                "value": completed.value,
            }
        # 无外部效果证据：授权重放（executor._resume_dispatched 依据标记放行一次）
        handle.store.transition_step(
            actor.tenant_id,
            run_id,
            step_id,
            {StepState.UNCERTAIN},
            StepState.DISPATCHED,
            error_json=json.dumps(
                {
                    "code": "uncertain",
                    "message": "reconciled: no external effect; replay authorized",
                    "replay_authorized": True,
                },
                ensure_ascii=False,
            ),
        )
        handle.store.enqueue_job(actor.tenant_id, run_id)
        return {"run_id": run_id, "step_id": step_id, "reconciled": "replay_authorized"}

    @app.get("/v1/runs")
    async def list_my_runs(
        authorization: str | None = Header(default=None),
        state: str | None = Query(default=None),
        q: str | None = Query(default=None, max_length=80),
    ) -> list[dict[str, Any]]:
        """历史列表（N3b）：管理员看本人部署全量；游客只返回已发布投影（列表/计数都过滤）。"""
        actor = await handle.deps.identity.authenticate(_credentials(authorization))
        if role_of(actor) == "guest":
            try:
                return handle.demo.guest_runs(actor)
            except AgentOSError as exc:
                raise _http_error(exc) from exc
        runs = handle.store.list_runs(
            actor.tenant_id,
            # R-D：管理员看本部署全量（含 webhook 等受控主体产生的任务）；其他主体仍按本人过滤
            subject=None if handle.access.is_admin(actor) else actor.subject,
        )
        if state:
            runs = [r for r in runs if r.state.value == state]
        if q:
            needle = q.lower()
            runs = [r for r in runs if needle in (_display_goal(r) or "").lower()
                    or needle in (r.goal_text or "").lower()]
        return [
            {
                "id": r.id,
                "state": r.state.value,
                "goal": _display_goal(r)[:80],
                "cost": handle.store.budget_totals(actor.tenant_id, r.id),
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in runs[:50]
        ]

    @app.get("/v1/mcp/servers")
    async def list_mcp_servers(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        actor = await _actor(authorization)
        handle.access.require(actor, "mcp.list")
        return await handle.tool_hub.list_servers(actor)

    @app.get("/v1/workflows")
    async def list_workflows(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        actor = await _actor(authorization)
        handle.access.require(actor, "workflow.list")
        rows = handle.store.list_workflow_runs(actor.tenant_id)
        return [row for row in rows if handle.access.check(
            actor, "task.read", kind="task", resource_id=str(row.get("internal_run_id") or ""))]

    @app.get("/v1/workflows/catalog")
    async def flow_catalog(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await _actor(authorization)
        handle.access.require(actor, "workflow.list")
        from agentos.services.capabilities import CapabilityCatalog

        catalog = handle.deps.tools
        assert isinstance(catalog, CapabilityCatalog)
        tools = await catalog.describe(actor)
        return {"engine_enabled": handle.workflow_driver.engine is not None,
                "can_edit": actor.has_scope("write"),
                "engine_status": ("available" if handle.workflow_driver.engine is not None
                                  else "unavailable" if handle.settings.temporal_address else "disabled"),
                "node_types": ["agent", "tool", "condition", "wait", "custom"],
                "business_templates": sorted(handle.workflow_driver.templates),
                "custom_nodes": sorted(handle.workflow_driver.custom),
                "tools": tools}

    @app.get("/v1/capabilities/catalog")
    async def capability_catalog(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await _actor(authorization)
        handle.access.require(actor, "capabilities.read")
        from agentos.services.capabilities import CapabilityCatalog

        catalog = handle.deps.tools
        assert isinstance(catalog, CapabilityCatalog)
        return {"tools": await catalog.describe(actor)}

    @app.get("/v1/workflow-drafts")
    async def flow_drafts(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        return handle.drafts.list(await _actor(authorization), "workflow")

    @app.get("/v1/workflow-drafts/{draft_id}")
    async def get_flow_draft(draft_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        return handle.drafts.get(await _actor(authorization), "workflow", draft_id)

    @app.post("/v1/workflow-drafts/{draft_id}")
    async def save_flow_draft(draft_id: str, body: dict[str, Any],
                              authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await _actor(authorization)
        if not actor.has_scope("write"):
            raise HTTPException(403, detail={"code": "denied", "message": "draft editing requires write scope"})
        payload = body.get("payload")
        if not isinstance(payload, dict):
            raise AgentOSError(ErrorCode.INVALID, "draft payload must be an object")
        return handle.drafts.save(actor, "workflow", draft_id,
                                  payload, body.get("expected_revision", -1))

    @app.post("/v1/workflow-drafts/{draft_id}/delete")
    async def delete_flow_draft(draft_id: str, body: dict[str, Any],
                                authorization: str | None = Header(default=None)) -> dict[str, bool]:
        handle.drafts.delete(await _actor(authorization), "workflow", draft_id, body.get("expected_revision", -1))
        return {"deleted": True}

    @app.get("/v1/mcp/proposals")
    async def connection_proposals(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        actor = await _admin_actor(authorization, "mcp.register")
        rows = handle.store.list_connection_drafts(actor.tenant_id)
        result = []
        for row in rows:
            server = handle.store.get_mcp_server(actor.tenant_id,
                json.loads(row["payload_json"])["config"]["server_id"])
            result.append({**handle.drafts._project(row), "proposed_by": row["subject"],
                           "applied": bool(server and server.get("proposal_id") == row["id"])})
        return result

    @app.post("/v1/mcp/proposals", status_code=201)
    async def propose_connection(body: dict[str, Any],
                                 authorization: str | None = Header(default=None)) -> dict[str, Any]:
        return handle.connections.propose(await _actor(authorization), body)

    @app.post("/v1/mcp/proposals/{proposal_id}/apply")
    async def apply_connection(proposal_id: str, body: dict[str, Any],
                               authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await _admin_actor(authorization, "mcp.register")
        return handle.connections.apply(actor, proposal_id, str(body.get("reviewed_hash", "")))

    @app.get("/v1/workflow-definitions")
    async def flow_definitions(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        actor = await _actor(authorization)
        handle.access.require(actor, "workflow.list")
        return handle.store.list_workflow_definitions(actor.tenant_id)

    @app.get("/v1/workflows/{workflow_id}/versions/{version}")
    async def flow_version(workflow_id: str, version: int,
                           authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await _actor(authorization)
        handle.access.require(actor, "workflow.list")
        return handle.workflows.export(actor, workflow_id, version)

    @app.post("/v1/workflows/{workflow_id}/publish", status_code=201)
    async def publish_workflow(
        workflow_id: str,
        body: dict[str, Any],
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor = await _write_actor(authorization, "workflow.publish")
        definition = dict(body)
        definition["id"] = workflow_id
        from agentos.services.capabilities import CapabilityCatalog

        catalog = handle.deps.tools
        assert isinstance(catalog, CapabilityCatalog)
        definition = await catalog.bind_graph(actor, definition)
        return handle.workflows.publish(actor, definition)

    @app.post("/v1/workflows/{workflow_id}/start", status_code=202)
    async def start_workflow(
        workflow_id: str,
        body: dict[str, Any],
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor = await _write_actor(authorization, "workflow.start")
        version = body.get("version")
        if type(version) is not int or version <= 0:
            raise _http_error(AgentOSError(ErrorCode.INVALID, "body.version required"))
        inputs = body.get("inputs", {})
        if not isinstance(inputs, dict):
            raise _http_error(AgentOSError(ErrorCode.INVALID, "body.inputs must be an object"))
        idempotency_key = str(body.get("idempotency_key") or f"wf_{workflow_id}_{version}")
        definition = handle.store.get_workflow_definition(actor.tenant_id, workflow_id, version)
        if (definition and json.loads(definition["definition_json"]).get("nodes")
                and handle.workflow_driver.engine is None):
            raise AgentOSError(ErrorCode.UNAVAILABLE, "workflow execution requires an available workflow engine")
        result = handle.workflows.start(actor, workflow_id, version, inputs, idempotency_key)
        return result

    @app.get("/v1/workflow-runs/{wf_run_id}")
    async def workflow_status(wf_run_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await handle.deps.identity.authenticate(_credentials(authorization))
        return handle.workflows.status(actor, wf_run_id)

    # ---- U4-06：知识后端（admin 管理/检索；游客只读 manifest 已发布片段）----
    @app.post("/v1/knowledge/sources/{source_id}/reconcile")
    async def reconcile_knowledge(source_id: str,
                                  authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await _write_actor(authorization, "knowledge.ingest")
        result: dict[str, Any] = await handle.knowledge.reconcile(actor, source_id)
        return result

    @app.get("/v1/knowledge/capabilities")
    async def knowledge_capabilities(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await _actor(authorization)
        handle.access.require(actor, "knowledge.search")
        caps = handle.knowledge.capabilities(actor)
        return {**caps, "guest_policy": "guest 只读 manifest 已发布的引用片段，不开放实时检索"}

    @app.post("/v1/knowledge/backend")
    async def select_knowledge_backend(
        body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        actor = await _admin_actor(authorization, "knowledge.backend")
        result: dict[str, Any] = await handle.knowledge.select(actor, str(body.get("backend") or ""))
        return result

    @app.post("/v1/knowledge/ingest", status_code=201)
    async def knowledge_ingest(
        body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        actor = await _write_actor(authorization, "knowledge.ingest")
        source_id = str(body.get("source_id") or "")
        text = body.get("text")
        if not source_id or not isinstance(text, str):
            raise HTTPException(
                status_code=422, detail={"code": "invalid", "message": "source_id and text are required"}
            )
        try:
            version = await handle.knowledge.ingest(actor, source_id, text, acl=list(body.get("acl") or []) or None)
        except AgentOSError as exc:
            raise _http_error(exc) from exc
        return {
            "source_id": source_id,
            "source_version": version,
            "backend": handle.knowledge.current(actor).backend_id,
        }

    @app.get("/v1/knowledge/search")
    async def knowledge_search(
        authorization: str | None = Header(default=None),
        q: str = Query(default="", max_length=400),
        limit: int = Query(default=5, ge=1, le=10),
    ) -> dict[str, Any]:
        actor = await _actor(authorization)
        handle.access.require(actor, "knowledge.search")
        try:
            binding = handle.knowledge.binding(actor)
            hits = await handle.knowledge.for_binding(binding).search(actor, q, limit)
        except AgentOSError as exc:
            raise _http_error(exc) from exc
        return {
            "backend": binding["backend"],
            "backend_version": binding["version"],
            "binding": binding,
            "hits": [h.model_dump(mode="json") for h in hits],
        }

    @app.post("/v1/knowledge/resolve")
    async def knowledge_resolve(
        body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        actor = await _actor(authorization)
        handle.access.require(actor, "knowledge.search")
        try:
            ref = Ref(
                source_id=str(body.get("source_id") or ""),
                source_version=str(body.get("source_version") or "1"),
                locator=str(body.get("locator") or ""),
                quote=str(body.get("quote") or ""),
            )
            backend = (
                handle.knowledge.for_binding(body["binding"])
                if body.get("binding")
                else handle.knowledge.current(actor)
            )
            out = await backend.resolve_ref(actor, ref)
        except AgentOSError as exc:
            raise _http_error(exc) from exc
        return dict(out)

    @app.delete("/v1/knowledge/sources/{source_id}")
    async def knowledge_delete_source(
        source_id: str, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        actor = await _write_actor(authorization, "knowledge.delete")
        try:
            await handle.knowledge.delete(actor, source_id)
        except AgentOSError as exc:
            raise _http_error(exc) from exc
        return {"deleted": source_id, "backend": handle.knowledge.current(actor).backend_id}

    # ---- U4-05：能力中心管理（全部 admin 核权；秘密只显示脱敏摘要）----
    @app.post("/v1/mcp/servers", status_code=201)
    async def register_mcp_server(
        body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        """注册并**真实探针**一次：配置→探针→发现→（默认启用）→可被任务解析。"""
        actor = await _admin_actor(authorization, "mcp.register")
        try:
            command = body.get("command")
            if isinstance(command, list):
                if not command or not all(isinstance(arg, str) and arg for arg in command):
                    raise AgentOSError(ErrorCode.INVALID, "command must contain nonempty arguments")
                import shlex

                command = shlex.join(command)
            if command is not None and not isinstance(command, str):
                raise AgentOSError(ErrorCode.INVALID, "command must be text or an argument list")
            result = await handle.tool_hub.add_server(
                actor,
                str(body.get("server_id") or ""),
                command=command,
                url=body.get("url"),
                risks=list(body.get("risks") or []) or None,
                env=dict(body.get("env") or {}) or None,
            )
        except AgentOSError as exc:
            raise _http_error(exc) from exc
        return result

    @app.post("/v1/mcp/servers/{server_id}/probe")
    async def probe_mcp_server(server_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await _admin_actor(authorization, "mcp.probe")
        try:
            return await handle.tool_hub.probe(actor, server_id)
        except AgentOSError as exc:
            raise _http_error(exc) from exc

    @app.post("/v1/mcp/servers/{server_id}/enabled")
    async def toggle_mcp_server(
        server_id: str, body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        actor = await _admin_actor(authorization, "mcp.enable")
        try:
            return await handle.tool_hub.set_enabled(actor, server_id, bool(body.get("enabled", True)))
        except AgentOSError as exc:
            raise _http_error(exc) from exc

    @app.delete("/v1/mcp/servers/{server_id}")
    async def delete_mcp_server(server_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await _admin_actor(authorization, "mcp.remove")
        try:
            await handle.tool_hub.remove_server(actor, server_id)
        except AgentOSError as exc:
            raise _http_error(exc) from exc
        return {"removed": server_id}

    @app.post("/v1/skills", status_code=201)
    async def install_skill(body: dict[str, Any], authorization: str | None = Header(default=None)) -> dict[str, Any]:
        """安装 Skill 只登记元数据与资源（安装 ≠ 授权，仍需按需读取正文与走策略）。"""
        actor = await _admin_actor(authorization, "skill.install")
        try:
            meta = (
                handle.skills.install_text(actor, str(body["content_text"]))
                if body.get("content_text")
                else handle.skills.install(actor, str(body.get("source") or ""))
            )
        except AgentOSError as exc:
            raise _http_error(exc) from exc
        # 安装结果只回元数据（不复述安装路径等内部细节）
        return {
            "id": meta.id,
            "version": meta.version,
            "digest": meta.digest,
            "description": meta.description,
            "allowed_tools": list(meta.allowed_tools),
            "entrypoints": list(meta.entrypoints),
        }

    @app.get("/v1/skills/{skill_id}/body")
    async def skill_body(
        skill_id: str,
        authorization: str | None = Header(default=None),
        version: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """按需读取正文：必须指定或解析到确定版本，包含版本与资源清单。"""
        actor = await _actor(authorization)
        handle.access.require(actor, "skill.list")
        try:
            body_text = handle.skills.load_body(actor, skill_id, version)
        except AgentOSError as exc:
            raise _http_error(exc) from exc
        rows = [r for r in handle.store.list_skills(actor.tenant_id) if r["id"] == skill_id]
        picked = next((r for r in rows if not version or str(r["version"]) == version), None)
        return {
            "skill_id": skill_id,
            "version": str(picked["version"]) if picked else version,
            "digest": str(picked["digest"]) if picked else "",
            "resources": json.loads(picked["entrypoints_json"]) if picked else [],
            "allowed_tools": json.loads(picked["allowed_tools_json"]) if picked else [],
            "body": body_text,
        }

    @app.post("/v1/skills/{skill_id}/enabled")
    async def toggle_skill(
        skill_id: str, body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        actor = await _admin_actor(authorization, "skill.activate")
        try:
            return handle.skills.set_enabled(
                actor, skill_id, str(body.get("version") or ""), bool(body.get("enabled", True))
            )
        except AgentOSError as exc:
            raise _http_error(exc) from exc

    @app.delete("/v1/skills/{skill_id}")
    async def remove_skill(
        skill_id: str,
        authorization: str | None = Header(default=None),
        version: str | None = Query(default=None),
    ) -> dict[str, Any]:
        actor = await _admin_actor(authorization, "skill.remove")
        try:
            removed = handle.skills.remove(actor, skill_id, version)
        except AgentOSError as exc:
            raise _http_error(exc) from exc
        return {"removed": removed, "skill_id": skill_id, "version": version}

    @app.get("/v1/skills")
    async def list_skills(
        authorization: str | None = Header(default=None), include_disabled: bool = Query(default=False)
    ) -> list[dict[str, Any]]:
        actor = await _actor(authorization)
        handle.access.require(actor, "skill.list")
        if include_disabled and not handle.access.is_admin(actor):
            raise HTTPException(status_code=403, detail={"code": "denied", "message": "admin required"})
        return handle.skills.list(actor, include_disabled=include_disabled)

    @app.get("/v1/grants")
    async def list_grants(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        """Owner pre-authorizations of single tools (FR-GOV-02); each is bounded and revocable."""
        return handle.grants.list(await _actor(authorization))  # type: ignore[no-any-return]

    @app.post("/v1/grants", status_code=201)
    async def issue_grant(body: dict[str, Any], authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await _write_actor(authorization, "policy.grant")
        hours = body.get("hours", 24)
        return await handle.grants.issue(actor, str(body.get("tool_name") or ""),  # type: ignore[no-any-return]
                                         hours if type(hours) is int else -1)

    @app.post("/v1/grants/{grant_id}/revoke")
    async def revoke_grant(grant_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await _write_actor(authorization, "policy.grant")
        return handle.grants.revoke(actor, grant_id)  # type: ignore[no-any-return]

    @app.get("/v1/approvals")
    async def list_approvals(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        creds = _credentials(authorization)
        actor = await handle.deps.identity.authenticate(creds)
        pending = handle.deps.approvals.list_pending(actor.tenant_id)
        # Q10：管理员管理个人部署内全部业务资源——包括受控入口（webhook/cron）
        # 产生的任务的待裁审批；其他主体仍只看到自己的，游客无任何写入口。
        if handle.access.is_admin(actor):
            return [p.model_dump(mode="json") for p in pending]
        return [p.model_dump(mode="json") for p in pending if p.subject == actor.subject]

    @app.post("/v1/approvals/{approval_id}/decision")
    async def decide_approval(
        approval_id: str,
        body: dict[str, Literal["approve", "reject"]],
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor = await _write_actor(authorization, "approval.decide")
        decision = body.get("decision")
        if decision not in {"approve", "reject"}:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid", "message": "decision must be approve|reject"},
            )
        record = handle.deps.approvals.decide(actor, approval_id, decision)
        if record.subject != actor.subject:
            # 跨主体代裁：审计记真实操作者与原始 owner，不冒充任务主体
            handle.deps.audit.record(
                actor.tenant_id,
                "approval.decided_cross_subject",
                subject=actor.subject,
                run_id=record.call_id.split(":", 1)[0],
                detail={"approval_id": approval_id, "owner": record.subject, "decision": decision},
            )
        # 审批决定后重新入队（唤醒 worker 重驱动 run）
        run_id = record.call_id.split(":", 1)[0]
        handle.store.enqueue_job(actor.tenant_id, run_id)
        return {"approval_id": approval_id, "state": record.state.value}

    @app.post("/v1/webhook/{source}", status_code=202)
    async def webhook_entry(
        source: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Webhook 入口（FR-ING-01 第四类）：HMAC 令牌验签 → 同一 accept 路径。"""
        import hashlib
        import hmac as hmac_mod

        body = await request.body()
        secret = getattr(handle.settings, "webhook_secret", "")
        if secret:
            provided = authorization or ""
            expected = hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()
            if not hmac_mod.compare_digest(provided, expected):
                handle.deps.audit.record(
                    "default", "identity.denied", subject=None, detail={"channel": "webhook", "reason": "bad signature"}
                )
                raise _http_error(AgentOSError(ErrorCode.DENIED, "invalid webhook signature"))
        from agentos.kernel.types import AuthContext, utc_now

        try:
            payload_body = json.loads(body or b"{}")
            actor_override = None
            if secret:
                # HMAC 验签通过 → 受限服务身份（外部通道默认需审批，不继承 owner）
                actor_override = AuthContext(
                    subject=f"webhook:{source}",
                    tenant_id="default",
                    scopes=["read", "write"],
                    policy_version="webhook-1",
                    expires_at=utc_now() + timedelta(minutes=5),
                )
            receipt: Receipt = await handle.ingress.accept(
                payload_body,
                channel=f"webhook:{source}",
                credentials={"token": (authorization or "").removeprefix("Bearer ").strip()},
                thread_id=payload_body.get("thread_id"),
                actor_override=actor_override,
            )
        except AgentOSError as exc:
            raise _http_error(exc) from exc
        return receipt.model_dump()

    @app.post("/v1/voice/tasks", status_code=202)
    async def voice_task(
        audio: UploadFile,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """语音入口（M6 切片）：音频转写 → 同一 accept 路径（身份/幂等/预算/注入防护零旁路）。"""
        import asyncio
        import tempfile

        if handle.transcriber is None:
            raise HTTPException(
                status_code=501,
                detail={"code": "unsupported", "message": "voice entry disabled (AGENTOS_STT_CMD not set)"},
            )
        creds = _credentials(authorization)
        suffix = Path(audio.filename or "audio.wav").suffix or ".wav"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(await audio.read())
            tmp_path = Path(tmp.name)
        try:
            text = await asyncio.to_thread(handle.transcriber.transcribe, tmp_path)
        except Exception as exc:  # noqa: BLE001 - 转写失败归一为受理错误
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid", "message": f"transcribe failed: {exc}"},
            ) from exc
        finally:
            tmp_path.unlink(missing_ok=True)
        try:
            receipt: Receipt = await handle.ingress.accept(
                {"payload": {"goal": text[:4000]}},
                channel="voice",
                credentials=creds,
            )
        except AgentOSError as exc:
            raise _http_error(exc) from exc
        return {**receipt.model_dump(), "transcript": text[:200]}

    # ---- 节点管理（M1 跨机主链：配对/心跳/撤销/列表）----

    @app.get("/v1/nodes")
    async def list_nodes(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        actor = await _actor(authorization)
        handle.access.require(actor, "node.list")
        return handle.nodes.list(actor)

    @app.post("/v1/nodes/pair")
    async def pair_node(body: dict[str, Any], authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await _write_actor(authorization, "node.pair")
        try:
            return handle.nodes.pair(
                actor,
                str(body.get("node_id") or ""),
                str(body.get("endpoint") or ""),
                os=str(body.get("os") or ""),
                arch=str(body.get("arch") or ""),
                token=str(body["token"]) if body.get("token") else None,
            )
        except AgentOSError as exc:
            raise _http_error(exc) from exc

    @app.post("/v1/nodes/{node_id}/heartbeat")
    async def node_heartbeat(
        node_id: str, body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        # 心跳用节点自身令牌认证（owner 令牌无效）
        token = (authorization or "").removeprefix("Bearer ").strip()
        try:
            return handle.nodes.heartbeat(
                "default",
                node_id,
                token,
                os=str(body.get("os") or ""),
                arch=str(body.get("arch") or ""),
                capabilities=list(body.get("capabilities") or []),
                version=str(body.get("version") or ""),
            )
        except AgentOSError as exc:
            raise _http_error(exc) from exc

    @app.post("/v1/nodes/{node_id}/revoke")
    async def revoke_node(node_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await _write_actor(authorization, "node.revoke")
        try:
            return handle.nodes.revoke(actor, node_id)
        except AgentOSError as exc:
            raise _http_error(exc) from exc

    # ---- 产品登录（双 token：短时 access + 轮换 refresh；静态令牌仍可用于 CLI/自动化）----
    @app.get("/v1/auth/setup")
    async def auth_setup() -> dict[str, Any]:
        """登录页引导（无敏感信息）：首次使用提示 + 免登游客是否可用。"""
        channel = handle.demo.public_channel_status("default")
        return {
            "username": "owner",
            "initial_password_pending": handle.identity_backend.initial_password_pending(),
            # U5-01：游客是默认角色——免登进入只读演示空间，不再要求访问码
            "guest": {
                "open": bool(channel["open"]),
                "published": bool(channel["published"]),
            },
        }

    @app.post("/v1/auth/login")
    async def auth_login(body: dict[str, Any]) -> dict[str, Any]:
        if handle.auth_sessions is None:
            raise HTTPException(
                status_code=501, detail={"code": "unsupported", "message": "auth sessions not configured"}
            )
        try:
            pair = handle.auth_sessions.login(
                password=str(body.get("password") or ""),
                remember=bool(body.get("remember")),
            )
        except AgentOSError as exc:
            raise _http_error(exc) from exc
        return {
            "access_token": pair.access_token,
            "expires_in": pair.access_expires_in,
            "refresh_token": pair.refresh_token,
            "refresh_expires_in": pair.refresh_expires_in,
            "token_type": "bearer",
            "subject": pair.subject,
            "role": pair.role,
        }

    @app.post("/v1/auth/refresh")
    async def auth_refresh(body: dict[str, Any]) -> dict[str, Any]:
        if handle.auth_sessions is None:
            raise HTTPException(
                status_code=501, detail={"code": "unsupported", "message": "auth sessions not configured"}
            )
        try:
            pair = handle.auth_sessions.refresh(str(body.get("refresh_token") or ""))
        except AgentOSError as exc:
            raise _http_error(exc) from exc
        return {
            "access_token": pair.access_token,
            "expires_in": pair.access_expires_in,
            "refresh_token": pair.refresh_token,
            "refresh_expires_in": pair.refresh_expires_in,
            "token_type": "bearer",
            "subject": pair.subject,
            "role": pair.role,
        }

    @app.post("/v1/auth/logout")
    async def auth_logout(body: dict[str, Any]) -> dict[str, Any]:
        if handle.auth_sessions is not None:
            handle.auth_sessions.revoke(str(body.get("refresh_token") or ""))
        return {"logged_out": True}

    @app.get("/v1/auth/me")
    async def auth_me(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        """当前会话的访问角色：admin / guest（前端据此分叉，不靠前端自报）。"""
        actor = await _actor(authorization)
        role = role_of(actor)
        info: dict[str, Any] = {
            "subject": actor.subject,
            "role": role,
            "tenant_id": actor.tenant_id,
            "expires_at": actor.expires_at.isoformat(),
            "scopes": list(actor.scopes),
        }
        if role == "guest":
            grant = handle.access.active_grant(actor)
            info["grant"] = (
                None
                if grant is None
                else {
                    "id": grant["id"],
                    "label": grant.get("label") or "",
                    "expires_at": grant.get("expires_at"),
                    "manifest_revision": grant.get("manifest_revision"),
                }
            )
            if grant is None:
                info["access_ended"] = True
        return info

    @app.post("/v1/auth/guest/bootstrap")
    async def guest_bootstrap() -> dict[str, Any]:
        """游客免登（U5-01）：无凭据换只读会话，默认角色不再要求访问码。

        服务端裁定：未开放 403、尚未发布 409、名额满 503；不签任何管理员凭据。
        """
        if handle.auth_sessions is None:
            raise HTTPException(
                status_code=501, detail={"code": "unsupported", "message": "auth sessions not configured"}
            )
        try:
            session_info = handle.demo.bootstrap_public_session("default")
            pair = handle.auth_sessions.issue_public_guest_pair(
                str(session_info["subject"]), hard_expiry=str(session_info["expires_at"])
            )
        except AgentOSError as exc:
            if exc.detail.get("reason") == "public_disabled":
                raise HTTPException(status_code=403, detail=exc.to_payload()) from exc
            raise _http_error(exc) from exc
        return {
            "access_token": pair.access_token,
            "expires_in": pair.access_expires_in,
            "refresh_token": pair.refresh_token,
            "refresh_expires_in": pair.refresh_expires_in,
            "token_type": "bearer",
            "role": pair.role,
            "subject": pair.subject,
            "manifest_revision": session_info["manifest_revision"],
            "expires_at": session_info["expires_at"],
        }

    @app.post("/v1/auth/guest/redeem")
    async def guest_redeem(body: dict[str, Any]) -> dict[str, Any]:
        """可选定向邀请：一次性兑换码 → 专用游客会话（不再是游客入口的必经步骤）。"""
        if handle.auth_sessions is None:
            raise HTTPException(
                status_code=501, detail={"code": "unsupported", "message": "auth sessions not configured"}
            )
        try:
            pair = handle.auth_sessions.redeem_invite(str(body.get("code") or ""))
        except AgentOSError as exc:
            raise _http_error(exc) from exc
        return {
            "access_token": pair.access_token,
            "expires_in": pair.access_expires_in,
            "refresh_token": pair.refresh_token,
            "refresh_expires_in": pair.refresh_expires_in,
            "token_type": "bearer",
            "role": pair.role,
            "subject": pair.subject,
        }

    # ---- U4-01/02：演示访问管理（仅管理员）与游客只读投影 ----
    @app.get("/v1/demo/public")
    async def demo_public_channel(
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """默认演示通道状态（仅管理员）：开关/发布 revision/在访游客数。"""
        actor = await _admin_actor(authorization, "demo.list")
        return handle.demo.public_channel_status(actor.tenant_id)

    @app.post("/v1/demo/public/open")
    async def demo_public_open(
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """开放免登游客入口（幂等；重新开放保留已发布 revision）。"""
        actor = await _admin_actor(authorization, "demo.publish")
        try:
            return handle.demo.set_public_open(actor, open_access=True)
        except AgentOSError as exc:
            raise _http_error(exc) from exc

    @app.post("/v1/demo/public/close")
    async def demo_public_close(
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """关闭免登游客：立即作废通道与全部访客会话（账本写不下去就不报成功）。"""
        actor = await _admin_actor(authorization, "demo.publish")
        try:
            return handle.demo.set_public_open(actor, open_access=False)
        except AgentOSError as exc:
            raise _http_error(exc) from exc

    @app.get("/v1/demo/grants")
    async def list_demo_grants(
        authorization: str | None = Header(default=None),
        kind: str | None = Query(default=None, pattern="^(invite|public)$"),
    ) -> list[dict[str, Any]]:
        actor = await _admin_actor(authorization, "demo.list")
        return handle.demo.list_grants(actor, kind=kind)

    @app.post("/v1/demo/grants", status_code=201)
    async def create_demo_grant(
        body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        actor = await _admin_actor(authorization, "demo.invite")
        try:
            return handle.demo.create_grant(
                actor,
                label=str(body.get("label") or "演示访问"),
                ttl_hours=int(body.get("ttl_hours") or 24),
            )
        except AgentOSError as exc:
            raise _http_error(exc) from exc

    @app.post("/v1/demo/void-all")
    async def void_all_demo_access(
        body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        """恢复旧备份后统一作废游客邀请与会话：已撤销的 grant 不因快照复活。"""
        actor = await _admin_actor(authorization, "demo.void_all")
        return handle.demo.void_all_for_restore(actor, reason=str(body.get("reason") or "backup_restore"))

    @app.post("/v1/demo/preview")
    async def demo_preview(body: dict[str, Any], authorization: str | None = Header(default=None)) -> dict[str, Any]:
        """发布前的游客可见内容预览（正文是否可公开由管理员确认）。"""
        actor = await _admin_actor(authorization, "demo.preview")
        try:
            return handle.demo.preview_payload(
                actor, runs=list(body.get("runs") or []), assets=list(body.get("assets") or [])
            )
        except AgentOSError as exc:
            raise _http_error(exc) from exc

    @app.post("/v1/demo/grants/{grant_id}/publish", status_code=201)
    async def publish_demo_manifest(
        grant_id: str, body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        actor = await _admin_actor(authorization, "demo.publish")
        try:
            return handle.demo.publish(
                actor,
                grant_id=grant_id,
                runs=list(body.get("runs") or []),
                assets=list(body.get("assets") or []),
                refs=list(body.get("refs") or []),
                note=str(body.get("note") or ""),
            )
        except AgentOSError as exc:
            raise _http_error(exc) from exc

    @app.post("/v1/demo/grants/{grant_id}/revoke")
    async def revoke_demo_grant(grant_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await _admin_actor(authorization, "demo.revoke")
        try:
            return handle.demo.revoke(actor, grant_id)
        except AgentOSError as exc:
            raise _http_error(exc) from exc

    @app.get("/v1/demo/home")
    async def demo_home(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await _actor(authorization)
        try:
            return handle.demo.guest_home(actor)
        except AgentOSError as exc:
            raise _http_error(exc) from exc

    @app.get("/v1/demo/runs/{run_id}")
    async def demo_run_detail(run_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await _actor(authorization)
        try:
            return handle.demo.guest_run_detail(actor, run_id)
        except AgentOSError as exc:
            raise _guest_or_404(exc) from exc

    @app.get("/v1/demo/runs/{run_id}/events")
    async def demo_run_events(
        run_id: str,
        authorization: str | None = Header(default=None),
        cursor: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        actor = await _actor(authorization)
        try:
            return handle.demo.guest_events(actor, run_id, cursor=cursor, limit=limit)
        except AgentOSError as exc:
            raise _guest_or_404(exc) from exc

    @app.get("/v1/demo/assets/{asset_id}")
    async def demo_asset_meta(asset_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await _actor(authorization)
        try:
            return handle.demo.guest_asset_meta(actor, asset_id)
        except AgentOSError as exc:
            raise _guest_or_404(exc) from exc

    @app.get("/v1/demo/assets/{asset_id}/download")
    async def demo_asset_download(asset_id: str, authorization: str | None = Header(default=None)) -> Any:
        """游客下载：鉴权 fetch→Blob 路径；逐资源在线核权，下载开关是独立判据。"""
        actor = await _actor(authorization)
        try:
            data, meta = handle.demo.guest_asset_bytes(actor, asset_id)
        except AgentOSError as exc:
            raise _guest_or_404(exc) from exc
        from fastapi.responses import Response

        return Response(
            content=data,
            media_type=str(meta.get("mime") or "application/octet-stream"),
            headers={
                "X-Asset-Sha256": str(meta.get("sha256")),
                "Cache-Control": "no-store, private",
            },
        )

    @app.get("/v1/demo/assets/{asset_id}/preview")
    async def demo_asset_preview(asset_id: str, authorization: str | None = Header(default=None)) -> Any:
        """游客预览字节：走 `demo.asset.preview` 判据，不复用下载开关。

        “能预览不等于能下载”必须在路由层也成立；未开放预览时统一 404。
        """
        actor = await _actor(authorization)
        try:
            data, meta = handle.demo.guest_asset_bytes(actor, asset_id, action="demo.asset.preview")
        except AgentOSError as exc:
            raise _guest_or_404(exc) from exc
        from fastapi.responses import Response

        return Response(
            content=data,
            media_type=str(meta.get("mime") or "application/octet-stream"),
            headers={
                "X-Asset-Sha256": str(meta.get("sha256")),
                "Cache-Control": "no-store, private",
            },
        )

    @app.get("/v1/demo/runs/{run_id}/stream")
    async def demo_run_stream(
        run_id: str,
        authorization: str | None = Header(default=None),
        cursor: int = Query(default=0, ge=0),
        follow: int = Query(default=1, ge=0, le=1),
    ) -> Any:
        """游客授权投影流（U5）：只发 `/v1/demo/*` 同一套白名单事件。

        与管理员流的区别：只读投影不含提示词/参数/内部路径；每批发送前重新核权，
        撤销或超出高水位时立即关闭。游标不越过本 revision 高水位（Q9）。
        """
        import asyncio

        from fastapi.responses import StreamingResponse

        async def _sse(event: dict[str, Any]) -> str:
            return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        actor = await _actor(authorization)
        try:
            handle.demo.guest_events(actor, run_id, cursor=cursor, limit=1)
        except AgentOSError as exc:
            raise _guest_or_404(exc) from exc

        async def _generate() -> Any:
            nonlocal cursor
            idle = 0
            while True:
                try:
                    page = handle.demo.guest_events(actor, run_id, cursor=cursor, limit=200)
                except AgentOSError:
                    yield await _sse(
                        {"kind": "access.revoked", "seq": cursor, "payload": {"reason": "demo access ended"}}
                    )
                    return
                page_cursor = int(page.get("cursor") or cursor)
                caught_up = page_cursor <= cursor
                cursor = max(cursor, page_cursor)
                events = page["events"]
                for event in events:
                    yield await _sse(event)
                if caught_up and not events:
                    run = handle.store.get_run(actor.tenant_id, run_id)
                    state = run.state.value if run else "unavailable"
                    yield await _sse({"kind": "stream.end", "seq": cursor, "payload": {"state": state, "mode": "demo"}})
                    return
                idle += 1
                if idle % 50 == 0:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(0.2)

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store, private", "X-Accel-Buffering": "no"},
        )

    @app.post("/v1/auth/password")
    async def auth_change_password(
        body: dict[str, Any],
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        creds = _credentials(authorization)
        actor = await handle.deps.identity.authenticate(creds)
        if actor.subject != "owner":
            raise HTTPException(status_code=403, detail={"code": "denied", "message": "only owner can change password"})
        new_password = str(body.get("new_password") or "")
        if not handle.identity_backend.verify_password(str(body.get("current_password") or "")):
            raise HTTPException(status_code=403, detail={"code": "denied", "message": "当前密码不正确"})
        try:
            handle.identity_backend.set_password(new_password)
        except AgentOSError as exc:
            raise _http_error(exc) from exc
        # 密码变更 → 撤销全部登录会话（要求重新登录），静态令牌不受影响
        if handle.auth_sessions is not None:
            handle.auth_sessions.revoke_all("owner")
        return {"changed": True}

    # ---- N2a：文本素材上传（粘贴/文本文件内容 → 稳定素材 ID）----
    @app.post("/v1/materials", status_code=201)
    async def upload_material(
        body: dict[str, Any],
        authorization: str | None = Header(default=None),
    ) -> Any:
        actor = await _write_actor(authorization, "material.upload")
        name = str(body.get("name") or "material.txt")[:120]
        content_text = body.get("content_text")
        if not isinstance(content_text, str):
            raise HTTPException(
                status_code=422, detail={"code": "invalid", "message": "content_text (string) is required"}
            )
        idem = body.get("idempotency_key")
        content_type = str(body.get("content_type") or "text/plain")
        try:
            result = handle.assets.register_material(
                actor,
                name=name,
                content_text=content_text,
                idempotency_key=str(idem) if idem else None,
                content_type=content_type,
            )
        except AgentOSError as exc:
            raise _http_error(exc) from exc
        status = 200 if result.get("replayed") else 201
        return JSONResponse(status_code=status, content=result)

    # ---- Asset 受控传输（远程产物必须登记为可下载引用）----
    @app.get("/v1/assets")
    async def list_assets(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
        actor = await _actor(authorization)
        handle.access.require(actor, "asset.list_all")
        return handle.assets.list(actor)  # 主体过滤 + 公开 DTO（无内部路径）

    @app.get("/v1/assets/{asset_id}")
    async def get_asset(asset_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        actor = await _actor(authorization)
        handle.access.require(actor, "asset.read", kind="asset", resource_id=asset_id)
        try:
            return handle.assets.get(actor, asset_id)
        except AgentOSError as exc:
            raise _http_error(exc) from exc

    @app.post("/v1/assets/{asset_id}/signed-url")
    async def signed_asset_url(
        asset_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """签发资源范围受限、短期的单资源下载 URL（浏览器 img/a 无法带 Authorization 头）。

        URL 只含 asset 专属签名（不含主令牌）；过期或跨资源即失效。
        """
        import hashlib
        import hmac as hmac_mod

        actor = await _write_actor(authorization, "asset.download")
        try:
            handle.assets.get(actor, asset_id)  # 属主校验
        except AgentOSError as exc:
            raise _http_error(exc) from exc
        exp = int((utc_now() + timedelta(minutes=10)).timestamp())
        msg = f"{asset_id}:{actor.subject}:{exp}"
        sig = hmac_mod.new(handle.identity_backend.signing_key().encode(), msg.encode(), hashlib.sha256).hexdigest()[
            :32
        ]
        return {
            "url": f"/v1/assets/{asset_id}/download?asset_token={sig}.{exp}",
            "expires_at": exp,
        }

    @app.get("/v1/assets/{asset_id}/download")
    async def download_asset(
        asset_id: str,
        authorization: str | None = Header(default=None),
        asset_token: str | None = Query(default=None),
    ) -> Any:
        """下载：**必须有有效身份**，再逐资源在线核权。

        U4-02 修正：旧匿名 owner 签名下载不再是私有资源入口——`asset_token` 只是
        附加的资源范围限制（asset+subject+过期），不替代身份，也不能单独授权读取。
        主令牌不接受 query 传递（N2a 已关闭该模式）。
        """
        import hashlib
        import hmac as hmac_mod

        actor = await _actor(authorization)  # 无 Bearer → 401（不再由签名兜底）
        if asset_token:
            sig, _, exp_s = asset_token.rpartition(".")
            if not sig or not exp_s.isdigit():
                raise HTTPException(status_code=403, detail={"code": "denied", "message": "invalid asset token"})
            exp = int(exp_s)
            if utc_now().timestamp() > exp:
                raise HTTPException(status_code=403, detail={"code": "denied", "message": "asset token expired"})
            # 签名覆盖面含**当前主体**：他人持同一签名也不能用（不再写死 owner）
            msg = f"{asset_id}:{actor.subject}:{exp}"
            expected = hmac_mod.new(
                handle.identity_backend.signing_key().encode(), msg.encode(), hashlib.sha256
            ).hexdigest()[:32]
            if not hmac_mod.compare_digest(sig, expected):
                handle.deps.audit.record(
                    actor.tenant_id,
                    "asset.token_mismatch",
                    subject=actor.subject,
                    detail={"asset_id": asset_id[:64]},
                )
                raise HTTPException(status_code=403, detail={"code": "denied", "message": "asset token mismatch"})
        handle.access.require(actor, "asset.download", kind="asset", resource_id=asset_id)
        try:
            data = handle.assets.read(actor, asset_id)
            meta = handle.assets.get(actor, asset_id)
        except AgentOSError as exc:
            raise _http_error(exc) from exc
        from fastapi.responses import Response

        return Response(
            content=data,
            media_type=str(meta.get("mime") or "application/octet-stream"),
            headers={
                "X-Asset-Sha256": str(meta.get("sha256")),
                "Cache-Control": "no-store, private",
            },
        )

    @app.post("/v1/nodes/{node_id}/assets/push")
    async def push_asset_to_node(
        node_id: str, request: Request, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        actor = await _write_actor(authorization, "asset.push")
        node = handle.store.get_node(actor.tenant_id, node_id)
        if node is None or node["status"] != "paired":
            raise HTTPException(status_code=403, detail={"code": "denied", "message": "node not paired"})
        data = await request.body()
        try:
            return handle.assets.push_to_node(
                actor,
                node_endpoint=str(node["endpoint"]),
                node_token=str(node["token_plain"]),
                data=data,
                filename=request.headers.get("X-Asset-Filename", "asset.bin"),
            )
        except AgentOSError as exc:
            raise _http_error(exc) from exc

    @app.post("/v1/nodes/{node_id}/assets/fetch")
    async def fetch_asset_from_node(
        node_id: str, body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        actor = await _write_actor(authorization, "asset.fetch")
        node = handle.store.get_node(actor.tenant_id, node_id)
        if node is None or node["status"] != "paired":
            raise HTTPException(status_code=403, detail={"code": "denied", "message": "node not paired"})
        try:
            result = await asyncio.to_thread(
                handle.assets.fetch_from_node,
                actor,
                node_endpoint=str(node["endpoint"]),
                node_token=str(node["token_plain"]),
                remote_ref=str(body.get("remote_ref") or ""),
                expect_sha256=str(body.get("sha256") or ""),
                mime=str(body.get("mime") or "application/octet-stream"),
                run_id=body.get("run_id"),
                source_node=node_id,
            )
            if body.get("run_id"):
                handle.store.event_append(
                    actor.tenant_id,
                    body["run_id"],
                    "asset.delivered",
                    {
                        "asset_id": result["asset_id"],
                        "node_id": node_id,
                        "sha256": result["sha256"],
                        "size": result["size"],
                        "mime": result["mime"],
                        "source": "delivery_retry",
                    },
                )
                run = handle.store.get_run(actor.tenant_id, body["run_id"])
                if (
                    run is not None
                    and run.state == RunState.PAUSED
                    and "collaboration" in json.loads(run.checkpoint_json or "{}")
                ):
                    handle.store.enqueue_job(actor.tenant_id, run.id)
            return result
        except AgentOSError as exc:
            raise _http_error(exc) from exc

    # ---- 全局控制台（静态 SPA；真实手机 PWA 验收按计划在 M1 真机执行）----
    # Wheels carry public assets; source checkouts use the same frontend sources.
    package_dir = Path(__file__).resolve().parents[1]
    frontend_dir = package_dir / "web"
    if not frontend_dir.is_dir():
        frontend_dir = package_dir.parents[1] / "frontend"
    if frontend_dir.is_dir():
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles

        # New workspace (React build) at /ui/; the previous workbench stays at /ui/legacy/.
        # Without a build (fresh source checkout) /ui/ keeps serving the previous workbench.
        app_dist = frontend_dir / "app-dist"
        if not app_dist.is_dir():
            app_dist = frontend_dir.parent / "app-dist"
        no_store = {"Cache-Control": "no-cache"}

        @app.get("/ui/legacy/", include_in_schema=False)
        async def legacy_ui() -> FileResponse:
            return FileResponse(frontend_dir / "index.html", headers=no_store)

        if (app_dist / "index.html").is_file():

            @app.get("/ui/", include_in_schema=False)
            @app.get("/ui/index.html", include_in_schema=False)
            async def workspace_ui() -> FileResponse:
                return FileResponse(app_dist / "index.html", headers=no_store)

            class ImmutableFiles(StaticFiles):
                async def get_response(self, path: str, scope: Any) -> Any:
                    response = await super().get_response(path, scope)
                    if response.status_code == 200:
                        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
                    return response

            app.mount("/ui/assets", ImmutableFiles(directory=str(app_dist / "assets")), name="ui-assets")

        app.mount("/ui", StaticFiles(directory=str(frontend_dir), html=True), name="ui")

    return app
