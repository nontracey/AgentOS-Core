"""U4-01 服务级资源授权：绕过 UI/路由直接调用服务，验证统一策略（A/R01/R02/R06）。

重点不是状态码，而是：**客户端自报字段无效、资源关系由服务端读取、拒绝即审计**。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta

import pytest

from agentos.apps.composition import SystemHandle
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import AuthContext, utc_now
from agentos.services.access import ROLE_ADMIN, AccessService, role_of
from tests.conftest import WRITE_FLOW_SCRIPT, add_subject, owner_token


def actor(handle: SystemHandle, token: str) -> AuthContext:
    return asyncio.run(handle.deps.identity.authenticate({"token": token}))


def guest_actor(handle: SystemHandle, label: str = "unit-guest") -> AuthContext:
    admin = actor(handle, owner_token(handle))
    created = handle.demo.create_grant(admin, label=label, ttl_hours=24)
    pair = handle.auth_sessions.redeem_invite(created["invite_code"])
    return asyncio.run(handle.deps.identity.authenticate({"token": pair.access_token}))


def test_role_comes_from_trusted_identity_only(make_system: Callable[..., SystemHandle]) -> None:
    handle = make_system(model_script=WRITE_FLOW_SCRIPT)
    handle.boot()
    admin = actor(handle, owner_token(handle))
    assert role_of(admin) == ROLE_ADMIN
    # 伪造：客户端自报 role/subject 不产生任何授权效力
    forged = AuthContext(
        subject="owner", tenant_id="default", scopes=["read", "write", "exec"],
        policy_version="forged", expires_at=utc_now() + timedelta(minutes=5),
        roles=[ROLE_ADMIN],
    )
    # 即便构造出同名字段，也拿不到别人的资源：资源关系仍由服务端判定
    decision = handle.access.check(forged, "task.read", kind="task", resource_id="missing-run")
    assert not decision.allowed, "资源不存在时不得因自报身份放行"
    peer = actor(handle, add_subject(handle, "peer", ["read"]))
    assert role_of(peer) == "member"
    assert not handle.access.check(peer, "demo.publish").allowed


def test_guest_write_denied_at_service_layer(make_system: Callable[..., SystemHandle]) -> None:
    handle = make_system(model_script=WRITE_FLOW_SCRIPT)
    handle.boot()
    guest = guest_actor(handle)
    for action in ("task.create", "task.cancel", "approval.decide", "material.upload",
                   "workflow.start", "node.pair", "tool.call", "demo.publish"):
        with pytest.raises(AgentOSError) as exc:
            handle.access.require(guest, action)
        assert exc.value.code is ErrorCode.DENIED
    # 拒绝必须留痕（审计记录存在且不含被拒资源正文）
    audit = handle.store.audit_query("default", kind_prefix="access.denied", limit=50)
    assert audit and any(a.subject == guest.subject for a in audit)


def test_guest_resource_scope_is_per_resource_not_per_task(
    make_system: Callable[..., SystemHandle],
) -> None:
    """一条任务 ≠ 其关联资源已授权：run 授权不扩散到 asset/其他 run。"""
    handle = make_system(model_script=WRITE_FLOW_SCRIPT)
    handle.boot()
    admin = actor(handle, owner_token(handle))
    receipt = asyncio.run(
        handle.ingress.accept({"payload": {"goal": "unit goal"}}, channel="cli",
                              credentials={"token": owner_token(handle)})
    )
    run_id = receipt.run_id
    other = asyncio.run(
        handle.ingress.accept({"payload": {"goal": "private goal"}}, channel="cli",
                              credentials={"token": owner_token(handle)})
    ).run_id
    # Q6：默认发布只接受已完成任务——服务级测试无 worker（gateway lifespan 才启动），
    # 直接经 store 把 run 推到终态
    from agentos.kernel.states import RUN_TERMINAL, RunState

    handle.store.transition_run("default", run_id, set(RunState) - RUN_TERMINAL,
                                RunState.SUCCEEDED)
    guest = guest_actor(handle)
    handle.demo.publish(admin, grant_id=handle.access.active_grant(guest)["id"],
                        runs=[{"run_id": run_id}])

    assert handle.access.check(guest, "task.read", kind="task", resource_id=run_id).allowed
    assert not handle.access.check(guest, "task.read", kind="task", resource_id=other).allowed
    # 任何全局读取面（检索/记忆/清单/审计/配置）对游客关闭
    for action in ("knowledge.search", "memory.read", "audit.read", "skill.list", "node.list"):
        assert not handle.access.check(guest, action).allowed


def test_admin_and_member_boundaries(make_system: Callable[..., SystemHandle]) -> None:
    handle = make_system(model_script=WRITE_FLOW_SCRIPT)
    handle.boot()
    admin = actor(handle, owner_token(handle))
    receipt = asyncio.run(
        handle.ingress.accept({"payload": {"goal": "admin goal"}}, channel="cli",
                              credentials={"token": owner_token(handle)})
    )
    assert handle.access.check(admin, "task.read", kind="task", resource_id=receipt.run_id).allowed
    # 同租户第二主体不能读他人任务（归属由服务端判定）
    peer = actor(handle, add_subject(handle, "peer", ["read"]))
    assert not handle.access.check(peer, "task.read", kind="task", resource_id=receipt.run_id).allowed
    # 另一 tenant 的管理员也不跨租户
    other_tenant = AuthContext(
        subject="owner", tenant_id="tenant-x", scopes=["read", "write"],
        policy_version="p", expires_at=utc_now() + timedelta(minutes=5), roles=[ROLE_ADMIN],
    )
    assert not handle.access.check(
        other_tenant, "task.read", kind="task", resource_id=receipt.run_id).allowed


def test_expired_and_revoked_guest_lose_access(
    make_system: Callable[..., SystemHandle],
) -> None:
    handle = make_system(model_script=WRITE_FLOW_SCRIPT)
    handle.boot()
    admin = actor(handle, owner_token(handle))
    created = handle.demo.create_grant(admin, label="short", ttl_hours=24)
    pair = handle.auth_sessions.redeem_invite(created["invite_code"])
    guest = asyncio.run(handle.deps.identity.authenticate({"token": pair.access_token}))
    assert handle.access.active_grant(guest) is not None
    handle.demo.revoke(admin, created["grant_id"])
    # 撤销后：主体层已失效，重新认证即失败
    with pytest.raises(AgentOSError):
        asyncio.run(handle.deps.identity.authenticate({"token": pair.access_token}))
    # 会话层也失效：refresh 不复活
    with pytest.raises(AgentOSError):
        handle.auth_sessions.refresh(pair.refresh_token)


def test_project_whitelist_drops_unlisted_fields() -> None:
    row = {"id": "a1", "name": "n", "path": "/private/x", "sha256": "abc", "size": 3}
    out = AccessService.project(row, ["id", "name", "sha256"])
    assert set(out) == {"id", "name", "sha256"}
    assert "path" not in out
    # 模型类事件整条丢弃：其 payload 含系统指令与用户原文，按"删敏感键"过滤不可靠
    prompt_event = {"seq": 1, "kind": "model.request", "created_at": "t", "run_id": "r",
                    "payload": {"messages": [{"role": "system", "content": "SECRET-INSTRUCTIONS"}],
                                "endpoint": "http://internal"}}
    assert AccessService.public_event(prompt_event) is None
    prompt_json = {"seq": 1, "kind": "model.request", "created_at": "t", "run_id": "r",
                   "payload_json": '{"messages":[{"role":"system","content":"SECRET"}]}'}
    assert AccessService.public_event(prompt_json) is None
    # 逐事件字段白名单（R-A）：只保留点名字段；普通新键与嵌套列表整体丢弃
    event = {"seq": 2, "kind": "tool.prepared", "created_at": "t", "run_id": "r",
             "payload": {"tool": "fs.write_text", "policy": "allowed",
                         "path": "/private/x", "endpoint": "http://x", "token": "t",
                         "note": "SYNTH_PRIVATE_MARKER", "items": ["SYNTH_PRIVATE_MARKER"]}}
    safe = AccessService.public_event(event)
    payload = str(safe["payload"])
    assert "path" not in payload and "endpoint" not in payload and "token" not in payload
    assert "SYNTH_PRIVATE_MARKER" not in payload, "任意新键与数组内文本不得透传"
    assert "fs.write_text" in payload and "allowed" in payload


def test_answer_stream_hidden_by_default_and_gated_by_live() -> None:
    """R-A：正文流默认不进游客投影；直播须显式开启且仍只放行点名字段。"""
    delta = {"seq": 3, "kind": "answer.delta", "created_at": "t", "run_id": "r",
             "payload": {"call_id": "c1", "delta": "未审阅正文增量", "seq_in_call": 1}}
    assert AccessService.public_event(delta) is None, "默认发布快照不含正文流"
    live_view = AccessService.public_event(delta, live=True)
    assert live_view is not None
    assert live_view["payload"]["delta"] == "未审阅正文增量"
    assert "call_id" not in live_view["payload"], "直播也只放行点名字段"
    # start/end 与状态事件的非点名 detail 不下发
    start = {"seq": 4, "kind": "answer.start", "created_at": "t", "run_id": "r",
             "payload": {"call_id": "c1", "run_id": "r"}}
    assert AccessService.public_event(start) is None
    assert AccessService.public_event(start, live=True)["payload"] == {}


def test_state_event_detail_never_reaches_guest_projection() -> None:
    """run.state_changed 的 event_detail（final/delivery）是未发布内容：不下发。"""
    event = {"seq": 5, "kind": "run.state_changed", "created_at": "t", "run_id": "r",
             "payload": {"to": "succeeded", "final": "SYNTH_PRIVATE_MARKER",
                         "delivery": {"asset_id": "a1"}, "tags": ["SYNTH_PRIVATE_MARKER"]}}
    out = AccessService.public_event(event)
    assert out is not None
    assert out["payload"] == {"to": "succeeded"}
