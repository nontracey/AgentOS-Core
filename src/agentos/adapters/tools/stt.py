"""节点侧语音转写工具（stt.transcribe）：AgentOS 任务链的离线 STT 能力。

复用 Windows local-asr-win 既有资产（SenseVoiceSmall 模型 + .venv-funasr），
Node Agent 通过 subprocess 调 stt_cli.py，自身不装 funasr 依赖。

流程：音频先经 Asset 通道 push 到节点（sandbox/assets）→ stt.transcribe({asset_ref})
→ subprocess 转写 → 返回 {text, duration_s, engine}。引擎标识随回执返回——
上层必须显示用了什么引擎，禁止静默降级（用户明确要求）。
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import AuthContext, CallContext, ToolSpec


def _decode(raw: bytes | None) -> str:
    """子进程输出解码：UTF-8 优先，失败回退 GBK（Windows 中文环境常见）。"""
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", errors="replace")


class SttToolAdapter:
    """Windows 节点的离线转写（SenseVoiceSmall，CPU，断网可用）。"""

    def __init__(
        self, *, python_exe: Path, cli_path: Path, assets_dir: Path,
        engine_name: str = "SenseVoiceSmall(local,cpu)",
        timeout_s: float = 120.0,
    ) -> None:
        self._py = python_exe
        self._cli = cli_path
        self._assets = assets_dir
        self._engine = engine_name
        self._timeout = timeout_s

    def has_tool(self, name: str) -> bool:
        return name == "stt.transcribe"

    def list_tools(self, actor: AuthContext) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="stt.transcribe",
                version="1.0.0",
                description=f"离线语音转写（{self._engine}）: 传入已 push 到节点的音频 asset_ref，返回文本",
                input_schema={
                    "type": "object",
                    "properties": {
                        "asset_ref": {"type": "string",
                                      "description": "已上传到节点 assets 的音频文件名"},
                    },
                    "required": ["asset_ref"],
                    "additionalProperties": False,
                },
                risks=["read", "exec"],
                recovery="manual",
                timeout_ms=int(self._timeout * 1000),
            ),
        ]

    async def call(self, name: str, args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        if name != "stt.transcribe":
            raise AgentOSError(ErrorCode.INVALID, f"unknown stt tool: {name}")
        ref = str(args.get("asset_ref") or "")
        if not ref or "/" in ref or "\\" in ref or ".." in ref:
            raise AgentOSError(ErrorCode.INVALID, "invalid asset_ref")
        wav = (self._assets / ref).resolve()
        if not wav.exists():
            raise AgentOSError(ErrorCode.INVALID, f"audio asset not found: {ref}")
        if not wav.is_relative_to(self._assets.resolve()):
            raise AgentOSError(ErrorCode.INVALID, "path escapes sandbox")

        t0 = time.monotonic()
        proc = subprocess.run(
            [str(self._py), str(self._cli), str(wav)],
            capture_output=True, timeout=self._timeout,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        elapsed = time.monotonic() - t0
        # 子进程 stdout 编码不可控（Windows 中文环境默认 cp936）：按字节取回，
        # UTF-8 优先、失败回退 GBK。此前统一以 UTF-8 errors=replace 解码，
        # cp936 输出整段被替换成 U+FFFD，转写文本以乱码进证据（D 盲跑实测）。
        text = _decode(proc.stdout).strip()
        if proc.returncode != 0 or not text:
            return {
                "status": "failed",
                "error_code": "unavailable",
                "value": {"message": (_decode(proc.stderr) or "empty transcript").strip()[-400:]},
            }
        return {
            "status": "completed",
            "value": {
                "text": text[:4000],
                "engine": self._engine,   # 引擎随回执返回，上层显式展示（禁静默降级）
                "duration_s": round(elapsed, 2),
                "chars": len(text),
            },
        }
