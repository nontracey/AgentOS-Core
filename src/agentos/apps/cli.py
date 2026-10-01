"""CLI（蓝图 §22 命令族）：boot / submit / task get / task cancel / approvals / serve。

M1 起新增 mcp/skill 管理命令族；M0 不虚设尚不存在的命令。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from agentos.apps.composition import build_system
from agentos.apps.config import Settings
from agentos.kernel.errors import AgentOSError, ErrorCode

app = typer.Typer(help="AgentOS CLI：统一、可插拔的 AI 基础设施", no_args_is_help=True)
skill_app = typer.Typer(help="SkillHub：技能安装/列表/删除（FR-SKL-03）", no_args_is_help=True)
app.add_typer(skill_app, name="skill")
mcp_app = typer.Typer(help="MCP Hub：server 注册/发现/删除（FR-TOOL-01~03）", no_args_is_help=True)
app.add_typer(mcp_app, name="mcp")
task_app = typer.Typer(help="任务生命周期", no_args_is_help=True)
approval_app = typer.Typer(help="审批", no_args_is_help=True)
app.add_typer(task_app, name="task")
app.add_typer(approval_app, name="approvals")


def _settings() -> Settings:
    return Settings.load()


def _echo(data: object) -> None:
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str))


@app.command()
def boot() -> None:
    """初始化数据目录、迁移库表并生成 owner 身份。"""
    settings = _settings()
    system = build_system(settings)
    token = system.boot()
    _echo(
        {
            "status": "ok",
            "data_dir": str(settings.data_dir),
            "model_provider": settings.model_provider,
            "governance": settings.governance_default,
            "decision": f"{settings.decision_mode}/{settings.decision_provider}",
            "owner_token_preview": token[:8] + "...",
        }
    )


def _cli_token(settings: Settings) -> str:
    identity_file = settings.identity_file
    if not identity_file.exists():
        raise AgentOSError(ErrorCode.DENIED, "not booted: run `agentos boot` first")
    data = json.loads(identity_file.read_text(encoding="utf-8"))
    return str(data["subjects"]["owner"]["token"])


@task_app.command("submit")
def submit(
    goal: Annotated[str, typer.Argument(help="目标描述")],
    attach: Annotated[list[Path] | None, typer.Option(help="附件路径（可重复）")] = None,
    idempotency_key: Annotated[str | None, typer.Option(help="入口幂等键；缺省自动生成")] = None,
    wait: Annotated[bool, typer.Option("--wait/--no-wait", help="是否内联驱动到终态/阻塞")] = True,
    script: Annotated[Path | None, typer.Option(help="scripted 模型脚本 JSON（离线演示）")] = None,
) -> None:
    """提交目标；返回受理回执（FR-ING-06）。需要先 `agentos boot`。"""
    settings = _settings()
    token = _cli_token(settings)  # 未 boot 时先拒绝，不静默初始化
    model_script = json.loads(script.read_text(encoding="utf-8")) if script else None
    system = build_system(settings, model_script=model_script)
    system.boot()

    attachments = []
    for path in attach or []:
        attachments.append({"name": path.name, "content": path.read_bytes()})

    async def _run() -> dict[str, object]:
        receipt = await system.ingress.accept(
            {"payload": {"goal": goal}, "idempotency_key": idempotency_key, "attachments": attachments},
            channel="cli",
            credentials={"token": token},
        )
        state = None
        if wait:
            state = await system.worker.drive_until_settled(receipt.run_id)
        run = system.store.get_run("default", receipt.run_id)
        return {
            "receipt": receipt.model_dump(),
            "state": state or (run.state.value if run else None),
            "error": json.loads(run.error_json) if run and run.error_json else None,
        }

    _echo(asyncio.run(_run()))


@task_app.command("get")
def task_get(run_id: Annotated[str, typer.Argument()]) -> None:
    settings = _settings()
    system = build_system(settings)
    store = system.store
    run = store.get_run("default", run_id)
    if run is None:
        raise typer.Exit(code=2)
    steps = store.list_steps("default", run_id)
    events = store.event_list("default", run_id, limit=200)
    _echo(
        {
            "run": json.loads(run.model_dump_json()),
            "steps": [json.loads(s.model_dump_json()) for s in steps],
            "budget": store.budget_totals("default", run_id),
            "events": events,
        }
    )


@task_app.command("cancel")
def task_cancel(run_id: Annotated[str, typer.Argument()]) -> None:
    """请求取消；取消在下一个循环边界生效（≤5s 目标，FR-ORC-02）。"""
    settings = _settings()
    system = build_system(settings)
    store = system.store

    async def _run() -> dict[str, object]:
        run = store.request_cancel("default", run_id)
        if run is None:
            return {"error": "not found"}
        state = await system.worker.drive_until_settled(run_id, deadline_s=5)
        return {"run_id": run_id, "state": state}

    _echo(asyncio.run(_run()))


@approval_app.command("list")
def approvals_list() -> None:
    settings = _settings()
    system = build_system(settings)
    _echo(
        [
            json.loads(a.model_dump_json())
            for a in system.deps.approvals.list_pending("default")
        ]
    )


@approval_app.command("decide")
def approvals_decide(
    approval_id: Annotated[str, typer.Argument()],
    approve: Annotated[bool, typer.Option("--approve/--reject")] = True,
) -> None:
    settings = _settings()
    system = build_system(settings)
    token = _cli_token(settings)

    async def _run() -> dict[str, object]:

        creds = await system.deps.identity.authenticate({"token": token})
        record = system.deps.approvals.decide(creds, approval_id, "approve" if approve else "reject")
        await system.worker.drive_until_settled(record.call_id.split(":", 1)[0])
        run = system.store.get_run("default", record.call_id.split(":", 1)[0])
        return {"approval": record.state.value, "run_state": run.state.value if run else None}

    _echo(asyncio.run(_run()))


@skill_app.command("install")
def skill_install(
    source: Annotated[Path, typer.Argument(help="包含 SKILL.md 的目录")],
) -> None:
    settings = _settings()
    system = build_system(settings)
    system.boot()
    token = _cli_token(settings)

    async def _run() -> dict[str, object]:
        actor = await system.deps.identity.authenticate({"token": token})
        meta = system.skills.install(actor, str(source))
        return {"id": meta.id, "version": meta.version, "digest": meta.digest,
                "description": meta.description, "allowed_tools": meta.allowed_tools}

    _echo(asyncio.run(_run()))


@skill_app.command("list")
def skill_list() -> None:
    settings = _settings()
    system = build_system(settings)
    token = _cli_token(settings)

    async def _run() -> list[dict[str, object]]:
        actor = await system.deps.identity.authenticate({"token": token})
        return system.skills.list(actor)

    _echo(asyncio.run(_run()))


@skill_app.command("remove")
def skill_remove(
    skill_id: Annotated[str, typer.Argument()],
    version: Annotated[str | None, typer.Option()] = None,
) -> None:
    settings = _settings()
    system = build_system(settings)
    token = _cli_token(settings)

    async def _run() -> dict[str, object]:
        actor = await system.deps.identity.authenticate({"token": token})
        removed = system.skills.remove(actor, skill_id, version)
        return {"removed": removed}

    _echo(asyncio.run(_run()))


@mcp_app.command("add")
def mcp_add(
    server_id: Annotated[str, typer.Argument()],
    command: Annotated[str | None, typer.Option(help="stdio 启动命令")] = None,
    url: Annotated[str | None, typer.Option(help="Streamable HTTP 端点")] = None,
    risk: Annotated[str | None, typer.Option(help="逗号分隔风险标记，默认 write（需审批）")] = None,
    env: Annotated[list[str] | None, typer.Option(help="环境变量 KEY=env:VAR_NAME")] = None,
) -> None:
    """注册并发现 MCP server；远端工具命名空间为 serverId:tool。"""
    settings = _settings()
    system = build_system(settings)
    system.boot()
    token = _cli_token(settings)
    risks = [r.strip() for r in (risk or "").split(",") if r.strip()] or None
    env_map = {}
    for item in env or []:
        k, _, v = item.partition("=")
        env_map[k] = v

    async def _run() -> dict[str, object]:
        actor = await system.deps.identity.authenticate({"token": token})
        added: dict[str, object] = await system.tool_hub.add_server(
            actor, server_id, command=command, url=url, risks=risks, env=env_map
        )
        return added

    _echo(asyncio.run(_run()))


@mcp_app.command("list")
def mcp_list() -> None:
    settings = _settings()
    system = build_system(settings)
    token = _cli_token(settings)

    async def _run() -> list[dict[str, object]]:
        actor = await system.deps.identity.authenticate({"token": token})
        servers: list[dict[str, object]] = await system.tool_hub.list_servers(actor)
        return servers

    _echo(asyncio.run(_run()))


@mcp_app.command("remove")
def mcp_remove(server_id: Annotated[str, typer.Argument()]) -> None:
    settings = _settings()
    system = build_system(settings)
    token = _cli_token(settings)

    async def _run() -> dict[str, object]:
        actor = await system.deps.identity.authenticate({"token": token})
        await system.tool_hub.remove_server(actor, server_id)
        return {"removed": server_id}

    _echo(asyncio.run(_run()))


@app.command()
def serve(
    host: Annotated[str, typer.Option()] = "127.0.0.1",
    port: Annotated[int, typer.Option()] = 8771,
) -> None:
    """启动个人单进程形态：gateway + 内联 worker（默认仅监听回环，FR-NODE-04）。"""
    import uvicorn

    from agentos.apps.gateway import create_app

    uvicorn.run(create_app(), host=host, port=port, log_level="info")


def main() -> None:
    try:
        app()
    except AgentOSError as exc:
        typer.echo(json.dumps(exc.to_payload(), ensure_ascii=False), err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
