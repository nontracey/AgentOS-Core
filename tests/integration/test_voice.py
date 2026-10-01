"""语音入口切片（M6 模拟）：转写 → 同一 accept 路径受理。

- 未配置 STT（transcriber=None）→ 501
- 注入 StubTranscriber → 音频上传 → 202 + transcript，run 与文本入口同路径
- WhisperCliTranscriber：模板替换 + stdout 提取 + 失败归一
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from fastapi.testclient import TestClient

from agentos.apps.composition import SystemHandle
from agentos.apps.gateway import create_app
from agentos.services.voice import StubTranscriber, WhisperCliTranscriber
from tests.conftest import WRITE_FLOW_SCRIPT, owner_token


def _client(make_system: Callable[..., SystemHandle], transcriber: object | None = None):
    handle = make_system(model_script=WRITE_FLOW_SCRIPT)
    if transcriber is not None or transcriber is None:
        handle.transcriber = transcriber  # 显式注入（含 None 用例）
    app = create_app(system=handle)
    return TestClient(app), handle, owner_token(handle)


def _post_audio(client: TestClient, token: str) -> object:
    return client.post(
        "/v1/voice/tasks",
        files={"audio": ("note.wav", b"RIFF....fake-bytes", "audio/wav")},
        headers={"Authorization": f"Bearer {token}"},
    )


def test_voice_disabled_returns_501(make_system: Callable[..., SystemHandle]) -> None:
    client, _, token = _client(make_system, transcriber=None)
    r = _post_audio(client, token)
    assert r.status_code == 501  # type: ignore[attr-defined]


def test_voice_stub_end_to_end(make_system: Callable[..., SystemHandle]) -> None:
    goal_text = "计算 7*6 并写入 voice_note.txt"
    client, handle, token = _client(make_system, transcriber=StubTranscriber(goal_text))
    r = _post_audio(client, token)
    assert r.status_code == 202  # type: ignore[attr-defined]
    data = r.json()  # type: ignore[attr-defined]
    assert data["transcript"].startswith("计算 7*6")
    # 与文本入口同一 accept 路径：run 已落库、goal 为转写文本
    run = handle.store.get_run(handle.store.list_runs("default")[0].tenant_id, data["run_id"])
    assert run is not None
    assert run.goal_text.startswith("计算 7*6")


def test_voice_unauthenticated(make_system: Callable[..., SystemHandle]) -> None:
    client, _, _ = _client(make_system, transcriber=StubTranscriber("x"))
    r = client.post("/v1/voice/tasks", files={"audio": ("a.wav", b"x", "audio/wav")})
    assert r.status_code == 401


def test_cli_transcriber_template_and_extract(tmp_path: Path) -> None:
    # 假 CLI：stdout 输出固定文本（模拟 whisper.cpp -nt）
    script = tmp_path / "fake_stt.py"
    script.write_text("import sys\nprint('识别结果 计算完成')\n", encoding="utf-8")
    tr = WhisperCliTranscriber(f"{sys.executable} {script} --audio {{audio}}")
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    text = tr.transcribe(audio)
    assert text == "识别结果 计算完成"


def test_cli_transcriber_rejects_template_without_placeholder() -> None:
    import pytest

    with pytest.raises(ValueError, match="audio"):
        WhisperCliTranscriber("whisper-cpp -m model.bin")


def test_cli_transcriber_failure_raises(tmp_path: Path) -> None:
    script = tmp_path / "fail_stt.py"
    script.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    tr = WhisperCliTranscriber(f"{sys.executable} {script} {{audio}}", timeout_s=10)
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    import pytest

    with pytest.raises(RuntimeError, match="exit=3"):
        tr.transcribe(audio)
