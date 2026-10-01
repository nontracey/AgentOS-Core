"""组合根（蓝图 §7）：唯一创建实现并注入依赖的位置。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentos.adapters.identity.local_tokens import LocalTokenIdentity
from agentos.adapters.knowledge import backend_from_settings
from agentos.adapters.model.openai_compat import OpenAICompatModelAdapter
from agentos.adapters.model.scripted import ScriptedModelAdapter
from agentos.adapters.store.sqlite import SqliteStore
from agentos.adapters.tools.local import LocalToolAdapter
from agentos.adapters.tools.mcp_hub import McpHubAdapter
from agentos.adapters.tools.node_hub import NodeHubAdapter
from agentos.apps.config import Settings
from agentos.kernel.ports import ModelPort
from agentos.kernel.store import StoreProtocol
from agentos.runtime.executor import RuntimeDeps
from agentos.runtime.loop import AgentLoop, LoopLimits
from agentos.runtime.worker import Worker
from agentos.services.access import AccessService
from agentos.services.approvals import ApprovalService
from agentos.services.assets import AssetService
from agentos.services.audit import AuditService
from agentos.services.budget import BudgetService
from agentos.services.connections import ConnectionService
from agentos.services.demo import DemoService
from agentos.services.drafts import DraftService
from agentos.services.identity import IdentityService
from agentos.services.ingress import IngressService
from agentos.services.nodes import NodeRegistryService
from agentos.services.planning import PlanRunner, RoleRegistry
from agentos.services.policy import PolicyService
from agentos.services.skills import SkillService
from agentos.services.workflow import WorkflowService


@dataclass
class SystemHandle:
    settings: Settings
    store: StoreProtocol
    deps: RuntimeDeps
    loop: AgentLoop
    worker: Worker
    planner: PlanRunner
    workflows: WorkflowService
    ingress: IngressService
    skills: SkillService
    tool_hub: NodeHubAdapter
    nodes: NodeRegistryService
    assets: AssetService
    identity_backend: LocalTokenIdentity
    knowledge: Any  # U4-06/07 知识后端（KnowledgePort 实现；业务调用者只依赖端口）
    access: AccessService  # U4-01 统一资源授权（gateway/服务/非 HTTP 入口共用）
    demo: DemoService  # U4-01/02 演示发布与游客只读投影
    drafts: DraftService
    connections: ConnectionService
    transcriber: Any = None  # 语音入口 STT 后端（M6 切片；None=语音端点 501）
    collaboration: Any = None
    model_gate: Any = None
    agent_engine: Any = None
    workflow_driver: Any = None
    workflow_engine: Any = None
    auth_sessions: Any = None  # 登录会话服务（双 token；None=未装配）

    def boot(self) -> str:
        """初始化数据目录与 owner 身份；返回 owner 令牌（仅首次生成时为新值）。

        启动时执行暂存附件 GC（蓝图 §8）：清理超时未被 run 引用的附件。
        """
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        removed = self.store.gc_attachments("default", self.settings.attachment_ttl_minutes)
        if removed:
            self.deps.audit.record(
                "default",
                "ingress.attachment_gc",
                subject=None,
                detail={"removed": len(removed), "ids": removed[:20]},
            )
        # R-C：启动时重放撤销事实——恢复旧快照复活的已撤销 grant 必须再次失效
        self.demo.replay_revocations("default")
        return self.identity_backend.ensure_owner()


def build_system(settings: Settings, *, model_script: list[dict[str, object]] | None = None) -> SystemHandle:
    settings.validate()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(settings.db_path)

    identity_backend = LocalTokenIdentity(settings.identity_file)
    from agentos.adapters.identity.federation import CredentialVault, FederatedIdentity, IdentityLane

    identity_provider = (FederatedIdentity(identity_backend,
                         [IdentityLane.model_validate(lane) for lane in settings.identity_lanes],
                         CredentialVault(settings.data_dir / "private-identities"))
                         if settings.identity_lanes else identity_backend)
    identity = IdentityService(identity_provider)
    policy = PolicyService(store, policy_version=identity_backend.policy_version)
    approvals = ApprovalService(store)
    audit = AuditService(store)
    audit_service = audit
    budget = BudgetService(store)
    access = AccessService(store, audit)

    skills_service = SkillService(store, settings.skills_dir)
    assets_service = AssetService(store, audit_service, settings.data_dir / "assets")
    # U4-06/07：知识后端由配置选择实现；业务调用者只拿到端口（切换后端不改上层）
    from dataclasses import replace

    from agentos.services.knowledge_backends import KnowledgeBackends

    available = {}
    selected = "disabled"
    if settings.knowledge_backend != "disabled":
        builtin = backend_from_settings(replace(settings, knowledge_backend="builtin"), store)
        available[builtin.backend_id] = builtin
        selected = builtin.backend_id
    if (settings.knowledge_backend != "disabled" and settings.knowledge_weknora_url
            and settings.knowledge_weknora_version
            and (settings.knowledge_backend == "weknora" or settings.knowledge_weknora_kb_id)):
        external = backend_from_settings(replace(settings, knowledge_backend="weknora"), store)
        available[external.backend_id] = external
        if settings.knowledge_backend == "weknora":
            selected = external.backend_id
    knowledge = KnowledgeBackends(store, available, selected, settings.data_dir / "knowledge-selection.json")
    tools = LocalToolAdapter(
        settings.sandbox_dir,
        material_reader=assets_service.read_run_material,  # 模板材料读取（服务端双重授权）
        skills=skills_service,
        store=store,
        knowledge_searcher=knowledge if knowledge.enabled else None,
    )

    inner_hub = McpHubAdapter(store, tools)
    tool_hub = NodeHubAdapter(inner_hub, store, audit_service, assets=assets_service)
    from agentos.services.capabilities import CapabilityCatalog

    catalog = CapabilityCatalog(tool_hub)
    nodes_service = NodeRegistryService(store, audit_service)
    roles = RoleRegistry(settings.data_dir / "roles")
    # 租约互斥依赖唯一 owner（同名字符串会让 claim 的 lease_owner=? 恒真）
    import os as _os
    import uuid as _uuid

    worker_owner = f"worker-{_os.getpid()}-{_uuid.uuid4().hex[:6]}"

    from agentos.adapters.telemetry.otel import OpenTelemetryMetadata

    deps = RuntimeDeps(
        store=store,
        identity=identity,
        policy=policy,
        approvals=approvals,
        audit=audit,
        budget=budget,
        tools=catalog,
        skills=skills_service,
        concurrency=settings.runtime.tools_per_run,
        telemetry=OpenTelemetryMetadata(),
    )

    model: ModelPort
    if settings.model_provider == "openai_compat":
        keys = [k.strip() for k in settings.model_api_keys.split(",") if k.strip()]
        model = OpenAICompatModelAdapter(
            base_url=settings.model_base_url,
            api_key=settings.model_api_key,
            model=settings.model_name,
            api_keys=keys or None,
        )
    else:
        # scripted provider：脚本持久化到数据目录，跨 CLI 命令/进程保持确定性。
        script_file = settings.data_dir / "model_script.json"
        if model_script is not None:
            script_file.write_text(json.dumps(model_script, ensure_ascii=False), encoding="utf-8")
        script: list[dict[str, object]] = []
        if script_file.exists():
            script = json.loads(script_file.read_text(encoding="utf-8"))
        model = ScriptedModelAdapter(script)

    limits = LoopLimits(
        max_steps=settings.runtime.max_steps,
        max_replans=settings.runtime.max_replans,
        token_budget=settings.runtime.token_budget,
        microusd_per_1k_input=settings.microusd_per_1k_input,
        microusd_per_1k_output=settings.microusd_per_1k_output,
    )
    router = None
    if settings.model_chain:
        from agentos.services.model_router import ModelRouter, ProviderSpec

        chain_names = [n.strip() for n in settings.model_chain.split(",") if n.strip()]
        primary_name = "primary"
        providers = [ProviderSpec(name=primary_name, adapter=model,
                                  egress="local" if settings.model_provider == "scripted" else "cloud")]
        for name in chain_names:
            if name == "openai_compat" and settings.model_provider != "openai_compat":
                keys = [k.strip() for k in settings.model_api_keys.split(",") if k.strip()]
                providers.append(
                    ProviderSpec(
                        name="openai_compat",
                        adapter=OpenAICompatModelAdapter(
                            base_url=settings.model_base_url,
                            api_key=settings.model_api_key,
                            model=settings.model_name,
                            api_keys=keys or None,
                        ),
                    )
                )
            elif name == "ollama" and "ollama" not in [p.name for p in providers]:
                if not settings.model_fallback_base_url or not settings.model_fallback_name:
                    raise ValueError("Ollama fallback requires BASE_URL and an installed MODEL_FALLBACK_NAME")
                # 本地兜底（egress=local）：断网/云端限流时显式降级（事件+模型名可查）
                providers.append(
                    ProviderSpec(
                        name="ollama",
                        adapter=OpenAICompatModelAdapter(
                            base_url=settings.model_fallback_base_url,
                            api_key="ollama",
                            model=settings.model_fallback_name,
                            reasoning_effort=settings.model_fallback_reasoning_effort or None,
                        ),
                    )
                )
            elif name == "scripted":
                script_file = settings.data_dir / "model_script.json"
                fallback_script = json.loads(script_file.read_text(encoding="utf-8")) if script_file.exists() else []
                providers.append(ProviderSpec(name="scripted", adapter=ScriptedModelAdapter(fallback_script)))
        router = ModelRouter(providers, primary="primary")

    model_gate = None
    engine = None
    if settings.agent_runtime == "codex":
        from agentos.adapters.runtime.codex import CodexRuntimeAdapter
        from agentos.runtime.agent_driver import AgentRuntimeDriver
        from agentos.services.model_gate import ModelGate

        model_gate = ModelGate(deps, gateway_url=settings.runtime_gateway_url,
            upstream_url=settings.model_base_url, api_key=settings.model_api_key, model=settings.model_name,
            egress=settings.model_egress, token_budget=settings.runtime.token_budget,
            input_rate=settings.microusd_per_1k_input, output_rate=settings.microusd_per_1k_output)
        engine = CodexRuntimeAdapter(binary=settings.codex_binary, state_dir=settings.data_dir / "runtime-state",
            base_url=settings.model_base_url, model=settings.model_name, version=settings.codex_version,
            grant=model_gate.grant, revoke=model_gate.revoke)
    loop = AgentLoop(deps, model, limits)
    if engine is not None:
        loop.use_engine(AgentRuntimeDriver(deps, engine, settings.sandbox_dir, settings.runtime.max_steps))
    loop.set_owner(worker_owner)
    if router is not None:
        loop.use_router(router)
    from agentos.services.template_runner import TemplateRunner

    engine_driver = loop._engine_driver
    report_runner = TemplateRunner(
        deps,
        model,
        assets_service,
        router=router,
        token_budget=settings.runtime.token_budget,
        microusd_per_1k_input=settings.microusd_per_1k_input,
        microusd_per_1k_output=settings.microusd_per_1k_output,
    )
    report_runner._engine_driver = engine_driver
    loop.use_template_runner(report_runner)
    planner = PlanRunner(
        deps,
        model,
        roles,
        router=router,
        input_rate=settings.microusd_per_1k_input,
        output_rate=settings.microusd_per_1k_output,
        token_budget=settings.runtime.token_budget,
    )
    planner._engine_driver = engine_driver
    deps.planner = planner  # 双向依赖：deps 先建，planner 后挂
    worker = Worker(loop, owner=worker_owner)
    from agentos.runtime.workflow_driver import WorkflowDriver

    flow_driver = WorkflowDriver(deps, engine_driver)
    flow_engine = None
    if settings.temporal_address:
        from agentos.adapters.workflow.temporal import TemporalWorkflowAdapter

        flow_engine = TemporalWorkflowAdapter(settings.temporal_address, settings.temporal_namespace,
            settings.temporal_queue, flow_driver.node, flow_driver.finish)
        flow_driver.engine = flow_engine
    async def passthrough(inputs: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        return inputs
    flow_driver.custom["passthrough"] = passthrough
    loop._workflow_driver = flow_driver
    workflows = WorkflowService(store, access, settings.run_budget_microusd)
    from agentos.adapters.tools.workflows import WorkflowTools

    catalog.workflow_tools = WorkflowTools(workflows, lambda: flow_driver.engine is not None)
    drafts = DraftService(store, access)
    connections = ConnectionService(drafts)
    ingress = IngressService(
        store,
        identity,
        audit,
        attachment_dir=settings.attachment_dir,
        run_budget_microusd=settings.run_budget_microusd,
        queue_limit=settings.runtime.queue_limit,
        knowledge=knowledge if knowledge.enabled else None,
        skills=skills_service,
        report_skill_source=Path(__file__).resolve().parents[1] / "resources" / "material-report",
        assets=assets_service,  # N2a：材料引用解析
        access=access,  # U4-01：guest 不得有任何业务写（所有入口同一判定）
    )
    from agentos.services.collaboration import CollaborationRunner

    collaboration = CollaborationRunner(report_runner, access, ingress)
    flow_driver.templates = {"material-report": report_runner, "activity-collaboration": collaboration}
    loop._collaboration_runner = collaboration
    demo = DemoService(
        store,
        audit,
        access,
        assets_service,
        identity_backend,
        knowledge=getattr(knowledge, "sync_service", None),
        revocation_ledger=settings.data_dir / "demo_revocations.json",
        public_enabled=settings.public_demo_enabled,
        public_ttl_hours=settings.public_demo_ttl_hours,
        public_max_sessions=settings.public_demo_max_sessions,
    )
    from agentos.services.voice import build_transcriber

    transcriber = build_transcriber(settings.stt_cmd)
    from agentos.services.auth_session import AuthSessionService

    auth_sessions = AuthSessionService(store, identity_backend)
    return SystemHandle(
        settings=settings,
        store=store,
        deps=deps,
        loop=loop,
        worker=worker,
        planner=planner,
        collaboration=collaboration,
        workflows=workflows,
        drafts=drafts,
        connections=connections,
        ingress=ingress,
        skills=skills_service,
        tool_hub=tool_hub,
        nodes=nodes_service,
        assets=assets_service,
        identity_backend=identity_backend,
        knowledge=knowledge,
        access=access,
        demo=demo,
        transcriber=transcriber,
        auth_sessions=auth_sessions,
        model_gate=model_gate,
        agent_engine=engine,
        workflow_driver=flow_driver,
        workflow_engine=flow_engine,
    )
