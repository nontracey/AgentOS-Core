# AgentOS 当前开发交接

> 版本：4.22
> 更新日期：2026-10-29
> 文档性质：当前交接
> 适用范围：STUDIO-V2 新工作台、直连对话与制作工作室（已合并到 `main`）

## 任务与变更

目标：工作台更好用；本地推理直连可用；AIGC 一键成片简单、可扩展，并且全部落在核心的治理与架构边界内。取舍见 [ADR-0026](03-架构决策.md#adr-0026)、[ADR-0027](03-架构决策.md#adr-0027)。

- 新工作台 `frontend/app`（Vite + React + TypeScript + Tailwind）：对话、工作室、任务、成果、设备、设置；浅/深色与手机底部导航。构建到 `frontend/app-dist/`（忽略，wheel 与镜像构建时生成）；旧界面保留在 `/ui/legacy/`。
- 直连对话分层：端点协议 `adapters/model/chat_endpoints.py`，会话 `adapters/store/chat_sqlite.py`，服务 `services/chat.py`，路由 `apps/chat.py`。端点声明 `egress`，`model_egress=local` 时云端端点停用；`model.chat` 审计只记元数据。
- 可选显卡互斥 `services/accelerator.py`：只有部署声明 `gpu_shared` 端点与 `accelerator_nodes` 时，节点生成与对话先到先得，生成前请对话服务卸载模型。
- 工作室：领域规则 `domains/aigc/studio/`（模型、导演、提示词、引擎表、参数规划），服务 `services/studio/`（草稿项目、运行读回、custom 节点），路由 `apps/studio.py`，合成与预览 `adapters/aigc/compositor.py`、`preview.py`。项目是 `studio` 草稿；生成、导演、合成都发布 FlowGraph 并启动运行；节点工具逐步经过策略/审批/预授权/账本；一键制作按阶段串联运行。旧的独立 SQLite、作业队列与模拟模式已移除。
- 进程内工作流引擎 `adapters/workflow/local.py`（`workflow_engine = "local"`），与 Temporal 同一路由语义，重启从检查点续跑。WorkflowDriver 给 custom 节点传入刷新后的身份与运行/节点 ID，节点失败事件带截断消息。
- 所有者预授权 `services/grants.py` 与 `/v1/grants`（FR-GOV-02）：单工具、1 小时至 7 天、可撤销、审计；删除/外发类工具不可预授权。工作室与设备页提供授权和撤销入口。
- Temporal 日志按消息形态限流；`to_public` 增加 `kind` 供成果页分组。
- 开发辅助：`scripts/dev_mock_ollama.py`（对话与导演）和 `scripts/dev_mock_node.py`（按核心节点工具契约应答的生成节点），只产生合成内容。

## 验证

- 门禁：`uv run ruff check .`、`uv run mypy src/agentos`、`uv run lint-imports`、`uv run pytest`、`scripts/export_contracts.py --check`、`scripts/check_dependencies.py`、`scripts/check_documents.py`；前端 `npm run build`（含 `tsc --noEmit`）。均通过；pytest 798 项中 792 通过、6 跳过。Gitleaks（`scripts/check_privacy.py`）需在部署方机器上运行，本轮未运行。
- 工作室合成证据：假节点 + 脚本化导演覆盖审批、预授权、选片来源、核对重放、取消重试、一键串联与导出；规划参数按核心节点工具 schema 校验。容器内用两个开发替身跑通“梗概→剧本→分镜→定妆→画面→动态→导出”，并截图检查工作室、队列与设备页（桌面与手机宽度）。均为合成数据，不代表真实模型质量。

## 未完成与下一动作

- 真实模型验证由部署方在自己的仓库与受保护目录完成：节点各 recipe 参数与耗时、配音参考音、口型、真实推理速度。
- 状态已记入 [开发计划](04-开发计划.md) 的 STUDIO-V2；成熟化方案 MATURITY-01（[成熟化升级方案](11-成熟化升级方案.md)）仍待使用者确认。
- 物理手机检查后置；能力中心、工作流编辑器与演示访问仍在旧界面。
