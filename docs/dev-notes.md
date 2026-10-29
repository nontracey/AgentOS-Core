# AgentOS 当前开发交接

> 版本：4.23
> 更新日期：2026-10-29
> 文档性质：当前交接
> 适用范围：CLOUD-AGENT-01 按用途开放云端模型与外部 Agent 令牌（建立在已合并的 STUDIO-V2 上）

## 任务与变更

目标：只让少量、高收益的文本环节（写剧本、拆分镜、改写提示词）可选用云端模型，其余保持本地；让 Claude Code、Codex 等外部 Agent 用自己的受限身份经入站 MCP 接入。取舍见 [ADR-0028](03-架构决策.md#adr-0028)。

- 按用途出口：`chat_cloud_purposes`（`[chat] cloud_purposes` 或 `AGENTOS_CHAT_CLOUD_PURPOSES`）。`model_egress=local` 时云端对话端点只服务列出的用途；`ChatService.refusal/endpoint/resolve_model` 按用途裁定，自动挑选只看本地端点，被拒的部署默认模型跳过、被拒的明确选择报错。智能体任务的模型网关不变。
- 端点模型白名单：`ChatEndpoint.models`，列表与调用都受限，用于把同一网关的本地路由与云端路由拆成两个端点。
- `/v1/chat/models?purpose=` 只返回该用途可用的模型，端点带 `egress`、`blocked`（对该用途）、`open_for`。前端 `ModelPicker` 接受 `purpose` 和“跟随默认”选项，云端端点带标记与外发提示。
- 工作室导演模型：`EnginePrefs.director`，`studio_director` 节点在运行时读取（请求未指定模型时），一键制作同样使用；项目设置（原“引擎与画风”菜单项改名为“项目设置”）可选，剧本页显示当前导演模型。
- 外部 Agent 令牌：`services/agent_tokens.py` 与 `/v1/agent-tokens`（列表、签发、撤销，仅所有者）；身份文件里的 `agent:` 主体为成员角色，只存令牌 SHA-256，scope 从 read/write/exec/egress 选，1～365 天，审计 `identity.agent_token_issued/revoked`。设置页“外部 Agent 接入”签发后显示一次令牌和 Claude Code / Codex 配置。入站 MCP 地址为 `runtime_gateway_url + "/mcp/"`（不带斜杠会 307）。
- 成员权限收紧：节点配对/撤销、删除知识源改为仅所有者（此前只挡游客，Agent 成为第一个非所有者写入主体）。
- 开发替身：`scripts/dev_mock_ollama.py` 增加 OpenAI 兼容的 `/v1/models` 与 `/v1/chat/completions`，可同时扮演本地端点和云端端点。

## 验证

- 门禁：`uv run ruff check .`、`uv run mypy src/agentos`、`uv run lint-imports`、`uv run pytest`、`scripts/export_contracts.py --check`、`scripts/check_dependencies.py`、`scripts/check_documents.py`、前端 `npm run build`（含 `tsc --noEmit`）均通过；pytest 803 项中 797 通过、6 跳过。Gitleaks（`scripts/check_privacy.py`）需在部署方机器上运行，本轮未运行。
- 新增测试：按用途放行与审计、云端从不被自动选中、模型白名单（`tests/integration/test_chat.py`）；令牌哈希存储、成员不能审批/签发/预授权/配对节点/删除知识、所有者裁定 Agent 的审批、经 `/mcp/` 列出工具、撤销与到期（`tests/integration/test_agent_tokens.py`）；一键制作使用项目导演模型（`tests/integration/test_studio.py`）。
- 合成环境：开发替身里项目导演模型选云端后，写剧本走云端端点，审计记 `egress=cloud, purpose=studio.director`；设置页令牌签发、项目设置的导演模型选择、对话里云端端点“未开放”的提示在桌面浅色与手机深色下截图检查。均为合成数据，不代表真实云端模型质量。

## 未完成与下一动作

- 真实云端模型由部署方在自己的网关路由与受保护配置中接入并验收：剧本/分镜质量、结构化输出兼容性与费用。
- Agent 建的工作室项目属于 Agent 自己的主体，不出现在所有者的工作室里；如需代所有者操作项目，另行设计委托。
- STUDIO-V2 的真实节点 recipe、配音参考音与口型实测仍待部署方完成；状态见 [开发计划](04-开发计划.md)。MATURITY-01（[成熟化升级方案](11-成熟化升级方案.md)）仍待使用者确认。
