"""Studio on core primitives: drafts, governed node tool steps, custom nodes and the local engine.

Scripted evidence only: a fake generation node answers the node protocol over patched httpx with
the core AIGC tool schemas, and the director's model calls are scripted. FFmpeg and Pillow run
for real. Nothing here says anything about real model quality.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import wave
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from agentos.adapters.aigc.tools import AigcToolAdapter
from agentos.apps.composition import SystemHandle
from agentos.apps.gateway import create_app
from agentos.kernel.types import AuthContext, utc_now
from tests.conftest import add_subject, owner_token

FFMPEG = shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(FFMPEG is None, reason="FFmpeg is required for studio media")

SCRIPT = """# 灯塔与猫
## 场1 海边灯塔 · 傍晚
【画面】守灯人老周提着马灯走上旋转楼梯，一只橘猫跟在后面。
老周（平静）：今晚风大，你别乱跑。
## 场2 灯塔顶层 · 夜
【画面】橘猫跳上窗台，望着远处的渔船灯光。
小满（惊讶）：爷爷，那是谁的船？
"""

CAST = {"logline": "风夜里，守灯人和孙女等一条迟归的渔船。", "characters": [
    {"name": "老周", "role": "主角", "description": "六十多岁，花白短发，深蓝棉袄", "visual_prompt":
     "old man, short gray hair, navy padded jacket", "voice": "低沉缓慢"},
    {"name": "小满", "role": "配角", "description": "十岁女孩，红色毛线帽", "visual_prompt":
     "young girl, red knit hat, yellow raincoat", "voice": "清脆"}],
    "locations": [{"name": "灯塔", "description": "白色灯塔，旋转楼梯", "visual_prompt":
                   "white lighthouse interior, spiral staircase, warm lamp light"}]}

SHOTS = {
    "场1": {"shots": [
        {"location": "灯塔", "characters": ["老周"], "size": "全景", "camera": "tilt_up",
         "action": "老周提着马灯上楼", "image_prompt": "old man climbs spiral stairs with a lantern",
         "motion": "i2v", "motion_prompt": "he climbs slowly, lantern swings", "duration": 3,
         "lines": [{"speaker": "老周", "text": "今晚风大，你别乱跑。", "emotion": "平静"}]}]},
    "场2": {"shots": [
        {"location": "灯塔", "characters": ["小满"], "size": "近景", "camera": "push_in",
         "action": "小满趴在窗边", "image_prompt": "girl at the window looking at distant boat lights",
         "motion": "still", "motion_prompt": "her breath fogs the glass", "duration": 3,
         "lines": [{"speaker": "小满", "text": "爷爷，那是谁的船？", "emotion": "惊讶", "intensity": 0.6}]}]},
}


def wav_bytes(seconds: float, freq: float = 220.0) -> bytes:
    rate = 16000
    frames = b"".join(struct.pack("<h", int(6000 * math.sin(2 * math.pi * freq * n / rate)))
                      for n in range(int(seconds * rate)))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(frames)
    return buffer.getvalue()


def png_bytes(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, "PNG")
    return buffer.getvalue()


def mp4_bytes(width: int, height: int, seconds: float) -> bytes:
    assert FFMPEG
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "clip.mp4"
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        f"testsrc=size={width}x{height}:rate=16:duration={seconds:.2f}", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", str(out)], check=True, capture_output=True)
        return out.read_bytes()


def node_capabilities(tmp: Path) -> list[dict[str, Any]]:
    """The core AIGC node adapter's own tool contracts, as a node advertises them."""
    adapter = AigcToolAdapter(base_url="http://comfy.invalid", sandbox_dir=tmp, ffmpeg=str(FFMPEG),
                              speech_command=["speak"], lipsync_command=["lipsync"])
    actor = AuthContext(subject="node", tenant_id="default", scopes=["read"], policy_version="t",
                        expires_at=utc_now() + timedelta(hours=1))
    return [{"name": s.name, "version": s.version, "description": s.description, "input_schema": s.input_schema,
             "risks": s.risks, "recovery": s.recovery} for s in adapter.list_tools(actor)
            if s.name in ("aigc.image", "aigc.generate", "aigc.speak", "aigc.lipsync")]


class FakeNode:
    """Answers /tools, /assets and /execute like a generation node, with synthetic media."""

    endpoint = "http://gen.test"

    def __init__(self, tmp: Path) -> None:
        self.capabilities = node_capabilities(tmp)
        self.files: dict[str, bytes] = {}
        self.executions: list[dict[str, Any]] = []
        self.hold = threading.Event()
        self.hold.set()
        self.online = True
        self.fail_execute = False

    def _store(self, data: bytes, suffix: str) -> tuple[str, str]:
        sha = hashlib.sha256(data).hexdigest()
        ref = f"out-{sha[:16]}{suffix}"
        self.files[ref] = data
        return ref, sha

    def get(self, url: str, **_: Any) -> httpx.Response:
        if not self.online:
            raise httpx.ConnectError("offline")
        if url.endswith("/tools"):
            return httpx.Response(200, json=self.capabilities)
        ref = url.rsplit("/", 1)[-1]
        return httpx.Response(200, content=self.files[ref]) if ref in self.files else httpx.Response(404)

    def post(self, url: str, **kw: Any) -> httpx.Response:
        if not self.online:
            raise httpx.ConnectError("offline")
        if url.endswith("/assets"):
            data = kw["content"]
            ref, sha = self._store(data, Path(kw["headers"]["X-Asset-Filename"]).suffix)
            return httpx.Response(200, json={"asset_id": ref, "sha256": sha})
        body = kw["json"]
        self.executions.append(body)
        self.hold.wait(timeout=30)
        if self.fail_execute:
            return httpx.Response(500, json={"error": "synthetic failure"})
        tool, args = body["tool_name"], body["args"]
        if tool == "aigc.image":
            data, mime, suffix = png_bytes(args["width"] // 8, args["height"] // 8,
                                           (args["seed"] % 255, 120, 160)), "image/png", ".png"
        elif tool == "aigc.generate":
            assert args["input_asset_ref"] in self.files, "start frame must be pushed first"
            data, mime, suffix = mp4_bytes(160, 288, args["frames"] / args["fps"]), "video/mp4", ".mp4"
        elif tool == "aigc.speak":
            assert args["input_asset_ref"] in self.files, "reference voice must be pushed first"
            data, mime, suffix = wav_bytes(max(0.6, len(args["text"]) / 6)), "audio/wav", ".wav"
        else:
            data, mime, suffix = mp4_bytes(160, 288, 2.0), "video/mp4", ".mp4"
        ref, sha = self._store(data, suffix)
        return httpx.Response(200, json={"status": "completed", "value": {
            "asset_ref": ref, "sha256": sha, "job_id": "job-" + ref[4:12],
            "assets": [{"asset_ref": ref, "sha256": sha, "mime": mime, "role": "output"}]}})


class DirectorCalls(list[str]):
    """Audited purposes of scripted director calls, plus the model each call asked for."""

    def __init__(self) -> None:
        super().__init__()
        self.models: list[str] = []

    def record(self, kw: dict[str, Any]) -> None:
        self.append(kw["purpose"])
        self.models.append(str(kw.get("model_ref") or ""))


def script_director(handle: SystemHandle, monkeypatch: pytest.MonkeyPatch) -> DirectorCalls:
    purposes = DirectorCalls()

    async def complete_json(actor: AuthContext, messages: list[dict[str, Any]], schema: dict[str, Any],
                            **kw: Any) -> dict[str, Any]:
        purposes.record(kw)
        properties = schema.get("properties", {})
        if "characters" in properties:
            return json.loads(json.dumps(CAST))
        if "shots" in properties:
            scene = "场2" if "场2" in messages[-1]["content"] else "场1"
            return json.loads(json.dumps(SHOTS[scene]))
        return {"image_prompt": "refined still", "motion_prompt": "refined motion"}

    async def stream_text(actor: AuthContext, messages: list[dict[str, Any]], **kw: Any) -> AsyncIterator[str]:
        purposes.record(kw)
        for line in SCRIPT.splitlines(keepends=True):
            await asyncio.sleep(0)
            yield line

    monkeypatch.setattr(handle.chat, "complete_json", complete_json)
    monkeypatch.setattr(handle.chat, "stream_text", stream_text)
    return purposes


@pytest.fixture
def studio(make_system: Callable[..., SystemHandle], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
           ) -> tuple[SystemHandle, FakeNode, DirectorCalls]:
    node = FakeNode(tmp_path / "node")
    monkeypatch.setattr(httpx, "get", node.get)
    monkeypatch.setattr(httpx, "post", node.post)
    handle = make_system(workflow_engine="local", studio_enabled=True, studio_node_id="gen")
    return handle, node, script_director(handle, monkeypatch)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def wait_view(c: TestClient, token: str, pid: str, done: Callable[[dict[str, Any]], bool],
              timeout: float = 30) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        view = c.get(f"/v1/studio/projects/{pid}", headers=auth(token)).json()
        if done(view):
            return view
        assert time.monotonic() < deadline, json.dumps(view["jobs"][:6], ensure_ascii=False)
        time.sleep(0.05)


def settled(view: dict[str, Any]) -> bool:
    return bool(view["jobs"]) and all(j["state"] not in ("queued", "running") for j in view["jobs"])


def pair(c: TestClient, token: str) -> None:
    r = c.post("/v1/nodes/pair", headers=auth(token), json={"node_id": "gen", "endpoint": FakeNode.endpoint,
                                                            "os": "linux"})
    assert r.status_code == 200, r.text


def test_project_to_film_with_approvals_grants_and_provenance(studio: tuple[SystemHandle, FakeNode, list[str]]
                                                              ) -> None:
    handle, node, purposes = studio
    with TestClient(create_app(system=handle)) as c:
        token = owner_token(handle)
        pair(c, token)
        overview = c.get("/v1/studio/overview", headers=auth(token)).json()
        assert overview["node"]["state"] == "online" and overview["workflow"]["available"]
        engines = {e["id"]: e for e in overview["engines"]}
        assert engines["klein"]["available"] and engines["wan22-fast"]["available"]
        assert overview["permissions"]["needs_approval"] and overview["permissions"]["can_grant"]

        created = c.post("/v1/studio/projects", headers=auth(token), json={"title": "灯塔与猫", "script": SCRIPT})
        assert created.status_code == 201, created.text
        pid = created.json()["project"]["id"]
        job = c.post(f"/v1/studio/projects/{pid}/director/breakdown", headers=auth(token), json={}).json()["job"]
        assert job["kind"] == "director:breakdown"
        view = wait_view(c, token, pid, lambda v: len(v["project"]["shots"]) == 2 and settled(v))
        assert {ch["name"] for ch in view["project"]["characters"]} == {"老周", "小满"}
        assert purposes == ["studio.director"] * 3   # cast + one storyboard per scene, audited purpose
        shots = view["project"]["shots"]
        first, second = shots[0]["id"], shots[1]["id"]

        # Node tools are governed steps: without a grant the first candidate waits for approval.
        jobs = c.post(f"/v1/studio/projects/{pid}/generate", headers=auth(token),
                      json={"kind": "frame", "target_ids": [first]}).json()["jobs"]
        view = wait_view(c, token, pid, lambda v: any(j["state"] == "approval" for j in v["jobs"]))
        pending = next(j for j in view["jobs"] if j["state"] == "approval")
        assert pending["id"] == jobs[0]["id"] and pending["approval_id"] and not node.executions
        decided = c.post(f"/v1/approvals/{pending['approval_id']}/decision", headers=auth(token),
                         json={"decision": "approve"})
        assert decided.status_code == 200
        view = wait_view(c, token, pid, lambda v: f"frame:{first}" in v["selections"])
        frame_take = view["selections"][f"frame:{first}"]
        assert frame_take == pending["id"]   # first candidate is used until someone chooses
        run_id = pending["run_id"]
        task = c.get(f"/v1/tasks/{run_id}", headers=auth(token)).json()
        assert task["run"]["goal_text"].startswith("灯塔与猫 · ")
        assert [s["tool_name"] for s in task["steps"]] == ["node_gen_aigc_image"]

        # The owner pre-authorizes the node's production tools; later batches run unattended.
        granted = c.post("/v1/studio/permissions", headers=auth(token), json={"hours": 24})
        assert granted.status_code == 201, granted.text
        assert {g["tool_name"] for g in granted.json()["grants"]} == {
            "node_gen_aigc_image", "node_gen_aigc_generate", "node_gen_aigc_speak", "node_gen_aigc_lipsync"}
        assert not c.get("/v1/studio/overview", headers=auth(token)).json()["permissions"]["needs_approval"]

        result = c.post(f"/v1/studio/projects/{pid}/generate", headers=auth(token),
                        json={"kind": "frame", "target_ids": [first, second], "count": 2}).json()
        assert [j["label"] for j in result["jobs"]][:2] == ["第 1 镜画面 · 候选 1", "第 1 镜画面 · 候选 2"]
        view = wait_view(c, token, pid, settled)
        frames = [t for t in view["takes"] if t["slot"] == "frame"]
        assert len(frames) == 5 and all(t["thumb"] and t["mime"] == "image/png" for t in frames)
        assert view["selections"][f"frame:{first}"] == frame_take
        newer = next(t["id"] for t in frames if t["target_id"] == first and t["id"] != frame_take)
        assert c.post(f"/v1/studio/projects/{pid}/select", headers=auth(token),
                      json={"slot": "frame", "target_id": first, "take_id": newer}).json()[
            "selections"][f"frame:{first}"] == newer
        executed = [e for e in node.executions if e["tool_name"] == "aigc.image"]
        assert executed[-1]["idempotency_key"].endswith(":flow-n004")   # ledger key per node step

        # Voices need a reference voice per speaker; missing ones are reported, not failed.
        lines = [shots[0]["lines"][0]["id"], shots[1]["lines"][0]["id"]]
        speaker = shots[0]["lines"][0]["character_id"]
        up = c.post(f"/v1/studio/projects/{pid}/upload?slot=voice_ref&target_id={speaker}",
                    headers={**auth(token), "Content-Type": "audio/wav", "X-File-Name": "ref.wav"},
                    content=wav_bytes(1.0))
        assert up.status_code == 201, up.text
        voiced = c.post(f"/v1/studio/projects/{pid}/generate", headers=auth(token),
                        json={"kind": "voice", "target_ids": lines}).json()
        assert len(voiced["jobs"]) == 1 and "没有参考音" in voiced["skipped"][0]
        clip = c.post(f"/v1/studio/projects/{pid}/generate", headers=auth(token),
                      json={"kind": "clip", "target_ids": [first]}).json()["jobs"][0]
        assert clip["engine"] == "wan22-fast"
        view = wait_view(c, token, pid, settled)
        voice = next(t for t in view["takes"] if t["slot"] == "voice")
        assert voice["meta"]["duration"] > 0.5 and voice["meta"]["character_id"] == speaker
        clip_take = next(t for t in view["takes"] if t["slot"] == "clip")
        assert clip_take["mime"] == "video/mp4" and clip_take["poster_url"]
        generate_call = next(e for e in node.executions if e["tool_name"] == "aigc.generate")
        frame_asset = next(t["asset_id"] for t in view["takes"] if t["id"] == newer)
        assert generate_call["args"]["input_asset_id"] == frame_asset   # made from the chosen frame

        # Edits keep their own revision: choosing takes does not invalidate a pending edit.
        project = view["project"]
        project["shots"][1]["action"] = "小满把额头贴在玻璃上"
        saved = c.put(f"/v1/studio/projects/{pid}", headers=auth(token),
                      json={"revision": project["revision"], "project": project})
        assert saved.status_code == 200 and saved.json()["project"]["revision"] == project["revision"] + 1
        stale = c.put(f"/v1/studio/projects/{pid}", headers=auth(token),
                      json={"revision": project["revision"], "project": project})
        assert stale.status_code == 409

        # Final cut: a custom node composes picked media on the control plane.
        assert c.post(f"/v1/studio/projects/{pid}/render", headers=auth(token),
                      json={"quality": "preview"}).status_code == 202
        view = wait_view(c, token, pid, lambda v: f"render:{pid}" in v["selections"] and settled(v), timeout=90)
        film = next(t for t in view["takes"] if t["slot"] == "render")
        assert film["meta"]["missing_frames"] == 0 and film["srt_url"] and film["poster_url"]
        assert film["meta"]["width"] == 540 and film["meta"]["height"] == 960
        media = c.get(film["url"])
        assert media.status_code == 200 and media.headers["content-type"] == "video/mp4"
        thumb = c.get(frames[0]["thumb"])
        assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg"
        poster = c.get(clip_take["poster_url"])
        assert poster.status_code == 200 and poster.headers["content-type"] == "image/jpeg"
        assert c.get(film["url"].replace("sig=", "sig=0")).status_code == 403

        # Hiding a take falls back to the next candidate; ratings are kept per take.
        assert c.post(f"/v1/studio/projects/{pid}/takes/{newer}/rate", headers=auth(token),
                      json={"rating": 4}).status_code == 200
        assert c.delete(f"/v1/studio/projects/{pid}/takes/{newer}", headers=auth(token)).status_code == 200
        view = c.get(f"/v1/studio/projects/{pid}", headers=auth(token)).json()
        assert view["selections"][f"frame:{first}"] == frame_take

        # Projects are private drafts and runs belong to their subject.
        other = add_subject(handle, "second", ["read", "write", "exec"])
        assert c.get("/v1/studio/projects", headers=auth(other)).json() == []
        assert c.get(f"/v1/studio/projects/{pid}", headers=auth(other)).status_code == 422
        assert c.post(f"/v1/studio/jobs/{clip['id']}/cancel", headers=auth(other)).status_code == 422
        audit = [e.kind for e in handle.store.audit_query("default", limit=5000)]
        assert "policy.grant_issued" in audit and "tool.called" in audit and "workflow.started" in audit


def test_one_click_production_chains_stage_runs(studio: tuple[SystemHandle, FakeNode, DirectorCalls]) -> None:
    handle, node, purposes = studio
    with TestClient(create_app(system=handle)) as c:
        token = owner_token(handle)
        pair(c, token)
        assert c.post("/v1/studio/permissions", headers=auth(token), json={"hours": 1}).status_code == 201
        created = c.post("/v1/studio/projects", headers=auth(token),
                         json={"title": "灯塔与猫", "premise": "风夜里，守灯人和孙女等一条迟归的渔船。"}).json()
        pid, project = created["project"]["id"], created["project"]
        assert project["engines"]["director"] == ""
        project["engines"]["director"] = "writer/story-model"   # the project's director model
        assert c.put(f"/v1/studio/projects/{pid}", headers=auth(token),
                     json={"revision": project["revision"], "project": project}).status_code == 200
        started = c.post(f"/v1/studio/projects/{pid}/autopilot", headers=auth(token), json={})
        assert started.status_code == 202, started.text
        assert started.json()["jobs"][0]["kind"] == "director:script"
        view = wait_view(c, token, pid, lambda v: f"render:{pid}" in v["selections"], timeout=120)
        project = view["project"]
        assert project["script"].startswith("# 灯塔与猫") and len(project["shots"]) == 2
        kinds = [j["kind"] for j in view["jobs"]]
        for kind in ("director:script", "director:breakdown", "char_ref", "loc_ref", "frame", "clip", "render"):
            assert kind in kinds
        assert "voice" not in kinds   # no reference voices yet: skipped with a note
        notes = [n for j in view["jobs"] if j["kind"].startswith("stage:") for n in j["result"].get("skipped", [])]
        assert any("没有参考音" in n for n in notes)
        assert purposes.count("studio.director") == 4
        assert purposes.models == ["writer/story-model"] * 4   # one-click steps use the project's model
        frame_call = next(e for e in node.executions if e["tool_name"] == "aigc.image"
                          and len(e["args"].get("reference_asset_ids", [])) >= 1)
        assert frame_call["args"]["recipe"] == "klein-image"   # frames use the picked references
        runs = {j["run_id"] for j in view["jobs"]}
        assert len(runs) >= 5
        # Retrying a stage continuation starts the pipeline again from that stage only.
        stage_job = next(j for j in view["jobs"] if j["kind"] == "stage:render")
        retried = c.post(f"/v1/studio/jobs/{stage_job['id']}/retry", headers=auth(token))
        assert retried.status_code == 202 and retried.json()["jobs"][0]["kind"] == "render"


def test_cancel_retry_and_unavailable_node(studio: tuple[SystemHandle, FakeNode, list[str]]) -> None:
    handle, node, _purposes = studio
    with TestClient(create_app(system=handle)) as c:
        token = owner_token(handle)
        pair(c, token)
        assert c.post("/v1/studio/permissions", headers=auth(token), json={"hours": 1}).status_code == 201
        pid = c.post("/v1/studio/projects", headers=auth(token), json={"title": "取消测试", "script": SCRIPT}).json()[
            "project"]["id"]
        c.post(f"/v1/studio/projects/{pid}/director/breakdown", headers=auth(token), json={})
        view = wait_view(c, token, pid, lambda v: len(v["project"]["shots"]) == 2 and settled(v))
        targets = [s["id"] for s in view["project"]["shots"]]
        node.hold.clear()
        jobs = c.post(f"/v1/studio/projects/{pid}/generate", headers=auth(token),
                      json={"kind": "frame", "target_ids": targets}).json()["jobs"]
        wait_view(c, token, pid, lambda v: any(j["id"] == jobs[0]["id"] and j["state"] == "running"
                                               for j in v["jobs"]))
        cancelled = c.post(f"/v1/studio/jobs/{jobs[1]['id']}/cancel", headers=auth(token))
        assert cancelled.status_code == 200, cancelled.text
        node.hold.set()
        view = wait_view(c, token, pid, lambda v: {j["state"] for j in v["jobs"] if j["run_id"] == jobs[0]["run_id"]}
                         <= {"cancelled", "succeeded"})
        assert next(j for j in view["jobs"] if j["id"] == jobs[1]["id"])["state"] == "cancelled"
        retried = c.post(f"/v1/studio/jobs/{jobs[1]['id']}/retry", headers=auth(token)).json()["jobs"]
        assert retried[0]["target_id"] == targets[1] and retried[0]["run_id"] != jobs[0]["run_id"]
        wait_view(c, token, pid, lambda v: f"frame:{targets[1]}" in v["selections"])

        # A node error leaves the step unsettled (the node may have started): reconcile replays the same key.
        node.fail_execute = True
        failing = c.post(f"/v1/studio/projects/{pid}/generate", headers=auth(token),
                         json={"kind": "frame", "target_ids": [targets[0]]}).json()["jobs"][0]
        view = wait_view(c, token, pid, lambda v: any(j["id"] == failing["id"] and j["state"] == "blocked"
                                                      for j in v["jobs"]))
        blocked = next(j for j in view["jobs"] if j["id"] == failing["id"])
        assert "生成节点执行出错" in blocked["error"] and "核对并继续" in blocked["error"]
        node.fail_execute = False
        reconciled = c.post(f"/v1/tasks/{failing['run_id']}/reconcile", headers=auth(token),
                            json={"step_id": blocked["step_id"]})
        assert reconciled.status_code == 202, reconciled.text
        wait_view(c, token, pid, lambda v: any(j["id"] == failing["id"] and j["state"] == "succeeded"
                                               for j in v["jobs"]))
        keys = [e["idempotency_key"] for e in node.executions if e["run_id"] == failing["run_id"]]
        assert len(keys) == 2 and keys[0] == keys[1]

        node.online = False
        handle.studio._node_cache = None
        offline = c.post(f"/v1/studio/projects/{pid}/generate", headers=auth(token),
                         json={"kind": "frame", "target_ids": [targets[0]]})
        assert offline.status_code == 503 and "离线" in offline.json()["message"]
        assert c.get("/v1/studio/overview", headers=auth(token)).json()["node"]["state"] == "offline"


def test_studio_explains_missing_engine_and_node(make_system: Callable[..., SystemHandle],
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    handle = make_system(studio_enabled=True)   # no workflow engine, no generation node
    script_director(handle, monkeypatch)
    with TestClient(create_app(system=handle)) as c:
        token = owner_token(handle)
        overview = c.get("/v1/studio/overview", headers=auth(token)).json()
        assert overview["node"]["state"] == "unconfigured" and not overview["workflow"]["available"]
        assert all(not e["available"] and e["reason"] for e in overview["engines"])
        pid = c.post("/v1/studio/projects", headers=auth(token), json={"title": "草稿", "script": SCRIPT}).json()[
            "project"]["id"]
        r = c.post(f"/v1/studio/projects/{pid}/director/breakdown", headers=auth(token), json={})
        assert r.status_code == 503 and "workflow_engine" in r.json()["message"]
        r = c.post(f"/v1/studio/projects/{pid}/generate", headers=auth(token),
                   json={"kind": "frame", "target_ids": ["x"]})
        assert r.status_code == 503 and "生成节点" in r.json()["message"]
        up = c.post(f"/v1/studio/projects/{pid}/upload?slot=style_ref&target_id={pid}",
                    headers={**auth(token), "Content-Type": "image/png"}, content=png_bytes(64, 64, (9, 9, 9)))
        assert up.status_code == 201 and up.json()["origin"] == "upload"
        bad = c.post(f"/v1/studio/projects/{pid}/upload?slot=style_ref&target_id={pid}",
                     headers={**auth(token), "Content-Type": "audio/wav"}, content=wav_bytes(0.5))
        assert bad.status_code == 422
        assert c.get("/v1/studio/projects", headers=auth(token)).json()[0]["id"] == pid
