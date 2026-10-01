"""MCP 演示 server（M1 验收 fixture）：stdio 与 Streamable HTTP 两种传输共用同一工具集。

运行方式：
- stdio:   python examples/mcp_demo_server.py            （stdin/stdout JSON-RPC）
- http:    由测试导入 build_http_app() 后用 uvicorn 挂载
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

server: MCPServer = MCPServer(name="agentos-demo", instructions="AgentOS M1 Hub 验收演示工具")


@server.tool()
def echo(text: str) -> str:
    """原样返回输入文本。"""
    return f"echo: {text}"


@server.tool()
def add(a: int, b: int) -> int:
    """两数相加。"""
    return a + b


def build_http_app():
    """Streamable HTTP ASGI app（挂载路径 /mcp 由 SDK 决定）。"""
    return server.streamable_http_app()


if __name__ == "__main__":
    import anyio

    anyio.run(server.run_stdio_async)
