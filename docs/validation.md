# AgentOS 验证说明

> 版本：1.0
> 更新日期：2026-10-01
> 文档性质：可复现检查与事实边界
> 适用范围：公开仓库当前设计与实现
> 阅读入口：[文档目录](README.md)

## 1. 验证分工

公开文件树保留代码、契约、必要设计与验证摘要。个人/客户现场记录、凭据、原始日志和截图不在本仓库中。核心从审查后的通用文件建立独立 Git 历史；不复制旧仓库的 .git、数据、过程目录或现场打包文件。

源码核对、确定性测试、合成服务、真实提供方与真设备是不同证据。当前支持程度和未完成项由 [开发计划](04-开发计划.md) 裁定，不能把设计方案或组件存在当作能力已经实现。

## 2. 工程检查

在干净配置中执行与改动相称的检查；发布前：

```sh
uv run ruff check .
uv run mypy src/agentos
uv run lint-imports
uv run pytest
uv run python scripts/export_contracts.py --check
uv run python scripts/check_dependencies.py
```

可视编辑器改动在 `frontend/workflow` 执行实际 `npm run typecheck` 与 `npm run build`。纯文档工作不重复真实模型负载或安装外部服务；未跑的检查不写为已通过。

## 3. 文档与隐私检查

```sh
uv run python scripts/check_documents.py
uv run python scripts/render_diagrams.py --check
uv run python scripts/md2html.py docs/00-需求规格.md
uv run python scripts/md2html.py docs/01-总体架构.md
uv run python scripts/md2html.py docs/02-工程设计.md
uv run python scripts/check_privacy.py
```

文档检查针对当前 Git 公开文件集，链接不能依赖忽略目录；强制加入历史/证据目录也会被拒绝，提交 hook 和 CI 同样检查。稳定需求/来源/ADR 编号由 [编号基线](../contracts/document-identifiers.json) 校验。Mermaid 摘要保证源和 SVG 一致，仍需视觉核对。隐私扫描需 Gitleaks 8.30.1，另人工核对本次 diff、路径、个人/公司信息及生成物；不输出秘密原文。

## 4. 拆仓与安装验证边界

SPLIT-01 将通用产品文件与个人部署分开，并修复 wheel 缺失 UI 的安装问题。当前核心确定性/合成测试 432 passed、6 skipped；类型、依赖方向和契约检查通过。独立虚拟环境从 wheel 安装，UI、工作流静态资源、healthz 和内置 Skill 均通过合成检查。

这些检查未调用真实模型或三机外部服务，skip 不算真实服务验收。Docker 多阶段构建配方已更新，当前环境没有 Docker，不能宣称镜像构建/运行或全部 CPU 架构验证通过。具体最新状态见开发计划；所有原始命令输出仍只保存在本地。

构建并检查独立包：

```sh
uv build --wheel
uv run python scripts/check_wheel.py dist/agentos-0.1.0-py3-none-any.whl
```

构建前先在 frontend/workflow 运行 npm ci/typecheck/build。wheel 检查使用锁定依赖、独立数据目录和安装环境，不读取个人配置。发布源码、镜像和间接依赖需分别审查，现有组件清单不是完整 SBOM。
