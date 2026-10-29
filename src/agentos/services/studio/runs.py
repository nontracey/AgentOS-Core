"""Read studio workflow runs back as jobs and takes.

Every generation request is an ordinary published workflow run. Its graph nodes carry a small
``studio`` description (what the node makes and for which target); the run's checkpoint,
ledger steps and events say how far it got. Nothing here writes: jobs and takes are views of
runs the control plane already records, and settled runs are cached because they no longer
change.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from agentos.kernel.states import RUN_TERMINAL, RunState, StepState
from agentos.kernel.store import RunRecord, StepRecord, StoreProtocol

PREFIX = "studio_"


def workflow_prefix(subject: str, project_id: str) -> str:
    """Workflow ids of one subject's project; definitions are tenant-wide, so the subject is hashed in."""
    return PREFIX + hashlib.sha256(f"{subject}\0{project_id}".encode()).hexdigest()[:24] + "_"


def friendly_error(error: dict[str, Any] | None) -> str:
    """Short, actionable text for a failed or unsettled node; raw codes stay in the task trace."""
    if not error:
        return "失败"
    code = str(error.get("code") or "")
    message = str(error.get("message") or "")
    lowered = message.lower()
    if "node unavailable" in lowered or "unreachable" in lowered or "transfer unavailable" in lowered:
        reason = "连不上生成节点：确认节点已开机、节点服务在运行且网络可达"
    elif "node not paired" in lowered:
        reason = "还没有配对生成节点"
    elif "token rejected" in lowered:
        reason = "生成节点拒绝了配对令牌，请重新配对节点"
    elif "execute failed" in lowered:
        reason = "生成节点执行出错（" + message.rsplit(":", 1)[-1].strip()[:40] + "），请查看节点日志"
    elif code == "timeout" or "timeout" in lowered:
        reason = "生成节点没有及时回复"
    elif "arguments invalid" in lowered:
        reason = "参数不符合节点工具要求：" + message.split(":", 1)[-1].strip()[:160]
    elif "rejected" in lowered and code == "denied":
        reason = "已拒绝执行"
    elif "value" in error:
        value = error.get("value")
        detail = (value.get("error") or value.get("message")) if isinstance(value, dict) else ""
        reason = f"生成节点报告失败（{code or 'failed'}）" + (f"：{str(detail)[:160]}" if detail else "")
    else:
        reason = message[:200] or code or "失败"
    if error.get("needs_reconciliation") or code == "uncertain":
        reason += "。节点上可能已经开始生成：恢复后点“核对并继续”接回同一结果，或取消这一批"
    return reason


@dataclass
class NodeView:
    run_id: str
    node_id: str
    kind: str
    slot: str
    target: str
    engine: str
    label: str
    estimate: float
    meta: dict[str, Any]
    request: dict[str, Any]
    is_tool: bool
    state: str = "queued"      # queued running approval blocked succeeded failed cancelled skipped
    error: str = ""
    output: dict[str, Any] | None = None
    step_id: str = ""
    approval_id: str = ""
    started_at: str = ""
    finished_at: str = ""

    @property
    def id(self) -> str:
        return f"{self.run_id}.{self.node_id}"


@dataclass
class RunView:
    tenant: str
    subject: str
    run_id: str
    project_id: str
    workflow_id: str
    state: str
    created_at: str
    nodes: list[NodeView] = field(default_factory=list)

    @property
    def settled(self) -> bool:
        return RunState(self.state) in RUN_TERMINAL


def _iso(value: Any) -> str:
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else str(value or "")


def _load(text: str | None) -> dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


class StudioRuns:
    """Run index per subject project plus parsed views; settled runs are parsed once."""

    def __init__(self, store: StoreProtocol) -> None:
        self.store = store
        self._settled: dict[tuple[str, str], RunView] = {}
        self.running: dict[tuple[str, str], RunView] = {}   # last view of each unsettled run

    def run_ids(self, tenant: str, subject: str, project_id: str,
                rows: list[dict[str, Any]] | None = None) -> list[str]:
        prefix = workflow_prefix(subject, project_id)
        return [str(row["internal_run_id"]) for row in (self.store.list_workflow_runs(tenant) if rows is None
                                                         else rows)
                if str(row.get("workflow_id", "")).startswith(prefix) and row.get("internal_run_id")]

    def view(self, tenant: str, run_id: str, *, progress: dict[str, dict[str, Any]] | None = None,
             accelerator: dict[str, Any] | None = None) -> RunView | None:
        key = (tenant, run_id)
        if key in self._settled:
            return self._settled[key]
        run = self.store.get_run(tenant, run_id)
        if run is None:
            return None
        parsed = self._parse(run, progress or {}, accelerator or {})
        if parsed is None:
            return None
        if parsed.settled:
            self._settled[key] = parsed
            self.running.pop(key, None)
        else:
            self.running[key] = parsed
        return parsed

    def project_runs(self, tenant: str, subject: str, project_id: str, *,
                     rows: list[dict[str, Any]] | None = None, **context: Any) -> list[RunView]:
        """Newest first; ``rows`` reuses one workflow-run listing across projects."""
        views = []
        for run_id in self.run_ids(tenant, subject, project_id, rows):
            view = self.view(tenant, run_id, **context)
            if view is not None and view.project_id == project_id:
                views.append(view)
        return views

    def forget(self, tenant: str, run_id: str) -> None:
        self._settled.pop((tenant, run_id), None)
        self.running.pop((tenant, run_id), None)

    # ------------------------------------------------------------------ parsing
    def _parse(self, run: RunRecord, progress: dict[str, dict[str, Any]],
               accelerator: dict[str, Any]) -> RunView | None:
        payload = _load(run.envelope_json).get("payload") or {}
        graph = payload.get("workflow_graph") or {}
        workflow_id = str(graph.get("id") or "")
        if not workflow_id.startswith(PREFIX):
            return None
        studio = (payload.get("inputs") or {}).get("studio") or {}
        done = _load(run.checkpoint_json).get("workflow_nodes") or {}
        steps: dict[str, StepRecord] = {s.id: s for s in self.store.list_steps(run.tenant_id, run.id)}
        state = RunState(run.state)
        failures: dict[str, str] = {}
        if state in RUN_TERMINAL and any(n.get("kind") == "custom" and n.get("id") not in done
                                         for n in graph.get("nodes") or []):
            failures = self._custom_failures(run)
        view = RunView(tenant=run.tenant_id, subject=run.subject, run_id=run.id,
                       project_id=str(studio.get("project_id") or ""), workflow_id=workflow_id,
                       state=state.value, created_at=_iso(run.created_at))
        holder = accelerator.get("holder") or {}
        waiting = {(t.get("run_id"), t.get("step_id")) for t in accelerator.get("waiting") or []}
        for node in graph.get("nodes") or []:
            info = (node.get("config") or {}).get("studio") or {}
            item = NodeView(run_id=run.id, node_id=str(node.get("id")),
                            kind=str(info.get("kind") or node.get("kind")),
                            slot=str(info.get("slot") or ""), target=str(info.get("target") or ""),
                            engine=str(info.get("engine") or ""),
                            label=str(node.get("label") or info.get("kind") or ""),
                            estimate=float(info.get("estimate") or 0), meta=dict(info.get("meta") or {}),
                            request=dict(info.get("request") or {}), is_tool=node.get("kind") == "tool")
            entry = done.get(item.node_id) or {}
            step = steps.get("flow-" + item.node_id) if item.is_tool else None
            if step is not None:
                item.step_id = step.id
                item.started_at, item.finished_at = _iso(step.started_at), _iso(step.finished_at)
            live = progress.get(item.id)
            if entry.get("state") == "completed":
                item.state = "succeeded"
                output = entry.get("output")
                item.output = output if isinstance(output, dict) else {"value": output}
            elif step is not None and step.state != StepState.COMPLETED and (
                    state not in RUN_TERMINAL or step.state in (StepState.FAILED, StepState.UNCERTAIN)):
                self._from_step(item, step, holder, waiting)
            elif item.node_id in failures:
                item.state, item.error = "failed", failures[item.node_id]
            elif state in (RunState.CANCELLED, RunState.CANCEL_REQUESTED):
                item.state = "cancelled"
            elif state in RUN_TERMINAL:
                item.state = "skipped"
                item.error = "前面的项目没有完成，这一项未执行"
            elif step is not None or live is not None:
                item.state = "running"
                item.meta["progress"] = dict(live or {"stage": "整理结果"})
                item.started_at = item.started_at or str((live or {}).get("started_at") or "")
            view.nodes.append(item)
        return view

    @staticmethod
    def _from_step(item: NodeView, step: StepRecord, holder: dict[str, Any], waiting: set[tuple[Any, Any]]) -> None:
        error = _load(step.error_json) if step.error_json else None
        if step.state == StepState.AWAITING_APPROVAL:
            item.state, item.approval_id = "approval", step.approval_id or ""
            item.meta["progress"] = {"stage": "等待批准"}
        elif step.state == StepState.UNCERTAIN:
            item.state, item.error = "blocked", friendly_error(error or {"code": "uncertain"})
        elif step.state == StepState.FAILED:
            item.state, item.error = "failed", friendly_error(error)
        elif step.state == StepState.CANCELLED:
            item.state = "cancelled"
        else:  # prepared, dispatched, pending_external
            item.state = "running"
            if holder.get("run_id") == item.run_id and holder.get("step_id") == item.step_id:
                stage = "生成中"
            elif (item.run_id, item.step_id) in waiting:
                stage = "等待显卡空闲"
            else:
                stage = "已提交到生成节点"
            item.meta["progress"] = {"stage": stage}

    def _custom_failures(self, run: RunRecord) -> dict[str, str]:
        failures: dict[str, str] = {}
        after = 0
        while True:
            events = self.store.event_list(run.tenant_id, run.id, after, 500)
            for event in events:
                if event["kind"] == "workflow.node_failed":
                    detail = event.get("payload") or {}
                    failures[str(detail.get("node_id"))] = str(detail.get("message") or detail.get("code") or "失败")
            if len(events) < 500:
                return failures
            after = int(events[-1]["seq"])
