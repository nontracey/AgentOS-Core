# AgentOS 验证说明

> 版本：1.2
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

这些拆仓检查未调用真实模型或外部服务，skip 不算真实服务验收。当时 Docker 配方尚未实际构建；后续验收应按下方当前工作单区分，不把旧结果当成当前全部 CPU 架构验证。所有原始命令输出只保存在本地。

构建并检查独立包：

```sh
uv build --wheel
uv run python scripts/check_wheel.py dist/agentos-0.1.0-py3-none-any.whl
```

构建前先在 frontend/workflow 运行 npm ci/typecheck/build。wheel 检查使用锁定依赖、独立数据目录和安装环境，不读取个人配置。发布源码、镜像和间接依赖需分别审查，现有组件清单不是完整 SBOM。

## 5. CAP-01 验证摘要

本轮关闭知识、统一工具目录与参数表单改动完成完整门禁：444 passed、6 skipped，Ruff/mypy/导入方向/契约/依赖检查通过；编辑器 typecheck/build 与独立 wheel 安装通过。新增 12 项合成机制测试包含目录权限、MCP 发现及停用、固定契约重启、已确认效果回放、未知效果保持对账、Agent 节点工具选择范围、Temporal 启动失败和外部知识不静默回退。

使用 Tabbit 在独立合成实例验证真实 MCP 发现、参数表单填写/HTTP 发布、停用自动同步且参数/版本保留、390px 窄屏无横向溢出、旧目录 API 兼容、SW v38→v39 缓存清理。图经浏览器渲染检查。原始截图不纳入 Git；该证据不是物理手机、真实推理或企业生产验收。

知识关闭不删除数据；Temporal 启动失败恢复需重启核心；旧无绑定的已发布定义需检查后重新发布。工作流/外部 Agent 工具化与 MCP 接入助手仍后置，详见开发计划。

## 6. VALIDATE-01 必做验收

个人实例在确认既有任务全部成功、无待审批/工作流后，备份数据库和配套身份、成果与运行状态，再用固定核心安全重启。使用已有模型端点经 Codex 完成加法与一项审批写入，核对准确合成文本、两项完成步骤、用量与治理事件；没有实际业务材料或额外模型安装。真实 Temporal 图工作流发布时固定契约并完成只读工具节点；WeKnora 健康、协议版本与指定 KB 访问通过。官方 MCP SDK 完成实际入站发现及任务状态读取。

Temporal 样例曾因验收参数字段错误被拒绝，修正后又遇到一次服务端命名空间异常；核对远端没有该执行后，另用稳定键提交只读样例成功。该证据证明当前闭环，不证明外部开发服务连续可用或生产 HA；未知业务效果仍需对账，不能靠重新提交掩盖失败。

从已提交的 CAP-01 基线全新本地克隆，执行 `uv sync --frozen`、`npm ci --no-audit --no-fund`、`npm run typecheck`、`npm run build`、`uv build --wheel` 和 `check_wheel.py --offline`，全部通过；没有复制个人配置或依赖原检出目录。备份在独立受保护目录恢复，SQLite integrity_check、原身份认证和已完成任务保留通过，不连接外部执行能力。

Tabbit 在升级后的个人实例验证真实任务详情直开、390px 无横向溢出、已发布工具表单保留数值和 SW v39；这是桌面浏览器视口模拟，未冒充物理手机验收。本轮再次运行完整门禁，444 passed、6 skipped；Ruff、87 文件 mypy、3 项导入契约、15 项契约导出和依赖方向检查通过。

复用已有 Docker 宿主完成 Linux/arm64 实际构建。原精简运行镜像缺少 libstdc++6，修复后构建探针通过 Node 22.23.3 与 Codex 0.159.2；下载使用可配置可信 registry，原生包完整性摘要与官方 registry 一致。隔离容器的 UI、未认证拒绝、知识 disabled、精确审批写入通过；删除测试容器并重新创建后，原令牌指纹、任务、文件和幂等回执一致。首轮创建异常经独立启动核对并清理；最终脚本按挂载目录用户执行，保护目录权限，完整两轮验收通过。脚本另验证未被 Git 忽略的目录会在生成身份前拒绝。没有删除卷、改既有服务或使用现场凭据。

容器复现命令见 [部署说明](09-集成部署.md#31-容器构建与隔离验收)。这是单平台、本次 Dockerfile 候选的实际验收；镜像构建元信息未指定时保持 unknown，不冒充已发布修订。真实模型验收来自上述个人实例，不冒充容器内真实推理；其他架构、完整 SBOM、生产 HA 与企业/真机验证未完成。

旧个人仓库托管缓存清除不由本核心控制。只读 API 仍能访问三个旧提交；清理申请草稿已准备，发送需明确授权，托管方清除确认前保持未完成。核心始终使用独立新历史，公开验证摘要不含旧泄露内容、现场地址或身份材料。
