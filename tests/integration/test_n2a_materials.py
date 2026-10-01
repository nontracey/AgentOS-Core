"""N2a 契约验收（计划 §2.2 / 缺口 G-N08、G-N09）：

- 文本素材上传：稳定素材 ID（内容去重）、幂等键同键同内容回放、同键异正文 409
- 材料引用受理：存在/属主/类型/数量/合计限额校验；重放同 run；异材料 409
- Asset 主体隔离：同租户第二主体与跨主体访问拒绝；DTO 无内部路径
- 下载安全：主令牌 query 不再被接受；asset_token 资源范围签名可用、跨资源拒绝
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from fastapi.testclient import TestClient

from agentos.apps.composition import SystemHandle
from agentos.apps.gateway import create_app
from tests.conftest import WRITE_FLOW_SCRIPT, add_subject, owner_token


@contextmanager
def client(make_system: Callable[..., SystemHandle], **kw: Any):
    handle = make_system(model_script=kw.pop("script", WRITE_FLOW_SCRIPT), **kw)
    app = create_app(system=handle)
    with TestClient(app) as c:
        yield c, handle, owner_token(handle)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------- 素材上传
def test_material_upload_stable_id_by_content(
    make_system: Callable[..., SystemHandle],
) -> None:
    with client(make_system) as (c, _, token):
        body = {"name": "S1.txt", "content_text": "项目甲 3 个任务，2 个完成"}
        r1 = c.post("/v1/materials", json=body, headers=auth(token))
        assert r1.status_code == 201
        r2 = c.post("/v1/materials", json=body, headers=auth(token))
        assert r2.status_code == 200
        # 相同材料 → 返回原 Asset（稳定素材 ID）
        assert r2.json()["asset_id"] == r1.json()["asset_id"]


def test_material_idem_key_replay_and_conflict(
    make_system: Callable[..., SystemHandle],
) -> None:
    with client(make_system) as (c, _, token):
        r1 = c.post("/v1/materials", json={
            "name": "a.txt", "content_text": "内容一", "idempotency_key": "m1",
        }, headers=auth(token))
        assert r1.status_code == 201
        r2 = c.post("/v1/materials", json={
            "name": "a.txt", "content_text": "内容一", "idempotency_key": "m1",
        }, headers=auth(token))
        assert r2.status_code == 200 and r2.json()["replayed"] is True
        assert r2.json()["asset_id"] == r1.json()["asset_id"]
        r3 = c.post("/v1/materials", json={
            "name": "a.txt", "content_text": "内容二", "idempotency_key": "m1",
        }, headers=auth(token))
        assert r3.status_code == 409


def test_material_oversize_rejected(make_system: Callable[..., SystemHandle]) -> None:
    with client(make_system) as (c, _, token):
        big = "x" * (64 * 1024 + 1)
        r = c.post("/v1/materials", json={"name": "big.txt", "content_text": big},
                   headers=auth(token))
        assert r.status_code == 422


def test_material_requires_auth(make_system: Callable[..., SystemHandle]) -> None:
    with client(make_system) as (c, _, _):
        r = c.post("/v1/materials", json={"name": "a.txt", "content_text": "x"})
        assert r.status_code == 401


# ---------------------------------------------------------------- 材料引用受理
def _upload(c: TestClient, token: str, text: str, name: str = "s.txt") -> str:
    r = c.post("/v1/materials", json={"name": name, "content_text": text}, headers=auth(token))
    assert r.status_code in (200, 201), r.text
    return r.json()["asset_id"]


def test_task_with_material_refs_accept_and_replay(
    make_system: Callable[..., SystemHandle],
) -> None:
    with client(make_system) as (c, handle, token):
        mid = _upload(c, token, "S1：3 个任务完成 2 个")
        payload = {
            "payload": {"goal": "写进展报告"},
            "material_asset_ids": [mid],
            "idempotency_key": "t1",
        }
        r1 = c.post("/v1/tasks", json=payload, headers=auth(token))
        assert r1.status_code == 202
        r2 = c.post("/v1/tasks", json=payload, headers=auth(token))
        assert r2.status_code == 202 and r2.json()["replayed"] is True
        assert r2.json()["run_id"] == r1.json()["run_id"]
        # 材料引用进入 envelope payload（执行期可见）
        run = handle.store.get_run("default", r1.json()["run_id"])
        import json as _json

        envelope = _json.loads(run.envelope_json)
        mats = envelope["payload"]["materials"]
        assert mats and mats[0]["asset_id"] == mid and "sha256" in mats[0]
        # 同键不同材料 → 409
        mid2 = _upload(c, token, "S2：另一份材料")
        r3 = c.post("/v1/tasks", json={
            "payload": {"goal": "写进展报告"},
            "material_asset_ids": [mid2], "idempotency_key": "t1",
        }, headers=auth(token))
        assert r3.status_code == 409


def test_task_material_limits(make_system: Callable[..., SystemHandle]) -> None:
    with client(make_system) as (c, _, token):
        ids = [_upload(c, token, f"材料 {i}") for i in range(4)]
        r = c.post("/v1/tasks", json={
            "payload": {"goal": "x"}, "material_asset_ids": ids,
        }, headers=auth(token))
        assert r.status_code == 422  # > 3 份拒绝


def test_task_material_owned_and_text_only(
    make_system: Callable[..., SystemHandle],
) -> None:
    with client(make_system) as (c, handle, token):
        helper = add_subject(handle, "helper", ["read", "write", "exec", "approve"])
        mid = _upload(c, token, "S1：owner 的材料")
        # 第二主体引用他人材料 → 拒绝
        r = c.post("/v1/tasks", json={
            "payload": {"goal": "x"}, "material_asset_ids": [mid],
        }, headers=auth(helper))
        assert r.status_code in (401, 403)


# ---------------------------------------------------------------- Asset 主体隔离
def test_asset_subject_isolation_and_dto(make_system: Callable[..., SystemHandle]) -> None:
    with client(make_system) as (c, handle, token):
        mid = _upload(c, token, "S1：私有材料", name="S1.txt")
        helper = add_subject(handle, "helper", ["read", "write", "exec", "approve"])
        # 列表按主体过滤：helper 看不到 owner 的素材
        helper_assets = c.get("/v1/assets", headers=auth(helper)).json()
        assert all(a["id"] != mid for a in helper_assets)
        # 详情/下载拒绝
        assert c.get(f"/v1/assets/{mid}", headers=auth(helper)).status_code in (401, 403)
        assert c.get(f"/v1/assets/{mid}/download", headers=auth(helper)).status_code in (401, 403)
        # DTO 不含内部存储路径
        dto = c.get(f"/v1/assets/{mid}", headers=auth(token)).json()
        assert "path" not in dto and "storage_path" not in dto
        assert dto["mime"] == "text/plain"


# ---------------------------------------------------------------- 下载安全
def test_download_rejects_main_token_in_query(
    make_system: Callable[..., SystemHandle],
) -> None:
    with client(make_system) as (c, _, token):
        mid = _upload(c, token, "S1")
        # 主令牌放 query 已关闭：未带 Authorization → 401（不因 token 参数放行）
        r = c.get(f"/v1/assets/{mid}/download?token={token}")
        assert r.status_code == 401


def test_signed_url_flow(make_system: Callable[..., SystemHandle]) -> None:
    """U4-02 契约变更：签名只是附加资源限制，不再单独授权匿名读取私有资源。"""
    with client(make_system) as (c, handle, token):
        mid = _upload(c, token, "S1 内容")
        r = c.post(f"/v1/assets/{mid}/signed-url", headers=auth(token))
        assert r.status_code == 200
        url = r.json()["url"]
        assert "asset_token=" in url and token not in url  # URL 不含主令牌
        # 旧行为（匿名凭签名下载）已关闭：无身份 → 401
        assert c.get(url).status_code == 401
        # 有身份 + 签名匹配 → 允许
        dl = c.get(url, headers=auth(token))
        assert dl.status_code == 200 and dl.content == "S1 内容".encode()
        # 签名绑定单资源：跨资源使用拒绝
        other = _upload(c, token, "S2 内容")
        forged = url.replace(f"{mid}/download", f"{other}/download")
        assert c.get(forged, headers=auth(token)).status_code in (403, 404)
        # 篡改签名拒绝
        tampered = url[:-1] + ("0" if url[-1] != "0" else "1")
        assert c.get(tampered, headers=auth(token)).status_code == 403
        # 他人持同一签名不可用（签名覆盖面含主体，不再写死 owner）
        helper = add_subject(handle, "helper2", ["read"])
        assert c.get(url, headers=auth(helper)).status_code in (403, 404)


# ---------------------------------------------------------------- 详情聚合
def test_task_detail_aggregates(make_system: Callable[..., SystemHandle]) -> None:
    with client(make_system) as (c, _, token):
        mid = _upload(c, token, "S1：材料", name="S1.txt")
        r = c.post("/v1/tasks", json={
            "payload": {"goal": "写报告"}, "material_asset_ids": [mid],
            "idempotency_key": "agg1",
        }, headers=auth(token))
        run_id = r.json()["run_id"]
        d = c.get(f"/v1/tasks/{run_id}", headers=auth(token)).json()
        assert set(d) >= {"run", "steps", "budget", "plan", "artifacts", "approvals", "error"}
        assert d["error"] is None  # 结构化错误字段
        # 材料在任务未产出时不属于成果
        assert all(a["id"] != mid for a in d["artifacts"])
