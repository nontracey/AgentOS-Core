"""Development stand-in for a generation node (studio UI work without a GPU machine).

uv run python scripts/dev_mock_node.py --port 8765 [--token TOKEN] [--delay 2]
Then pair it: POST /v1/nodes/pair {"node_id": "mock", "endpoint": "http://127.0.0.1:8765", "token": TOKEN}
and start the core with studio enabled and studio_node_id = "mock".

It advertises the core AIGC node tool contracts and answers with synthetic placeholder media:
gradient stills, short test-pattern clips and tone "voices". Nothing here is model output.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import math
import shutil
import struct
import subprocess
import tempfile
import wave
from datetime import timedelta
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import Response
from PIL import Image, ImageDraw

from agentos.adapters.aigc.tools import AigcToolAdapter
from agentos.kernel.types import AuthContext, utc_now

TOOLS = ("aigc.image", "aigc.generate", "aigc.speak", "aigc.lipsync")
files: dict[str, bytes] = {}
done: dict[str, dict[str, Any]] = {}   # idempotency key -> response, like a real node's job ledger
settings: dict[str, Any] = {"token": "", "delay": 2.0}
app = FastAPI()


def capabilities() -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory() as tmp:
        adapter = AigcToolAdapter(base_url="http://comfy.invalid", sandbox_dir=Path(tmp), ffmpeg="ffmpeg",
                                  speech_command=["speak"], lipsync_command=["lipsync"])
        actor = AuthContext(subject="mock-node", tenant_id="default", scopes=["read"], policy_version="dev",
                            expires_at=utc_now() + timedelta(hours=1))
        return [{"name": s.name, "version": s.version, "description": "[mock] " + s.description,
                 "input_schema": s.input_schema, "risks": s.risks, "recovery": s.recovery}
                for s in adapter.list_tools(actor) if s.name in TOOLS]


def check(authorization: str | None) -> None:
    if settings["token"] and authorization != f"Bearer {settings['token']}":
        raise HTTPException(status_code=401, detail="token rejected")


def keep(data: bytes, suffix: str) -> tuple[str, str]:
    sha = hashlib.sha256(data).hexdigest()
    ref = f"mock-{sha[:20]}{suffix}"
    files[ref] = data
    return ref, sha


def still(width: int, height: int, seed: int, label: str) -> bytes:
    image = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(image)
    hue = (seed % 360) / 360
    for y in range(height):
        t = y / max(1, height)
        shade = 0.3 + 0.5 * t
        r, g, b = (int(255 * shade * (0.6 + 0.4 * math.sin(2 * math.pi * (hue + k / 3)))) for k in range(3))
        draw.line([(0, y), (width, y)], fill=(r, g, b))
    draw.rectangle([width * 0.08, height * 0.08, width * 0.92, height * 0.2], outline=(255, 255, 255), width=3)
    draw.text((width * 0.1, height * 0.11), f"MOCK {label}"[:40], fill=(255, 255, 255))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def clip(seconds: float, width: int, height: int) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise HTTPException(status_code=500, detail="the mock node needs ffmpeg for clips")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "clip.mp4"
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        f"testsrc2=size={width}x{height}:rate=16:duration={seconds:.2f}", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", str(out)], check=True, capture_output=True)
        return out.read_bytes()


def tone(seconds: float) -> bytes:
    rate = 24000
    samples = b"".join(struct.pack("<h", int(7000 * max(0.0, math.sin(math.pi * ((n / rate * 4.2) % 1.0))) ** 1.5
                                              * math.sin(2 * math.pi * 210 * n / rate)))
                       for n in range(int(seconds * rate)))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples)
    return buffer.getvalue()


@app.get("/tools")
async def tools(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
    check(authorization)
    return capabilities()


@app.post("/assets")
async def push(request: Request, authorization: str | None = Header(default=None)) -> dict[str, str]:
    check(authorization)
    data = await request.body()
    ref, sha = keep(data, Path(request.headers.get("x-asset-filename", "in.bin")).suffix[:8])
    if sha != request.headers.get("x-asset-sha256"):
        raise HTTPException(status_code=400, detail="sha256 mismatch")
    return {"asset_id": ref, "sha256": sha}


@app.get("/assets/{ref}")
async def fetch(ref: str, authorization: str | None = Header(default=None)) -> Response:
    check(authorization)
    if ref not in files:
        raise HTTPException(status_code=404)
    return Response(files[ref], media_type="application/octet-stream")


@app.post("/execute")
async def execute(body: dict[str, Any], authorization: str | None = Header(default=None)) -> dict[str, Any]:
    check(authorization)
    key = str(body.get("idempotency_key") or "")
    if key in done:
        return done[key]
    await asyncio.sleep(float(settings["delay"]))
    tool, args = str(body.get("tool_name")), dict(body.get("args") or {})
    if tool == "aigc.image":
        data, mime, suffix = still(int(args.get("width", 576)), int(args.get("height", 1024)),
                                   int(args.get("seed", 1)), str(args.get("recipe", ""))), "image/png", ".png"
    elif tool == "aigc.generate":
        seconds = int(args.get("frames", 49)) / int(args.get("fps", 16))
        data, mime, suffix = clip(seconds, int(args.get("width", 480)), int(args.get("height", 848))), "video/mp4", ".mp4"
    elif tool == "aigc.speak":
        data, mime, suffix = tone(max(0.8, len(str(args.get("text", ""))) / 4.6)), "audio/wav", ".wav"
    elif tool == "aigc.lipsync":
        data, mime, suffix = clip(float(args.get("duration", 3.0)), 480, 848), "video/mp4", ".mp4"
    else:
        raise HTTPException(status_code=404, detail="unknown tool")
    ref, sha = keep(data, suffix)
    response = {"status": "completed", "value": {"asset_ref": ref, "sha256": sha, "job_id": "mock-" + sha[:8],
                                                 "assets": [{"asset_ref": ref, "sha256": sha, "mime": mime,
                                                             "role": "output"}]}}
    if key:
        done[key] = response
    return response


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", default="", help="require this bearer token (the one given when pairing)")
    parser.add_argument("--delay", type=float, default=2.0, help="seconds each generation pretends to take")
    options = parser.parse_args()
    settings.update(token=options.token, delay=options.delay)
    uvicorn.run(app, host="127.0.0.1", port=options.port, log_level="warning")
