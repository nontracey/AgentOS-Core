"""语音入口切片（M6 模拟）：音频转写 → 同一 accept 路径受理。

- Transcriber 协议：任意 STT 后端（whisper.cpp / faster-whisper / 云 API 均可接入）
- WhisperCliTranscriber：调用外部 CLI，命令模板 `{audio}` 占位（env AGENTOS_STT_CMD），
  约定 CLI 把识别文本写到 stdout（如 whisper.cpp `-otxt -` 或包装脚本）
- 语音目标与文本目标走同一条 accept 路径：身份/幂等/预算/注入防护零旁路
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Any, Protocol


class Transcriber(Protocol):
    """STT 后端协议：输入音频文件，输出识别文本。"""

    def transcribe(self, audio_path: Path) -> str: ...


class WhisperCliTranscriber:
    """外部 whisper CLI 适配器（whisper.cpp / faster-whisper 包装脚本均可）。

    cmd_template 形如 "whisper-cpp -m model.bin -nt -f {audio}"，
    识别文本取 stdout（-nt = no timestamps）。
    """

    def __init__(self, cmd_template: str, timeout_s: float = 120.0) -> None:
        if "{audio}" not in cmd_template:
            raise ValueError("stt cmd template must contain {audio} placeholder")
        self._template = cmd_template
        self._timeout_s = timeout_s

    def transcribe(self, audio_path: Path) -> str:
        cmd = shlex.split(self._template)
        cmd[cmd.index("{audio}")] = str(audio_path)
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=self._timeout_s,
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[-200:]
            raise RuntimeError(f"stt cli exit={proc.returncode}: {detail}")
        text = " ".join(proc.stdout.split())
        if not text:
            raise RuntimeError("stt cli produced empty transcript")
        return text


class StubTranscriber:
    """测试用转写器：返回注入的固定文本（不依赖真实模型）。"""

    def __init__(self, text: str) -> None:
        self._text = text

    def transcribe(self, audio_path: Path) -> str:  # noqa: ARG002 - 协议签名一致
        return self._text


def build_transcriber(stt_cmd: str) -> Any:
    """按配置构造转写器；未配置返回 None（语音端点 501）。"""
    if not stt_cmd:
        return None
    return WhisperCliTranscriber(stt_cmd)
