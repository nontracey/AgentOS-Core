"""U4-05 固定样本：真实 MCP stdio server（合成数据，无外部依赖）。

用途：给 AgentOS 的 MCP Hub 一个**真实**可发现的 server——探针能协商协议版本、
列出工具，普通任务能真实调用其中一个短工具并把结果写进 trace。

只暴露只读短工具：
- `team_metrics`：返回合成团队的完成率（不访问任何真实系统）
- `now_utc`：返回注入的固定时间戳（可复现）

运行方式（由 McpHubAdapter 以 stdio 拉起）：
    python tests/fixtures/u4/mcp_metrics_server.py
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

server = MCPServer("u4-metrics-fixture")


@server.tool()
def team_metrics(sprint: str = "S1") -> dict:
    """返回合成冲刺指标（只读）。"""
    return {
        "sprint": sprint,
        "completed": 2,
        "total": 3,
        "note": "合成数据：用于验证真实 MCP 调用链，不代表任何真实团队",
    }


@server.tool()
def now_utc() -> dict:
    """返回固定时间戳（可复现，不读系统时钟）。"""
    return {"utc": "2026-09-30T00:00:00+00:00", "source": "fixture"}


if __name__ == "__main__":
    server.run(transport="stdio")
