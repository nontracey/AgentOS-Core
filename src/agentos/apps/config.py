"""配置（蓝图 §7）：默认值 < 配置文件 < 环境变量；启动时校验非法组合。

profile: personal —— 默认单机、SQLite、content_telemetry=false（FR-OBS-05 默认不采集）。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentos.kernel.errors import AgentOSError, ErrorCode


@dataclass
class RuntimeLimits:
    active_runs: int = 4
    tools_per_run: int = 2
    tools_global: int = 4
    queue_limit: int = 200
    max_steps: int = 24
    max_replans: int = 2
    token_budget: int = 64_000


@dataclass
class Settings:
    profile: str = "personal"
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("AGENTOS_DATA_DIR", "./data")))
    runtime: RuntimeLimits = field(default_factory=RuntimeLimits)
    agent_runtime: str = "scripted"  # codex | scripted (deterministic fixtures)
    codex_binary: str = "codex"
    codex_version: str = "0.159.2"
    runtime_gateway_url: str = "http://127.0.0.1:8800"
    model_egress: str = "local"  # local | cloud, explicit privacy boundary
    temporal_address: str = ""
    temporal_namespace: str = "default"
    temporal_queue: str = "agentos"
    model_provider: str = "scripted"  # scripted | openai_compat
    model_base_url: str = "http://127.0.0.1:8000/v1"
    model_name: str = "agentos-model"
    identity_lanes: list[dict[str, Any]] = field(default_factory=list)
    model_api_key: str = ""
    model_api_keys: str = ""  # 逗号分隔 key 池（限流轮换）；env AGENTOS_MODEL_API_KEYS
    model_fallback_base_url: str = ""  # 本地兜底端点（Ollama）；env AGENTOS_MODEL_FALLBACK_BASE_URL
    model_fallback_name: str = ""  # 兜底模型名
    model_fallback_reasoning_effort: str = "none"  # Ollama 报告默认关闭长思考，可按模型调整
    decision_mode: str = "shadow"
    decision_provider: str = "rule"
    governance_default: str = "deny"
    content_telemetry: bool = False
    run_budget_microusd: int = 0
    attachment_ttl_minutes: int = 24 * 60  # 暂存附件 TTL（蓝图 §8 GC 依据）
    model_chain: str = ""  # 逗号分隔降级链（空=单 provider）；FR-MDL-03
    webhook_secret: str = ""  # Webhook 入口 HMAC 密钥（env AGENTOS_WEBHOOK_SECRET）
    microusd_per_1k_input: int = 0
    microusd_per_1k_output: int = 0
    stt_cmd: str = ""  # 语音入口 STT CLI 模板（{audio} 占位，env AGENTOS_STT_CMD；空=语音端点关闭）
    # U4-06/07：知识后端（只在此处选择实现；kernel/runtime/业务调用者不感知）
    knowledge_backend: str = "builtin"      # disabled | builtin | weknora
    knowledge_weknora_url: str = ""         # WeKnora 基址（env AGENTOS_WEKNORA_URL）
    knowledge_weknora_auth_kind: str = "api_key"
    knowledge_weknora_probe_token: str = ""
    knowledge_weknora_token: str = ""       # 服务凭据（受控注入，不下发游客）
    knowledge_weknora_version: str = ""     # 锁定的版本/digest（禁止浮动 latest）
    knowledge_weknora_kb_id: str = ""       # 目标知识库（env AGENTOS_WEKNORA_KB_ID）
    # U5-01：游客为默认访问角色——免登进入只读演示空间（不是企业 IAM/SSO）
    public_demo_enabled: bool = True        # 总开关（env AGENTOS_PUBLIC_DEMO_ENABLED）
    public_demo_ttl_hours: int = 24         # 单次访客会话/授权到期上限
    public_demo_max_sessions: int = 200     # 同时有效的免登访客名额（防无限增殖）

    def validate(self) -> None:
        if self.agent_runtime not in {"codex", "scripted"}:
            raise AgentOSError(ErrorCode.INVALID, "unknown agent runtime")
        if (self.model_provider == "openai_compat") != (self.agent_runtime == "codex"):
            raise AgentOSError(ErrorCode.INVALID,
                               "real models require Codex runtime; scripted runtime is fixture-only")
        if self.model_egress not in {"local", "cloud"}:
            raise AgentOSError(ErrorCode.INVALID, "unknown model egress")
        if self.profile != "personal":
            raise AgentOSError(ErrorCode.INVALID, f"unknown profile: {self.profile}")
        if self.model_provider not in {"scripted", "openai_compat"}:
            raise AgentOSError(ErrorCode.INVALID, f"unknown model provider: {self.model_provider}")
        if self.governance_default != "deny":
            raise AgentOSError(ErrorCode.INVALID, "governance.default must be deny (fail closed)")
        if self.decision_mode != "shadow":
            # M3 前决策层只允许影子模式（FR-DEC-06）
            raise AgentOSError(ErrorCode.INVALID, "decision.mode must be shadow before M3")
        if self.content_telemetry:
            raise AgentOSError(ErrorCode.INVALID, "content_telemetry must stay false in M0")
        if not 1 <= self.public_demo_ttl_hours <= 24 * 30:
            raise AgentOSError(ErrorCode.INVALID, "public_demo_ttl_hours must be 1..720")
        if not 1 <= self.public_demo_max_sessions <= 5000:
            raise AgentOSError(ErrorCode.INVALID, "public_demo_max_sessions must be 1..5000")
        if self.knowledge_backend not in {"disabled", "builtin", "weknora"}:
            raise AgentOSError(
                ErrorCode.INVALID, f"unknown knowledge backend: {self.knowledge_backend}")
        if self.knowledge_backend == "weknora":
            # 外部后端必须显式配置并锁定版本：不使用浮动 latest，不允许静默退回内置
            if not self.knowledge_weknora_url:
                raise AgentOSError(ErrorCode.INVALID, "knowledge.weknora requires a base url")
            if not self.knowledge_weknora_version:
                raise AgentOSError(
                    ErrorCode.INVALID,
                    "knowledge.weknora requires a pinned version/digest (no floating latest)")

    @classmethod
    def load(cls, *, config_file: Path | None = None) -> Settings:
        settings = cls()
        source: Path | None = config_file
        if source is None:
            candidate = settings.data_dir / "agentos.toml"
            source = candidate if candidate.exists() else None
        if source is not None and source.exists():
            data = tomllib.loads(source.read_text(encoding="utf-8"))
            settings.identity_lanes = data.get("identity", {}).get("lanes", [])
            runtime = data.get("runtime", {})
            for key, value in runtime.items():
                if hasattr(settings.runtime, key):
                    setattr(settings.runtime, key, int(value))
            for key in ("model_provider", "model_base_url", "model_name", "model_chain",
                        "model_fallback_base_url", "model_fallback_name", "model_fallback_reasoning_effort",
                        "decision_mode",
                        "decision_provider", "run_budget_microusd", "microusd_per_1k_input",
                        "microusd_per_1k_output"):
                if key in data.get("model", {}):
                    setattr(settings, key, data["model"][key])
                elif key in data:
                    setattr(settings, key, data[key])
        for key in ("agent_runtime", "codex_binary", "codex_version", "runtime_gateway_url",
                    "temporal_address", "temporal_namespace", "temporal_queue", "model_egress"):
            if source is not None and source.exists() and key in data:
                setattr(settings, key, str(data[key]))
            value = os.environ.get("AGENTOS_" + key.upper())
            if value is not None:
                setattr(settings, key, value)
        # 环境变量覆盖
        settings.model_provider = os.environ.get("AGENTOS_MODEL_PROVIDER", settings.model_provider)
        settings.model_base_url = os.environ.get("AGENTOS_MODEL_BASE_URL", settings.model_base_url)
        settings.model_name = os.environ.get("AGENTOS_MODEL_NAME", settings.model_name)
        settings.model_api_key = os.environ.get("AGENTOS_MODEL_API_KEY", settings.model_api_key)
        settings.model_api_keys = os.environ.get("AGENTOS_MODEL_API_KEYS", settings.model_api_keys)
        settings.model_chain = os.environ.get("AGENTOS_MODEL_CHAIN", settings.model_chain)
        settings.model_fallback_base_url = os.environ.get(
            "AGENTOS_MODEL_FALLBACK_BASE_URL", settings.model_fallback_base_url)
        settings.model_fallback_name = os.environ.get(
            "AGENTOS_MODEL_FALLBACK_NAME", settings.model_fallback_name)
        settings.model_fallback_reasoning_effort = os.environ.get(
            "AGENTOS_MODEL_FALLBACK_REASONING_EFFORT", settings.model_fallback_reasoning_effort)
        settings.webhook_secret = os.environ.get("AGENTOS_WEBHOOK_SECRET", settings.webhook_secret)
        settings.stt_cmd = os.environ.get("AGENTOS_STT_CMD", settings.stt_cmd)
        settings.knowledge_backend = os.environ.get(
            "AGENTOS_KNOWLEDGE_BACKEND", settings.knowledge_backend)
        settings.knowledge_weknora_url = os.environ.get(
            "AGENTOS_WEKNORA_URL", settings.knowledge_weknora_url)
        settings.knowledge_weknora_auth_kind = os.environ.get(
            "AGENTOS_WEKNORA_AUTH_KIND", settings.knowledge_weknora_auth_kind)
        settings.knowledge_weknora_probe_token = os.environ.get(
            "AGENTOS_WEKNORA_PROBE_TOKEN", settings.knowledge_weknora_probe_token)
        settings.knowledge_weknora_token = os.environ.get(
            "AGENTOS_WEKNORA_TOKEN", settings.knowledge_weknora_token)
        settings.knowledge_weknora_version = os.environ.get(
            "AGENTOS_WEKNORA_VERSION", settings.knowledge_weknora_version)
        settings.knowledge_weknora_kb_id = os.environ.get(
            "AGENTOS_WEKNORA_KB_ID", settings.knowledge_weknora_kb_id)
        settings.public_demo_enabled = os.environ.get(
            "AGENTOS_PUBLIC_DEMO_ENABLED",
            "true" if settings.public_demo_enabled else "false").strip().lower() not in {
            "0", "false", "no", "off"}
        if (v := os.environ.get("AGENTOS_PUBLIC_DEMO_TTL_HOURS")) is not None:
            settings.public_demo_ttl_hours = int(v)
        if (v := os.environ.get("AGENTOS_PUBLIC_DEMO_MAX_SESSIONS")) is not None:
            settings.public_demo_max_sessions = int(v)
        if (v := os.environ.get("AGENTOS_RUN_BUDGET_MICROUSD")) is not None:
            settings.run_budget_microusd = int(v)
        if (v := os.environ.get("AGENTOS_MAX_STEPS")) is not None:
            settings.runtime.max_steps = int(v)
        settings.validate()
        return settings

    # ------------------------------------------------------------- paths
    @property
    def db_path(self) -> Path:
        return self.data_dir / "agentos.db"

    @property
    def identity_file(self) -> Path:
        return self.data_dir / "identity.json"

    @property
    def attachment_dir(self) -> Path:
        return self.data_dir / "attachments"

    @property
    def sandbox_dir(self) -> Path:
        return self.data_dir / "sandbox"

    @property
    def skills_dir(self) -> Path:
        return self.data_dir / "skills"
