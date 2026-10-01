"""N3a 资料报告模板验收（计划 §4 N3a / §2.3 P1~P3）：

当前环境无真实模型端点（N0 已记录）——用 scripted 模型按目标分组重放，
验收**机制级闭环**：模板步骤真实执行、材料经工具+授权读取、结构/引用检查
分别记录、不可变 Asset 登记与版本链、材料/目标改变结果、引用越权拒绝。
真实模型产物级验收在具备端点时以同一套判据复跑（AGENTOS_MODEL_PROVIDER=openai_compat）。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from fastapi.testclient import TestClient

from agentos.apps.composition import SystemHandle
from agentos.apps.gateway import create_app
from tests.conftest import add_subject, owner_token

P1_REPORT = (
    "# 项目甲进展报告\n\n## 摘要\n项目甲 3 个任务已完成 2 个（S1 §2）。\n\n"
    "## 关键结论\n- 完成进度 2/3（S1 §2）。\n- 剩余任务阻塞于输入缺失（S1 §3）。\n\n"
    "## 证据与引用\n| 结论 | 来源 |\n|---|---|\n| 2/3 完成 | S1 §2 |\n| 阻塞 | S1 §3 |\n\n"
    "## 未决项\n- 接口清单提供时间（材料未说明）。\n"
)
P2_REPORT = (
    "# 项目甲进展合并报告\n\n## 摘要\nS1 称 2/3 完成；S2 复核仅 1/3，两材料冲突。\n\n"
    "## 关键结论\n- S1 §2：2/3 完成。\n- S2 §1：仅 1/3，未提供取代依据。\n\n"
    "## 证据与引用\n| 结论 | 来源 |\n|---|---|\n| 2/3 | S1 §2 |\n| 1/3 | S2 §1 |\n\n"
    "## 未决项\n- 两份口径冲突，待确认权威版本。\n"
)
P3_REPORT = (
    "# 项目甲投入与进度报告\n\n## 摘要\n材料不足以回答投入金额与精确完成日期。\n\n"
    "## 关键结论\n- 进度 2/3（S1 §2）。\n- 投入金额：材料不足，不编造。\n"
    "- 完成日期：材料不足，不编造。\n\n"
    "## 证据与引用\n| 结论 | 来源 |\n|---|---|\n| 2/3 完成 | S1 §2 |\n\n"
    "## 未决项\n- 投入金额（材料未给出）。\n- 精确完成日期（材料未给出）。\n"
)
FABRICATED_REPORT = (
    "# 报告\n\n## 摘要\n结论见 S9。\n\n## 关键结论\n- 金额 100 万（S9 §1）。\n\n"
    "## 证据与引用\n| 结论 | 来源 |\n|---|---|\n| 金额 | S9 §1 |\n\n## 未决项\n无\n"
)

P2_REPORT_CONSERVATIVE = (
    "# 项目甲进展报告（保守口径）\n\n## 摘要\n采用保守口径：确认 2/3 完成（S1 §2）。\n\n"
    "## 关键结论\n- 进度 2/3（S1 §2）。\n\n"
    "## 证据与引用\n| 结论 | 来源 |\n|---|---|\n| 2/3 完成 | S1 §2 |\n\n"
    "## 未决项\n无新增。\n"
)

TEMPLATE_SCRIPT = [
    {"when_user_contains": "根据材料写进展报告与下一步（P1）", "script": [{"content": P1_REPORT}]},
    {"when_user_contains": "合并两份材料并说明冲突（P2）", "script": [{"content": P2_REPORT}]},
    {"when_user_contains": "说明项目投入金额与精确完成日期（P3）", "script": [{"content": P3_REPORT}]},
    {"when_user_contains": "越权引用测试", "script": [{"content": FABRICATED_REPORT}]},
    {"when_user_contains": "将结论改为保守口径（P1）", "script": [{"content": P2_REPORT_CONSERVATIVE}]},
]


@contextmanager
def client(make_system: Callable[..., SystemHandle], **kw: Any) -> Iterator[Any]:
    handle = make_system(model_script=kw.pop("script", TEMPLATE_SCRIPT), **kw)
    app = create_app(system=handle)
    with TestClient(app) as c:
        yield c, handle, owner_token(handle)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def wait_state(c: TestClient, token: str, run_id: str, states: set[str], timeout: float = 10) -> str:
    deadline = time.monotonic() + timeout
    state = ""
    while time.monotonic() < deadline:
        state = c.get(f"/v1/tasks/{run_id}", headers=auth(token)).json()["run"]["state"]
        if state in states:
            return state
        time.sleep(0.05)
    return state


def upload(c: TestClient, token: str, text: str, name: str = "S1.txt") -> str:
    r = c.post("/v1/materials", json={"name": name, "content_text": text}, headers=auth(token))
    assert r.status_code in (200, 201), r.text
    return r.json()["asset_id"]


def run_template(c: TestClient, token: str, goal: str, material_ids: list[str],
                 key: str, parent: str | None = None, reason: str = "") -> str:
    payload: dict[str, Any] = {
        "payload": {"goal": goal, "template": "material-report"},
        "material_asset_ids": material_ids,
        "idempotency_key": key,
    }
    if parent:
        payload["payload"]["parent_asset_id"] = parent
    if reason:
        payload["payload"]["revision_reason"] = reason
    r = c.post("/v1/tasks", json=payload, headers=auth(token))
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]
    state = wait_state(c, token, run_id, {"awaiting_approval", "succeeded", "failed", "paused"})
    if state == "awaiting_approval":
        d = c.get(f"/v1/tasks/{run_id}", headers=auth(token)).json()
        apv = d["approvals"][0]
        rd = c.post(f"/v1/approvals/{apv['id']}/decision",
                    json={"decision": "approve"}, headers=auth(token))
        assert rd.status_code == 200, rd.text
        state = wait_state(c, token, run_id, {"succeeded", "failed", "paused"})
    assert state == "succeeded", f"run {run_id} state={state}"
    return run_id


def test_p1_single_material_report(make_system: Callable[..., SystemHandle]) -> None:
    with client(make_system) as (c, handle, token):
        s1 = upload(c, token, "S1 内容：2/3 完成，阻塞于输入缺失。", name="S1_项目甲计划.txt")
        run_id = run_template(c, token, "根据材料写进展报告与下一步（P1）", [s1], "p1")
        d = c.get(f"/v1/tasks/{run_id}", headers=auth(token)).json()
        arts = d["artifacts"]
        assert len(arts) == 1 and arts[0]["run_id"] == run_id
        assert arts[0]["version"] == 1 and arts[0]["mime"] == "text/markdown"
        # 下载内容与登记一致
        dl = c.get(f"/v1/assets/{arts[0]['id']}/download", headers=auth(token))
        assert dl.status_code == 200
        assert dl.headers["X-Asset-Sha256"] == arts[0]["sha256"]
        assert "2/3" in dl.text and "S1" in dl.text
        # 检查分别记录且通过
        ev = c.get(f"/v1/tasks/{run_id}/events?limit=200", headers=auth(token)).json()["events"]
        kinds = {e["kind"] for e in ev}
        assert "report.structure_check" in kinds and "report.citation_check" in kinds
        struct = next(e for e in ev if e["kind"] == "report.structure_check")
        cite = next(e for e in ev if e["kind"] == "report.citation_check")
        assert struct["payload"]["passed"] and cite["payload"]["passed"]
        # 步骤留痕：材料读取 + 写盘
        step_ids = [s["id"] for s in d["steps"]]
        assert any("tpl1_read" in s for s in step_ids)
        assert any("tpl3_write" in s for s in step_ids)


def test_p2_conflict_report_differs_by_materials(
    make_system: Callable[..., SystemHandle],
) -> None:
    """目标/材料确实改变结果：P2 双材料报告引用 S1+S2 且内容与 P1 不同。"""
    with client(make_system) as (c, handle, token):
        s1 = upload(c, token, "S1：2/3 完成", name="S1.txt")
        s2 = upload(c, token, "S2：最新复核仅 1/3，未提供取代旧版依据", name="S2.txt")
        run_id = run_template(c, token, "合并两份材料并说明冲突（P2）", [s1, s2], "p2")
        art2 = c.get(f"/v1/tasks/{run_id}", headers=auth(token)).json()["artifacts"][0]
        text = c.get(f"/v1/assets/{art2['id']}/download", headers=auth(token)).text
        assert "S1" in text and "S2" in text and "冲突" in text
        # 与 P1 内容不同（同一模板、不同材料 → 不同结果）
        with client(make_system) as (c2, _h2, t2):
            s1b = upload(c2, t2, "S1：2/3 完成", name="S1.txt")
            run1 = run_template(c2, t2, "根据材料写进展报告与下一步（P1）", [s1b], "p1b")
            art1 = c2.get(f"/v1/tasks/{run1}", headers=auth(t2)).json()["artifacts"][0]
            text1 = c2.get(f"/v1/assets/{art1['id']}/download", headers=auth(t2)).text
            assert text1 != text


def test_p3_insufficient_material_no_fabrication(
    make_system: Callable[..., SystemHandle],
) -> None:
    with client(make_system) as (c, handle, token):
        s1 = upload(c, token, "S1：只有进度信息", name="S1.txt")
        run_id = run_template(c, token, "说明项目投入金额与精确完成日期（P3）", [s1], "p3")
        art = c.get(f"/v1/tasks/{run_id}", headers=auth(token)).json()["artifacts"][0]
        dl = c.get(f"/v1/assets/{art['id']}/download", headers=auth(token))
        assert "材料不足" in dl.text
        assert "100 万" not in dl.text  # 不编造


def test_citation_outside_authorized_fails_run(
    make_system: Callable[..., SystemHandle],
) -> None:
    with client(make_system) as (c, handle, token):
        s1 = upload(c, token, "S1：正常材料", name="S1.txt")
        payload = {
            "payload": {"goal": "越权引用测试", "template": "material-report"},
            "material_asset_ids": [s1], "idempotency_key": "fab1",
        }
        r = c.post("/v1/tasks", json=payload, headers=auth(token))
        run_id = r.json()["run_id"]
        state = wait_state(c, token, run_id, {"succeeded", "failed", "awaiting_approval", "paused"})
        if state == "awaiting_approval":  # 写盘先于检查；批准后检查失败
            d = c.get(f"/v1/tasks/{run_id}", headers=auth(token)).json()
            c.post(f"/v1/approvals/{d['approvals'][0]['id']}/decision",
                   json={"decision": "approve"}, headers=auth(token))
            state = wait_state(c, token, run_id, {"succeeded", "failed", "paused"})
        assert state == "failed"
        d = c.get(f"/v1/tasks/{run_id}", headers=auth(token)).json()
        assert d["artifacts"] == []  # 检查失败 → 不登记成果
        assert "citation" in (d["error"] or {}).get("message", "")


def test_revision_creates_new_version_old_kept(
    make_system: Callable[..., SystemHandle],
) -> None:
    with client(make_system) as (c, handle, token):
        s1 = upload(c, token, "S1：2/3 完成", name="S1.txt")
        v1_run = run_template(c, token, "根据材料写进展报告与下一步（P1）", [s1], "rev1")
        v1_asset = c.get(f"/v1/tasks/{v1_run}", headers=auth(token)).json()["artifacts"][0]
        # 修改：新 run + parent + reason → 新版本
        v2_run = run_template(c, token, "将结论改为保守口径（P1）", [s1], "rev2",
                              parent=v1_asset["id"], reason="将结论改为保守口径")
        assert v2_run != v1_run
        d2 = c.get(f"/v1/tasks/{v2_run}", headers=auth(token)).json()
        v2_asset = d2["artifacts"][0]
        assert v2_asset["version"] == 2
        assert v2_asset["parent_asset_id"] == v1_asset["id"]
        assert v2_asset["revision_reason"] == "将结论改为保守口径"
        # 旧成果仍可访问且内容不变
        old = c.get(f"/v1/assets/{v1_asset['id']}", headers=auth(token)).json()
        assert old["version"] == 1 and old["sha256"] == v1_asset["sha256"]


def test_material_read_outside_run_denied(make_system: Callable[..., SystemHandle]) -> None:
    """模板材料读取的越权面：run 只能读受理时登记的材料。"""
    with client(make_system) as (c, handle, token):

        from agentos.kernel.errors import AgentOSError
        from agentos.kernel.types import AuthContext

        s1 = upload(c, token, "S1：授权材料", name="S1.txt")
        other = upload(c, token, "其他材料：未授权给本 run", name="SX.txt")
        run_id = run_template(c, token, "根据材料写进展报告与下一步（P1）", [s1], "auth1")
        # owner actor（与模板执行器同源构造），直接调用服务端读取边界
        env = json.loads(handle.store.get_run("default", run_id).envelope_json)
        actor = AuthContext.model_validate(env["actor"])
        assets = handle.assets
        # 本 run 授权材料可读；未登记进 run 的材料拒绝
        ok = assets.read_run_material(actor, run_id, s1)
        assert "S1" in ok["content"]
        try:
            assets.read_run_material(actor, run_id, other)
            raise AssertionError("expected DENIED")
        except AgentOSError as exc:
            assert "not authorized" in exc.message


def test_template_cross_subject_isolated(make_system: Callable[..., SystemHandle]) -> None:
    """第二主体不能引用他人材料跑模板（继承 N2a 边界）。"""
    with client(make_system) as (c, handle, token):
        helper = add_subject(handle, "helper", ["read", "write", "exec", "approve"])
        s1 = upload(c, token, "S1：owner 材料", name="S1.txt")
        r = c.post("/v1/tasks", json={
            "payload": {"goal": "进展报告与下一步（helper 尝试）", "template": "material-report"},
            "material_asset_ids": [s1], "idempotency_key": "hx1",
        }, headers=auth(helper))
        assert r.status_code in (401, 403)


def test_runs_history_filters(make_system: Callable[..., SystemHandle]) -> None:
    """N3b：本人历史按状态/目标子串过滤。"""
    with client(make_system) as (c, handle, token):
        s1 = upload(c, token, "S1：2/3 完成", name="S1.txt")
        r1 = run_template(c, token, "根据材料写进展报告与下一步（P1）", [s1], "f1")
        r2 = c.post("/v1/tasks", json={"payload": {"goal": "普通任务没有材料"},
                                       "idempotency_key": "f2"}, headers=auth(token))
        wait_state(c, token, r2.json()["run_id"], {"succeeded", "failed", "awaiting_approval"})
        # 状态过滤
        ok = c.get("/v1/runs?state=succeeded", headers=auth(token)).json()
        assert all(x["state"] == "succeeded" for x in ok) and ok
        # 目标子串过滤（大小写不敏感）
        q = c.get("/v1/runs?q=进展报告", headers=auth(token)).json()
        assert q and all("进展报告" in x["goal"] for x in q)
        assert any(x["id"] == r1 for x in q)
