"""Governance regressions across runtime, workflows, inbound MCP and integration ports."""

from __future__ import annotations

import asyncio
import json

import httpx
import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agentos.adapters.business.synthetic import SyntheticProjectAdapter
from agentos.adapters.telemetry.otel import OpenTelemetryMetadata
from agentos.apps.composition import build_system
from agentos.apps.config import Settings
from agentos.apps.gateway import create_app
from agentos.kernel.agent_runtime import RuntimeRequest
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.states import RunState
from agentos.kernel.workflow_graph import FlowGraph
from agentos.runtime.agent_driver import AgentRuntimeDriver
from agentos.runtime.executor import args_hash
from agentos.services.model_gate import ModelGate
from tests.conftest import owner_token


def test_engine_approval_restart_keeps_original_arguments_and_effect_id(tmp_path):
    class Engine:
        async def stream(self, request, handler):
            yield {"type": "binding", "thread_id": "synthetic-thread"}
            if not request.thread_id:
                result = await handler("immutable-call", "fs.write_text", {"path": "proof.txt", "content": "one"})
                assert result["blocked"]
                yield {"type": "blocked"}
            else:
                assert "Do not repeat" in request.prompt
                yield {"type": "assistant_message", "content": "finished"}
                yield {"type": "completed"}

    async def run():
        settings = Settings(data_dir=tmp_path)
        handle = build_system(settings)
        token = handle.boot()
        driver = AgentRuntimeDriver(handle.deps, Engine(), settings.sandbox_dir)
        handle.loop.use_engine(driver)
        receipt = await handle.ingress.accept(
            {"payload": {"goal": "write synthetic proof"}}, channel="http", credentials={"token": token}
        )
        assert await handle.worker.drive_until_settled(receipt.run_id) == "awaiting_approval"
        assert not (settings.sandbox_dir / "proof.txt").exists()
        rebuilt = build_system(settings)
        rebuilt.loop.use_engine(AgentRuntimeDriver(rebuilt.deps, Engine(), settings.sandbox_dir))
        actor = await rebuilt.deps.identity.authenticate({"token": token})
        approval = rebuilt.deps.approvals.list_pending("default")[0]
        rebuilt.deps.approvals.decide(actor, approval.id, "approve")
        assert await rebuilt.worker.drive_until_settled(receipt.run_id) == "succeeded"
        steps = rebuilt.store.list_steps("default", receipt.run_id)
        assert len(steps) == 1 and steps[0].args_hash == args_hash({"path": "proof.txt", "content": "one"})
        assert (settings.sandbox_dir / "proof.txt").read_text() == "one"

    asyncio.run(run())


def test_model_admission_blocks_cloud_compaction_and_native_tools_before_reservation(tmp_path):
    async def run():
        h = build_system(Settings(data_dir=tmp_path))
        t = h.boot()
        receipt = await h.ingress.accept(
            {"payload": {"goal": "private", "data_class": "local_only"}}, channel="http", credentials={"token": t}
        )
        h.store.transition_run("default", receipt.run_id, {RunState.CREATED}, RunState.RUNNING)
        gate = ModelGate(
            h.deps,
            gateway_url="http://127.0.0.1:8800",
            upstream_url="https://model.example/v1",
            api_key="",
            model="synthetic",
            egress="cloud",
            token_budget=100000,
            input_rate=1,
            output_rate=1,
        )
        _, credential = await gate.grant(RuntimeRequest(receipt.run_id, "", ""))
        with pytest.raises(AgentOSError, match="local_only"):
            await gate.admit(credential, {"model": "synthetic", "input": "compaction input"})
        assert not gate.inflight
        gate.egress = "local"
        with pytest.raises(AgentOSError, match="tool surface"):
            await gate.admit(
                credential, {"model": "synthetic", "tools": [{"type": "function", "name": "agentos_forged"}]}
            )
        gate.token_budget = 1
        with pytest.raises(AgentOSError) as error:
            await gate.admit(credential, {"model": "synthetic", "input": "x"})
        assert error.value.code == ErrorCode.BUDGET_EXCEEDED
        assert not gate.inflight
        gate.revoke(credential)
        with pytest.raises(AgentOSError, match="unknown"):
            await gate.admit(credential, {"model": "synthetic"})

    asyncio.run(run())


def test_flow_cycles_unknown_dependencies_and_cross_node_reads_are_denied(tmp_path):
    with pytest.raises(ValueError):
        FlowGraph(id="cycle", nodes=[{"id": "a", "kind": "wait", "depends_on": ["a"]}])

    async def run():
        h = build_system(Settings(data_dir=tmp_path))
        t = h.boot()
        actor = await h.deps.identity.authenticate({"token": t})
        graph = {
            "id": "references",
            "nodes": [
                {"id": "a", "kind": "tool", "config": {"tool": "math.add", "arguments": {"a": 2, "b": 3}}},
                {"id": "b", "kind": "custom", "config": {"adapter": "passthrough", "inputs": {"$ref": "nodes.a"}}},
            ],
        }
        with pytest.raises(AgentOSError, match="invalid workflow graph"):
            h.workflows.publish(actor, graph)
        assert h.store.list_runs("default") == []
        graph["nodes"][1]["depends_on"] = ["a"]
        p = h.workflows.publish(actor, graph)
        r = h.workflows.start(actor, graph["id"], p["version"], {}, "scope-test")
        binding = {"tenant_id": "default", "run_id": r["internal_run_id"]}
        assert (await h.workflow_driver.node({**binding, "node_id": "a"}))["status"] == "completed"
        assert (await h.workflow_driver.node({**binding, "node_id": "b"}))["status"] == "completed"
        assert len(h.store.list_steps("default", r["internal_run_id"])) == 1

    asyncio.run(run())


def test_inbound_mcp_independent_official_client_uses_same_idempotency_and_auth(tmp_path):
    async def run():
        h = build_system(Settings(data_dir=tmp_path), model_script=[{"content": "synthetic answer"}])
        h.boot()
        app = create_app(system=h)
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(  # noqa: SIM117 - injected independent client transport
                transport=httpx2.ASGITransport(app=app),
                headers={"Authorization": "Bearer " + owner_token(h)},
                follow_redirects=True,
            ) as transport:
                async with streamable_http_client("http://127.0.0.1:8800/mcp/", http_client=transport) as (read, write):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        tools = await client.list_tools()
                        assert "task_submit" in {t.name for t in tools.tools}
                        one = await client.call_tool("task_submit", {"goal": "synthetic", "idempotency_key": "same"})
                        two = await client.call_tool("task_submit", {"goal": "synthetic", "idempotency_key": "same"})
                        assert not one.is_error and not two.is_error
                        a = json.loads(one.content[0].text)
                        b = json.loads(two.content[0].text)
                        assert a["run_id"] == b["run_id"] and b["replayed"]
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
                r = await client.post(
                    "http://127.0.0.1:8800/mcp/",
                    headers={"Accept": "application/json, text/event-stream"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                )
                assert r.status_code == 401

    asyncio.run(run())


def test_project_adapter_membership_and_otel_never_export_contents(tmp_path):
    async def run():
        h = build_system(Settings(data_dir=tmp_path))
        actor = await h.deps.identity.authenticate({"token": h.boot()})
        adapter = SyntheticProjectAdapter(
            [
                {
                    "external_id": "old-1",
                    "name": "Synthetic",
                    "status": "active",
                    "tenant_id": "other-org",
                    "members": [actor.subject],
                }
            ]
        )
        with pytest.raises(AgentOSError):
            await adapter.get_project(actor, "old-1")

    asyncio.run(run())
    exporter = InMemorySpanExporter()
    tracing = OpenTelemetryMetadata(exporter)
    with tracing.span("tool.dispatch", run_id="run-synthetic", node_id="node-1"):
        pass
    span = exporter.get_finished_spans()[0]
    assert dict(span.attributes) == {"agentos.run.id": "run-synthetic", "agentos.node.id": "node-1"}
    assert not span.events


def test_concurrent_model_calls_reserve_combined_budget_before_wire(tmp_path):
    async def run():
        h = build_system(Settings(data_dir=tmp_path, run_budget_microusd=100))
        t = h.boot()
        r = await h.ingress.accept({"payload": {"goal": "budget concurrency"}}, channel="http",
                                   credentials={"token": t})
        h.store.transition_run("default", r.run_id, {RunState.CREATED}, RunState.RUNNING)
        gate = ModelGate(h.deps, gateway_url="http://localhost", upstream_url="https://model.example/v1",
                         api_key="", model="synthetic", egress="local", token_budget=1600,
                         input_rate=1, output_rate=1, max_output=512)
        _, credential = await gate.grant(RuntimeRequest(r.run_id, "", ""))
        results = await asyncio.gather(*[gate.admit(credential, {"model": "synthetic", "input": "x"})
                                        for _ in range(2)], return_exceptions=True)
        assert sum(isinstance(x, AgentOSError) for x in results) == 1
        assert len(gate.inflight) == 1
        admission = next(x for x in results if not isinstance(x, BaseException))
        gate.settle(admission, None)
        assert not gate.inflight
        with pytest.raises(AgentOSError):
            await gate.admit(credential, {"model": "synthetic", "input": "x"})
    asyncio.run(run())


def test_workflow_version_inputs_and_owner_stay_bound(tmp_path):
    async def run():
        h = build_system(Settings(data_dir=tmp_path))
        actor = await h.deps.identity.authenticate({"token": h.boot()})
        definition = {"id": "bound", "input_schema": {"type": "object", "required": ["label"],
            "properties": {"label": {"type": "string"}}}, "nodes": [{"id": "a", "kind": "custom",
            "config": {"adapter": "passthrough", "inputs": {"$ref": "inputs.label"}}}]}
        one = h.workflows.publish(actor, definition)
        assert h.workflows.publish(actor, definition)["reused"]
        with pytest.raises(AgentOSError):
            h.workflows.start(actor, "bound", one["version"], {}, "invalid")
        receipt = h.workflows.start(actor, "bound", one["version"], {"label": "first"}, "same")
        replay = h.workflows.start(actor, "bound", one["version"], {"label": "first"}, "same")
        assert receipt["internal_run_id"] == replay["internal_run_id"]
        with pytest.raises(AgentOSError):
            h.workflows.start(actor, "bound", one["version"], {"label": "changed"}, "same")
        with pytest.raises(AgentOSError):
            h.workflows.status(actor.model_copy(update={"subject": "unrelated", "roles": []}), receipt["wf_run_id"])
        r = h.store.get_run("default", receipt["internal_run_id"])
        assert json.loads(r.envelope_json)["payload"]["inputs"] == {"label": "first"}
        definition["nodes"][0]["config"]["inputs"] = "new"
        two = h.workflows.publish(actor, definition)
        assert two["version"] == one["version"] + 1
        saved = h.workflows.export(actor, "bound", one["version"])["definition"]
        assert saved["nodes"][0]["config"]["inputs"] == {"$ref": "inputs.label"}
    asyncio.run(run())


def test_workflow_http_boundaries_and_task_projection(tmp_path):
    from tests.conftest import add_subject

    async def run():
        h = build_system(Settings(data_dir=tmp_path))
        token = h.boot()
        actor = await h.deps.identity.authenticate({"token": token})
        graph = {"id": "http-boundary", "nodes": [{"id": "sum", "label": "合成加法", "kind": "tool",
                 "config": {"tool": "math.add", "arguments": {"a": 2, "b": 3}}}]}
        published = h.workflows.publish(actor, graph)
        receipt = h.workflows.start(actor, graph["id"], published["version"], {}, "http-proof")
        await h.workflow_driver.node({"tenant_id": "default", "run_id": receipt["internal_run_id"],
                                      "node_id": "sum"})
        member = add_subject(h, "unrelated-member", ["read", "write"])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(system=h)),
                                     base_url="http://localhost") as client:
            headers = {"Authorization": "Bearer " + token}
            for body in [{"version": "invalid"}, {"version": True},
                         {"version": 1, "inputs": []}, {"version": 1, "inputs": "bad"}]:
                r = await client.post("/v1/workflows/http-boundary/start", headers=headers, json=body)
                assert r.status_code == 422
            detail = await client.get("/v1/tasks/" + receipt["internal_run_id"], headers=headers)
            assert detail.status_code == 200
            plan = detail.json()["plan"]
            assert plan["source"] == "temporal" and plan["version"] == 1
            assert plan["tasks"][0]["title"] == "合成加法"
            assert plan["tasks"][0]["state"] == "completed"
            own = await client.get("/v1/workflows", headers=headers)
            assert len(own.json()) == 1
            foreign = await client.get("/v1/workflows", headers={"Authorization": "Bearer " + member})
            assert foreign.json() == []
            r = await client.get("/v1/workflow-runs/" + receipt["wf_run_id"],
                                 headers={"Authorization": "Bearer " + member})
            assert r.status_code == 401

    asyncio.run(run())


def test_temporal_skipped_branch_never_executes_descendant_effect(monkeypatch):
    from agentos.adapters.workflow import temporal

    dispatched = []

    async def activity(name, binding, **kwargs):
        if name == "agentos_flow_finish":
            assert binding["state"] == "succeeded"
            return None
        dispatched.append(binding["node_id"])
        return {"status": "completed", "branch": True}

    monkeypatch.setattr(temporal.workflow, "execute_activity", activity)
    flow = temporal.IntegrationFlow()
    result = asyncio.run(flow._run({"tenant_id": "synthetic", "run_id": "synthetic", "schedule": [
        {"id": "condition", "kind": "condition", "depends_on": [], "when": {}},
        {"id": "opposite", "kind": "tool", "depends_on": ["condition"], "when": {"condition": False}},
        {"id": "effect", "kind": "tool", "depends_on": ["opposite"], "when": {}},
    ]}))
    assert result == "succeeded" and dispatched == ["condition"]


@pytest.mark.parametrize("options", [{"model_provider": "openai_compat"}, {"agent_runtime": "codex"}])
def test_real_model_cannot_fall_back_to_legacy_reasoning_loop(options):
    with pytest.raises(AgentOSError, match="fixture-only"):
        Settings(**options).validate()


def test_weknora_setup_probe_credential_loads_separately(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGENTOS_WEKNORA_TOKEN", "synthetic-content-key")
    monkeypatch.setenv("AGENTOS_WEKNORA_PROBE_TOKEN", "synthetic-setup-only-key")
    settings = Settings.load()
    assert settings.knowledge_weknora_token == "synthetic-content-key"
    assert settings.knowledge_weknora_probe_token == "synthetic-setup-only-key"
