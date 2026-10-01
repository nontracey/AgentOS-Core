# 执行模块：引擎、编排与治理

本文说明当前实现细节；系统分层与部署形态见 [总体架构](../01-总体架构.md)。具体模块图不作为部署拓扑。

## 1. 组件调用关系

```mermaid
flowchart TB
    Web[Web UI / 工作流编辑 / HTTP / CLI]
    External[外部 Agent]
    IdP[企业既有 OIDC / ID4 / JWT]
    Accept[FastAPI 统一受理 / Authlib 与 PyJWT / 官方 MCP 入站]
    Govern[AgentOS：资源权限 / 审批 / 预算 / 任务与成果服务]
    Temporal[Temporal：编排 / 计时 / signal / 重试]
    Activity[受控节点 Activity / Workflow Port]
    Codex[Codex app-server：会话 / 循环 / 压缩]
    Executor[平台 executor：核权 / 审批 / 幂等 / 对账]
    Gate[ModelGate：每次推理及压缩前预留预算 / 裁定出域]
    Models[企业模型网关优先 / LiteLLM OSS]
    LLM[已配置模型端点 / 云端按出域策略]
    Knowledge[WeKnora / 企业知识平台]
    Tools[本地工具 / MCP 出站 / 授权执行节点]
    Skill[授权 Skill：固定版本与 digest]
    Ledger[SQLite 业务账本 / 不可变文件成果]
    OTel[OpenTelemetry：仅元数据]
    Future[后续 Port：IM / 企业额度 / 旧项目适配]
    Web --> Accept
    External -->|官方 MCP SDK| Accept
    IdP --> Accept --> Govern
    Govern -->|工作流| Temporal --> Activity
    Govern -->|单 Agent| Codex
    Activity -->|AI 或模板生成| Codex
    Activity -->|工具节点| Executor
    Codex -->|dynamicTools RPC| Executor
    Codex -->|模型请求| Gate --> Models --> LLM
    Executor --> Knowledge
    Executor --> Tools
    Executor -->|skill.read| Skill
    Govern --> Ledger
    Activity --> OTel
    Govern -. 预留 Adapter .-> Future
```

Temporal 是持久图工作流依赖；普通 Codex 任务可不接入 Temporal。模型端点可以部署于任意可达位置。知识实现当前为内置或 WeKnora，企业项目/额度/渠道图中虚线只表示预留 Port，未代表真实接入。

## 2. 单次工具动作与恢复

```mermaid
sequenceDiagram
    participant U as 使用者 / 外部 Agent
    participant A as AgentOS 受理与授权
    participant T as Temporal / 节点 Activity
    participant C as Codex app-server
    participant G as ModelGate / LiteLLM
    participant E as 平台 executor / 效果账本
    U->>A: 提交固定工作流版本和受权输入
    A->>T: 保存 run 并启动稳定 workflow ID
    T->>C: 委托 AI 节点 / 固定授权 Skill
    C->>G: 推理或上下文压缩请求
    G->>G: 复核身份 / 出域 / 预算预留
    G-->>C: 流与 usage / 结算
    C->>E: dynamicTool 请求具体业务动作
    E->>E: 核权 / 保存参数 / 稳定步骤键
    alt 动作需要审批
        E-->>A: 绑定原参数并暂停等待
        A-->>U: 展示待审批动作
        U->>A: 批准原动作
        A->>T: 唤醒原工作流
        T->>E: 用原参数恢复
    end
    E->>E: 复核批准 / 已知结果复用 / 未知先对账
    E-->>C: 受控执行结果 / 已确认回执
    C-->>T: 节点结果
    T->>A: 登记终态与不可变成果
    A-->>U: 受权查询 / 预览 / 下载
```

每次推理及压缩都经过 ModelGate。审批绑定保存的工具参数；恢复继续原调用，不重新生成参数。未知结果先核对，不通过重试假定成功。Codex 的原生工具关闭，平台工具经受控 executor 执行；控制面只读沙箱不等于远程系统已具备企业强隔离。

## 3. 代码定位

| 部分 | 代码 |
| --- | --- |
| 装配与模型闸门 | `apps/composition.py`、`services/model_gate.py` |
| Codex 协议与线程 | `adapters/runtime/codex.py`、`runtime/agent_driver.py` |
| Temporal 编排 | `adapters/workflow/temporal.py`、`runtime/workflow_driver.py` |
| 副作用与恢复 | `runtime/executor.py`、`services/access.py` |

通用引擎复用 Codex/Temporal，平台保留业务权限、审批、幂等和成果约束。业务策略与状态语义见 [工程设计](../02-工程设计.md)。
