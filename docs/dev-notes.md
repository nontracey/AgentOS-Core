# AgentOS 核心当前开发交接

> 版本：3.8
> 更新日期：2026-10-01
> 文档性质：当前工作快照
> 适用范围：CAP-01 核心装配、目录与工作流表单
> 阅读入口：[文档目录](README.md)

## 1. 当前任务

完成已授权前三项：知识按需关闭、统一工具目录、工作流接入变化同步与成熟参数表单；更新个人版本锁，只本地提交，不推送。

## 2. 变更

Settings 增加 disabled；组合根不装配知识、不为新任务绑定，不注入模型/MCP 检索工具，UI 显示未启用。Temporal 启动失败隔离普通任务。统一 CapabilityCatalog 装饰 ToolPort，提供 schema/风险/scope/契约摘要/发现状态，目录与派发统一。MCP 60 秒 TTL/3 秒发现上限，失败保留诊断描述并排除模型工具；停用直接调用拒绝，探针不重新启用。HTTP 发布固定工具/AI 节点契约，运行与审批恢复检查；已成功效果按账本回放。React Flow 编辑器复用 RJSF 6.11.0/AJV8，15 秒/聚焦同步；保留映射高级入口与草稿。SW 升级 v39。

## 3. 验证

本轮完整门禁：uv run ruff check .、mypy src/agentos、lint-imports、pytest、export_contracts.py --check、check_dependencies.py 全通过；pytest 444 passed、6 skipped。npm run typecheck/build、uv build --wheel 和 check_wheel.py 独立安装通过。Tabbit 实际表单填写并发布、停用自动同步保留参数/版本、390px 窄屏无横向溢出、旧目录接口兼容与 SW v38→v39 清理通过；没有真模型/物理手机/Docker 验证。21 个 Markdown/217 个本地链接及 5 张 Mermaid 摘要通过，00/01/02 HTML 已重建，能力图已浏览器渲染检查。公开文件 Gitleaks 无发现并人工核对源码、文档和生成物 diff。

## 4. 未完成与下一动作

本轮前三项检查完成，只本地提交并同步个人版本锁；个人现场进程没有重启，核心后台新功能重启后装配，旧 API 的前端兼容已检查。后续工作流/外部 Agent 自主选择、MCP 接入助手、Docker 实际构建、真实企业身份及业务验收仍按开发计划后置。旧无契约绑定的已发布定义需检查并重新发布以获得保护；Temporal 启动失败恢复需重启核心。
