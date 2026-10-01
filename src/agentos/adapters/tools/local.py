"""本地受控示例工具（M0：CLI 可受理并执行受控本地示例）。

设计要点（FR-TOOL-08、FR-ORC-08）：
- 工具声明风险元数据（risks/recovery），Policy 据此裁决；高危工具无审批必拒。
- 幂等语义显式实现：fs.write_text 同内容重放不重写；log.append 以 ctx.idempotency_key
  去重（模拟外部幂等系统），同 step 重试只有一次已确认效果。
- 文件工具锁定沙箱目录：拒绝绝对路径与路径穿越（提示注入样本的硬约束）。
- poll/cancel 明确返回 UNSUPPORTED_CAPABILITY，不伪装成功（FR-TOOL-10）。
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

from jsonschema import ValidationError
from jsonschema import validate as jsonschema_validate

from agentos.kernel.errors import AgentOSError, ErrorCode, unsupported_capability
from agentos.kernel.types import AuthContext, CallContext, ToolResult, ToolSpec

_TOOLS: dict[str, tuple[ToolSpec, Any]] = {}


def _spec(
    name: str,
    description: str,
    input_schema: dict[str, Any],
    risks: list[str],
    recovery: str,
    timeout_ms: int = 10_000,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        version="1.0.0",
        description=description,
        input_schema=input_schema,
        risks=risks,
        recovery=recovery,
        timeout_ms=timeout_ms,
    )


def _sandboxed_path(base: Path, raw_path: str) -> Path:
    if not raw_path or Path(raw_path).is_absolute() or ".." in Path(raw_path).parts:
        raise AgentOSError(ErrorCode.INVALID, f"path escapes sandbox: {raw_path!r}")
    candidate = (base / raw_path).resolve()
    base_resolved = base.resolve()
    if not candidate.is_relative_to(base_resolved):
        raise AgentOSError(ErrorCode.INVALID, f"path escapes sandbox: {raw_path!r}")
    return candidate


class LocalToolAdapter:
    """进程内工具执行器；远程 MCP/Node 传输在 M1 由 ToolPort 的其他实现承接。"""

    def __init__(
        self, sandbox_dir: Path, *, ledger_file: Path | None = None,
        material_reader: Any | None = None,
        knowledge_searcher: Any | None = None,
        skills: Any | None = None, store: Any | None = None, isolated_subjects: bool = True,
    ) -> None:
        self._sandbox = sandbox_dir
        self._isolated_subjects = isolated_subjects
        self._ledger_file = ledger_file or sandbox_dir.parent / "_log_append_ledger.json"
        self._ledger_lock = threading.Lock()
        self._material_reader = material_reader  # (actor, run_id, asset_id) -> dict（模板材料读取）
        self._knowledge = knowledge_searcher  # KnowledgePort：跨 await 边界只调端口
        self._skills, self._store = skills, store
        self._tools = self._build_registry()
        if knowledge_searcher is None:
            self._tools.pop("knw.search")

    def _build_registry(self) -> dict[str, tuple[ToolSpec, str]]:
        """注册表保存方法名（而非绑定方法）：测试可替换、运行时可核对版本。"""
        return {
            "math.add": (
                _spec(
                    "math.add",
                    "两数相加（纯只读示例工具）",
                    {
                        "type": "object",
                        "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                        "required": ["a", "b"],
                        "additionalProperties": False,
                    },
                    ["read"],
                    "pure",
                ),
                "_math_add",
            ),
            "clock.now": (
                _spec(
                    "clock.now",
                    "返回当前 UTC 时间（纯只读）",
                    {"type": "object", "properties": {}, "additionalProperties": False},
                    ["read"],
                    "pure",
                ),
                "_clock_now",
            ),
            "fs.write_text": (
                _spec(
                    "fs.write_text",
                    "在沙箱目录内写文本文件（写操作，默认需审批）",
                    {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "pattern": "^(?!/)(?!.*\\.\\.)(?!^\\.$)"},
                            "content": {"type": "string"},
                        },
                        "required": ["path", "content"],
                        "additionalProperties": False,
                    },
                    ["write"],
                    "idempotency_key",
                ),
                "_fs_write_text",
            ),
            "log.append": (
                _spec(
                    "log.append",
                    "向任务日志追加一行（写操作；按幂等键去重，模拟外部幂等系统）",
                    {
                        "type": "object",
                        "properties": {"message": {"type": "string"}},
                        "required": ["message"],
                        "additionalProperties": False,
                    },
                    ["write"],
                    "idempotency_key",
                ),
                "_log_append",
            ),
            "skill.read": (
                _spec("skill.read", "按需读取本任务已固定版本的技能正文（技能不能授予权限）", {
                    "type": "object", "properties": {"skill_id": {"type": "string"}},
                    "required": ["skill_id"], "additionalProperties": False,
                }, ["read"], "pure"), "_skill_read",
            ),
            "knw.search": (
                _spec(
                    "knw.search",
                    "检索授权知识库（只读；服务端按源/块 ACL 过滤，调用方不参与授权）",
                    {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                    ["read"],
                    "pure",
                ),
                "_knw_search",
            ),
            "material.read": (
                _spec(
                    "material.read",
                    "读取本次任务授权的文本材料（只读；仅限 run 受理时登记的材料）",
                    {
                        "type": "object",
                        "properties": {"asset_id": {"type": "string"}},
                        "required": ["asset_id"],
                        "additionalProperties": False,
                    },
                    ["read"],
                    "pure",
                ),
                "_material_read",
            ),
            "flaky.timeout": (
                _spec(
                    "flaky.timeout",
                    "测试工具：调用后超时，效果未知（仅测试注册）",
                    {"type": "object", "properties": {}, "additionalProperties": False},
                    ["read"],
                    "manual",
                ),
                "_flaky_timeout",
            ),
        }

    # ------------------------------------------------------------- ToolPort
    async def list_tools(self, actor: AuthContext) -> list[ToolSpec]:
        return [spec for spec, _ in self._tools.values()]

    async def resolve(self, actor: AuthContext, name: str) -> ToolSpec:
        entry = self._tools.get(name)
        if entry is None:
            raise AgentOSError(ErrorCode.INVALID, f"unknown tool: {name}")
        return entry[0]

    async def call(self, name: str, args: dict[str, Any], ctx: CallContext) -> ToolResult:
        entry = self._tools.get(name)
        if entry is None:
            raise AgentOSError(ErrorCode.INVALID, f"unknown tool: {name}")
        spec, handler_name = entry
        handler = getattr(self, handler_name)
        try:
            jsonschema_validate(instance=args, schema=spec.input_schema)
        except ValidationError as exc:
            return ToolResult(
                call_id=ctx.call_id,
                status="failed",
                error_code=str(ErrorCode.INVALID),
                value={"reason": exc.message},
            )
        try:
            result: ToolResult = await handler(args, ctx)
            return result
        except AgentOSError as exc:
            if exc.code == ErrorCode.INVALID:
                # 确定性输入错误（如沙箱逃逸）：结构化失败，无副作用
                return ToolResult(
                    call_id=ctx.call_id, status="failed",
                    error_code=str(ErrorCode.INVALID), value={"reason": exc.message},
                )
            raise  # 超时/不可用等交由 runtime 分类（uncertain/failed）

    async def poll(self, external_ref: str, ctx: CallContext) -> ToolResult:
        raise unsupported_capability("local tools have no external poll")

    async def cancel(self, external_ref: str, ctx: CallContext) -> ToolResult:
        raise unsupported_capability("local tools have no external cancel")

    # -------------------------------------------------------------- handlers
    @staticmethod
    async def _math_add(args: dict[str, Any], ctx: CallContext) -> ToolResult:
        value = float(args["a"]) + float(args["b"])
        return ToolResult(call_id=ctx.call_id, status="completed", value={"result": value})

    @staticmethod
    async def _clock_now(args: dict[str, Any], ctx: CallContext) -> ToolResult:
        from agentos.kernel.types import utc_now

        return ToolResult(call_id=ctx.call_id, status="completed", value={"utc": utc_now().isoformat()})

    def _subject_sandbox(self, actor: AuthContext) -> Path:
        if not self._isolated_subjects or actor.tenant_id == "default" and actor.subject == "owner":
            return self._sandbox
        digest = hashlib.sha256(f"{actor.tenant_id}\0{actor.subject}".encode()).hexdigest()
        return self._sandbox / "isolated" / digest

    async def _fs_write_text(self, args: dict[str, Any], ctx: CallContext) -> ToolResult:
        target = _sandboxed_path(self._subject_sandbox(ctx.actor), str(args["path"]))
        content = str(args["content"])
        if target.exists() and target.read_text(encoding="utf-8") == content:
            # 幂等重放：同内容重复写入不产生第二次副作用
            return ToolResult(
                call_id=ctx.call_id,
                status="completed",
                value={"written": False, "reason": "idempotent replay", "path": str(target)},
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return ToolResult(
            call_id=ctx.call_id,
            status="completed",
            value={"written": True, "path": str(target), "bytes": len(content.encode())},
        )

    async def _log_append(self, args: dict[str, Any], ctx: CallContext) -> ToolResult:
        """幂等键去重：同一 idempotency_key 重试只追加一次（模拟外部幂等系统）。"""
        ledger = (self._ledger_file if ctx.actor.tenant_id == "default" and ctx.actor.subject == "owner"
                  else self._subject_sandbox(ctx.actor) / "_append_ledger.json")
        ledger.parent.mkdir(parents=True, exist_ok=True)
        entries: dict[str, str] = {}
        if ledger.exists():
            entries = json.loads(ledger.read_text(encoding="utf-8"))
        if ctx.idempotency_key in entries:
            return ToolResult(
                call_id=ctx.call_id,
                status="completed",
                value={"appended": False, "reason": "idempotency key seen", "line": entries[ctx.idempotency_key]},
            )
        log_file = self._subject_sandbox(ctx.actor) / "task.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(str(args["message"]) + "\n")
        entries[ctx.idempotency_key] = str(args["message"])
        ledger.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        return ToolResult(call_id=ctx.call_id, status="completed", value={"appended": True})

    async def _material_read(self, args: dict[str, Any], ctx: CallContext) -> ToolResult:
        """模板材料读取：授权在服务端（AssetService.read_run_material 双重校验）。"""
        if self._material_reader is None:
            raise AgentOSError(ErrorCode.UNSUPPORTED, "material intake not configured")
        info = self._material_reader(ctx.actor, ctx.run_id, str(args["asset_id"]))
        return ToolResult(call_id=ctx.call_id, status="completed", value=info)

    async def _skill_read(self, args: dict[str, Any], ctx: CallContext) -> ToolResult:
        if self._skills is None or self._store is None:
            raise AgentOSError(ErrorCode.UNSUPPORTED, "skill service unavailable")
        run = self._store.get_run(ctx.actor.tenant_id, ctx.run_id)
        pinned = json.loads(run.envelope_json).get("payload", {}).get("skills", []) if run else []
        skill = next((s for s in pinned if s["id"] == args["skill_id"]), None)
        if skill is None:
            raise AgentOSError(ErrorCode.DENIED, "skill was not selected for this task")
        body = self._skills.load_body(ctx.actor, skill["id"], skill["version"])
        self._store.event_append(ctx.actor.tenant_id, ctx.run_id, "skill.loaded", skill)
        return ToolResult(call_id=ctx.call_id, status="completed", value={**skill, "body": body})

    async def _knw_search(self, args: dict[str, Any], ctx: CallContext) -> ToolResult:
        """知识检索：只调 KnowledgePort（换后端不改这里），ACL 由后端服务端强制。"""
        if self._knowledge is None:
            raise AgentOSError(ErrorCode.UNSUPPORTED, "knowledge backend not configured")
        limit = int(args.get("limit") or 5)
        backend = (self._knowledge.for_run(ctx.actor, ctx.run_id)
                   if hasattr(self._knowledge, "for_run") else self._knowledge)
        hits = await backend.search(ctx.actor, str(args["query"]), limit)
        return ToolResult(
            call_id=ctx.call_id, status="completed",
            value={
                "backend": backend.backend_id,
                "backend_version": backend.backend_version,
                "hits": [
                    {"chunk_id": h.chunk_id, "text": h.text,
                     "source_id": h.refs[0].source_id if h.refs else "",
                     "source_version": h.source_version,
                     "locator": h.refs[0].locator if h.refs else ""}
                    for h in hits
                ],
            },
            refs=[ref for h in hits for ref in h.refs],
        )

    @staticmethod
    async def _flaky_timeout(args: dict[str, Any], ctx: CallContext) -> ToolResult:
        import asyncio

        await asyncio.sleep(0.05)
        raise AgentOSError(ErrorCode.TIMEOUT, "injected timeout; external effect unknown")
