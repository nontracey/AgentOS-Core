# 架构图维护

总图只表示逻辑分层和通用部署；组件内部调用与下一步能力接入图放各模块说明。

| Markdown 源 | SVG 阅读版 | 图的职责 |
| --- | --- | --- |
| [总体架构](../01-总体架构.md) | [五层总图](01-current-1.svg)、[通用部署](01-current-2.svg) | 看系统层次与核心/可选能力，不绑定机器 |
| [执行模块](../modules/execution.md) | [组件调用](execution-current-1.svg)、[动作时序](execution-current-2.svg) | 引擎、编排、预算、审批和效果账本的内部细节 |
| [能力接入模块](../modules/capabilities.md) | [下一步目录方案](capabilities-current-1.svg) | 明确标注未实现的统一发现与双入口设计 |

唯一维护源是 Markdown Mermaid 块。使用官方 [Mermaid CLI](https://github.com/mermaid-js/mermaid-cli) `@mermaid-js/mermaid-cli@12.0.0`（MIT），关闭字体嵌入；本机 Chromium 配置通过 `MERMAID_PUPPETEER_CONFIG` 指定，不写入仓库。

```sh
uv run python scripts/render_diagrams.py
uv run python scripts/render_diagrams.py --check
uv run python scripts/md2html.py docs/00-需求规格.md
uv run python scripts/md2html.py docs/01-总体架构.md
uv run python scripts/md2html.py docs/02-工程设计.md
```

SVG 带源摘要，CI 离线检查源/图一致；渲染后仍人工检查。00/01/02 HTML 由 Markdown 重建，总体 HTML 直接显示分层与通用部署 SVG。模块文档由支持 Mermaid 的 Markdown 阅读器显示，SVG 也可独立打开。

退役旧图随阶段历史仅本地保留，公开目录只维护当前用途的图。清理源或改图编号时同步图清单与公开链接，不能留下依赖本地归档的引用。
