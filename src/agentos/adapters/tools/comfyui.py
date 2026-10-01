"""ComfyUI 受限 Adapter（步骤 3）：按需启动、健康/队列检查、提交、查询、
取真实输出、取消/超时、异常对账、空闲停机。

- 只暴露 comfyui.* 三个工具：generate / status / stop；不开放任意 graph 执行——
  graph 由版本化模板（Windows 既有 z_image/qwen_gguf）参数化，prompt 长度受限。
- generate 流程：探针→(按需启动)→POST /prompt→轮询 /history→GET /view 取字节
  →写入节点沙箱 assets→返回 {asset_ref, sha256, …}（产物经 AgentOS Asset 引用回传）。
- 超时/连接丢失：抛 TIMEOUT（side_effect_unknown）→ gateway 侧转 uncertain 对账，
  不盲重试（FR-ORC-08）。提交成功但响应丢失时按 client_id 查 /history 对账。
- ComfyUI 0.37 接口已对照官方 server.py：/prompt、/history/{id}、/view、/interrupt、
  /system_stats、/queue。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import random
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import AuthContext, CallContext, ToolSpec

_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
_MAX_PROMPT = 4000
_TIMEOUT_S = float(os.environ.get("AGENTOS_COMFYUI_TIMEOUT", "2400"))


def _http(method: str, url: str, body: dict[str, Any] | None = None, timeout: float = 20) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method=method
    )
    try:
        with _NO_PROXY.open(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raise AgentOSError(ErrorCode.UNAVAILABLE, f"comfyui HTTP {exc.code}: {url}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise AgentOSError(ErrorCode.UNAVAILABLE, f"comfyui unreachable: {exc}") from exc


class ComfyUIToolAdapter:
    """Windows 节点上的 ComfyUI 受限执行器。"""

    def __init__(
        self, *, base_url: str, workflows_dir: Path, sandbox_dir: Path,
        start_cmd: list[str] | None = None, start_cwd: Path | None = None,
        start_timeout_s: float = 120,
        idle_stop_s: int = 0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._wf_dir = workflows_dir
        self._sandbox = sandbox_dir
        self._start_cmd = start_cmd
        self._start_cwd = start_cwd
        self._start_timeout = start_timeout_s
        self._idle_stop_s = idle_stop_s
        # 生成完硬停进程（12G 显存卡社区协议：/free 软卸载有 CUDA 碎片残留，
        # 杀进程是唯一彻底释放方式）；下一任务按需拉起。
        self._stop_after_task = bool(int(os.environ.get("AGENTOS_COMFYUI_STOP_AFTER", "1")))
        self._last_used = 0.0
        self._client_id = f"agentos-node-{uuid.uuid4().hex[:8]}"
        self._started_by_us = False
        self._templates: dict[str, dict[str, Any]] = {}
        self._load_templates()

    def _load_templates(self) -> None:
        """加载版本化工作流模板（API graph）；模板文件名即模型名。"""
        if not self._wf_dir.is_dir():
            return
        for f in sorted(self._wf_dir.glob("*.json")):
            try:
                self._templates[f.stem] = json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue

    def has_tool(self, name: str) -> bool:
        return name in {"comfyui.generate", "comfyui.status", "comfyui.stop", "comfyui.probe"}

    # ------------------------------------------------------------- 生命周期
    async def _free_vram(self) -> None:
        """干完即卸：释放 ComfyUI 驻留模型显存，给同 GPU 的其他服务（Ollama 等）让路。"""
        with contextlib.suppress(AgentOSError):
            await asyncio.to_thread(
                _http, "POST", self._base + "/free",
                {"unload_models": True, "free_memory": True}, 15)  # 卸载失败不影响产物交付

    async def _alive(self) -> bool:
        try:
            await asyncio.to_thread(_http, "GET", self._base + "/system_stats", None, 5)
            return True
        except AgentOSError:
            return False

    async def _ensure_running(self) -> None:
        if await self._alive():
            return
        if not self._start_cmd:
            raise AgentOSError(
                ErrorCode.UNAVAILABLE,
                "comfyui not running and no start command configured",
            )
        subprocess.Popen(
            self._start_cmd, cwd=str(self._start_cwd) if self._start_cwd else None,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._started_by_us = True
        deadline = time.monotonic() + self._start_timeout
        while time.monotonic() < deadline:
            if await self._alive():
                return
            await asyncio.sleep(2.0)
        raise AgentOSError(ErrorCode.TIMEOUT, "comfyui start timeout")

    def stop(self) -> dict[str, Any]:
        """空闲停机：仅当 ComfyUI 是本节点拉起的才停，避免影响人工使用。

        硬停协议（dev-notes #32）：按端口 netstat 找 PID → taskkill，分配全归还驱动。
        （曾用 taskkill /FI WINDOWTITLE 过滤——无窗口的 python 进程匹配不到，从未生效。）
        """
        if not self._started_by_us:
            return {"stopped": False, "reason": "not started by node agent"}
        port = self._base.rsplit(":", 1)[-1]
        killed: list[str] = []
        try:
            out = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True, timeout=20,
            ).stdout
            pids = set()
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[-1].isdigit() and parts[-1] != "0" \
                        and f":{port}" in parts[1] and parts[3].upper() == "LISTENING":
                    pids.add(parts[-1])
            for pid in pids:
                subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=15)
                killed.append(pid)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"stopped": False, "reason": f"stop failed: {exc}"}
        self._started_by_us = False
        return {"stopped": bool(killed), "pids": killed}

    # ------------------------------------------------------------- 工具
    def list_tools(self, actor: AuthContext) -> list[ToolSpec]:
        models = ", ".join(sorted(self._templates)) or "none"
        return [
            ToolSpec(
                name="comfyui.generate",
                version="1.0.0",
                description=f"ComfyUI 文生图（模型: {models}）。产物以 asset_ref+sha256 返回。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string"},
                        "model": {"type": "string", "enum": sorted(self._templates)},
                        "width": {"type": "integer", "minimum": 256, "maximum": 2048},
                        "height": {"type": "integer", "minimum": 256, "maximum": 2048},
                        "seed": {"type": "integer"},
                        "input_asset_ref": {"type": "string",
                                            "description": "图生图时已 push 到节点的输入图片 asset_ref"},
                    },
                    "required": ["prompt"],
                    "additionalProperties": False,
                },
                risks=["read", "write", "exec"],
                recovery="manual",
                timeout_ms=int(_TIMEOUT_S * 1000),
            ),
            ToolSpec(
                name="comfyui.status", version="1.0.0",
                description="ComfyUI 健康/队列状态（探针）",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                risks=["read"], recovery="pure", timeout_ms=10_000,
            ),
            ToolSpec(
                name="comfyui.stop", version="1.0.0",
                description="空闲停机：停止本节点拉起的 ComfyUI 释放显存",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                risks=["write"], recovery="manual", timeout_ms=15_000,
            ),
            ToolSpec(
                name="comfyui.probe", version="1.0.0",
                description="对账探针：按幂等标签查外部生成是否已完成"
                            "（found/in_flight）；完成则补传产物，不重新生成",
                input_schema={
                    "type": "object",
                    "properties": {"idem_tag": {"type": "string"}},
                    "required": ["idem_tag"],
                    "additionalProperties": False,
                },
                risks=["read"], recovery="pure", timeout_ms=60_000,
            ),
        ]

    async def call(self, name: str, args: dict[str, Any], ctx: CallContext) -> Any:
        if name == "comfyui.status":
            # 节点 agent 以 ToolResult(call_id, **value) 包装回执：必须给
            # {status, value} 标准信封——裸 dict（无 status 或 status 值不在
            # completed/pending/input_required/failed/uncertain 枚举内）会
            # ValidationError→HTTP 500→网关侧 UNAVAILABLE→步骤误转 uncertain。
            return {"status": "completed", "value": await self._status()}
        if name == "comfyui.stop":
            return {"status": "completed", "value": await asyncio.to_thread(self.stop)}
        if name == "comfyui.probe":
            return await self._probe(args)
        if name == "comfyui.generate":
            return await self._generate(args, ctx)
        raise AgentOSError(ErrorCode.INVALID, f"unknown comfyui tool: {name}")

    async def _probe(self, args: dict[str, Any]) -> dict[str, Any]:
        """对账探针（FR-ORC-08）：效果未知时查外部真实状态，而不是盲重试。

        返回 value.found / value.in_flight 供 gateway 对账分支决策：
        - found=True：外部已完成 → 顺带补传产物（等价 generate 的 completed 结果）
        - in_flight=True：外部仍在执行 → 对账方必须等待，禁止此刻重放
        - 两者皆 False：无外部效果证据 → 可安全授权重放
        """
        idem = str(args.get("idem_tag") or "")
        if not idem:
            raise AgentOSError(ErrorCode.INVALID, "probe requires idem_tag")
        if not await self._alive():
            return {"status": "completed", "value": {"found": False, "in_flight": False,
                                                     "note": "comfyui not running"}}
        prior = await asyncio.to_thread(self._find_history, idem)
        if prior is None:
            return {"status": "completed", "value": {"found": False, "in_flight": False,
                                                     "note": "no matching prompt in history"}}
        entry = (await asyncio.to_thread(
            _http, "GET", self._base + f"/history/{prior}", None, 10)).get(prior)
        if not entry:
            return {"status": "completed", "value": {"found": False, "in_flight": False}}
        status = entry.get("status", {})
        if status.get("status_str") == "error":
            return {"status": "completed", "value": {
                "found": False, "in_flight": False, "note": "external generation errored"}}
        if not status.get("completed"):
            return {"status": "completed", "value": {"found": False, "in_flight": True,
                                                     "note": "generation still running"}}
        images = self._collect_images(entry.get("outputs", {}))
        if not images:
            return {"status": "completed", "value": {"found": False, "in_flight": False,
                                                     "note": "completed but no output files"}}
        result = await self._download_to_asset(images[0], prior, idem, "reconciled")
        return {"status": "completed", "value": {"found": True, "in_flight": False,
                                                 "result": result}}

    async def _status(self) -> dict[str, Any]:
        if not await self._alive():
            return {"status": "stopped", "models": sorted(self._templates)}
        stats: dict[str, Any] = await asyncio.to_thread(_http, "GET", self._base + "/system_stats")
        queue: dict[str, Any] = await asyncio.to_thread(_http, "GET", self._base + "/queue")
        return {
            "status": "running",
            "version": stats.get("system", {}).get("comfyui_version"),
            "queue_running": len(queue.get("queue_running", [])),
            "queue_pending": len(queue.get("queue_pending", [])),
            "models": sorted(self._templates),
        }

    def _parametrize(self, model: str, args: dict[str, Any]) -> dict[str, Any]:
        template = self._templates.get(model)
        if template is None:
            raise AgentOSError(
                ErrorCode.INVALID,
                f"unknown model {model!r}; available: {sorted(self._templates)}",
            )
        prompt_text = str(args.get("prompt") or "").strip()
        if not prompt_text or len(prompt_text) > _MAX_PROMPT:
            raise AgentOSError(ErrorCode.INVALID, "prompt empty or too long")
        graph: dict[str, Any] = json.loads(json.dumps(template))  # deep copy
        width = int(args.get("width") or 1024)
        height = int(args.get("height") or 1024)
        seed = int(args.get("seed") or random.randint(0, 2**31 - 1))
        for node in graph.values():
            inputs = node.get("inputs", {})
            if "text" in inputs and isinstance(inputs["text"], str) and inputs["text"]:
                inputs["text"] = prompt_text          # 正向提示词节点
            elif node.get("class_type") == "EmptySD3LatentImage":
                inputs["width"], inputs["height"] = width, height
            if node.get("class_type") == "KSampler":
                inputs["seed"] = seed
        return graph

    async def _upload_input_image(self, asset_ref: str) -> str:
        """把节点沙箱 assets 里的图片上传到 ComfyUI input 目录，返回服务端文件名。"""
        import mimetypes
        import uuid as _uuid

        path = (self._sandbox / "assets" / asset_ref).resolve()
        if not path.exists() or not path.is_relative_to((self._sandbox / "assets").resolve()):
            raise AgentOSError(ErrorCode.INVALID, f"input image asset not found: {asset_ref}")
        boundary = f"----agentos{_uuid.uuid4().hex}"
        fname = f"agentos_{_uuid.uuid4().hex[:8]}_{Path(asset_ref).name}"
        mime = mimetypes.guess_type(fname)[0] or "image/png"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="image"; filename="{fname}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            self._base + "/upload/image", data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        def _up() -> dict[str, Any]:
            with _NO_PROXY.open(req, timeout=30) as r:
                parsed: dict[str, Any] = json.loads(r.read())
                return parsed

        try:
            resp = await asyncio.to_thread(_up)
        except (urllib.error.URLError, OSError) as exc:
            raise AgentOSError(ErrorCode.UNAVAILABLE, f"input image upload failed: {exc}") from exc
        return str(resp.get("name") or fname)

    async def _generate(self, args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        model = str(args.get("model") or (sorted(self._templates)[0] if self._templates else ""))
        graph = self._parametrize(model, args)
        input_ref = args.get("input_asset_ref")
        if input_ref:
            uploaded = await self._upload_input_image(str(input_ref))
            for node in graph.values():
                if node.get("class_type") == "LoadImage":
                    node["inputs"]["image"] = uploaded
        await self._ensure_running()

        # 提交（幂等：同 idempotency_key 先查历史，防重复生成）
        idem = f"agentos-{ctx.idempotency_key}"[:60]
        prior = await asyncio.to_thread(self._find_history, idem)
        if prior is None:
            try:
                resp = await asyncio.to_thread(
                    _http, "POST", self._base + "/prompt",
                    {"prompt": graph, "client_id": self._client_id,
                     "extra_data": {"agentos_idem": idem}})
            except AgentOSError as exc:
                if exc.code == ErrorCode.UNAVAILABLE:
                    raise AgentOSError(ErrorCode.TIMEOUT, "comfyui submit lost") from exc
                raise
            prompt_id = str(resp.get("prompt_id") or "")
            if not prompt_id:
                raise AgentOSError(ErrorCode.UNCERTAIN, "comfyui submit without prompt_id")
        else:
            prompt_id, self._started_by_us = prior, self._started_by_us

        # 轮询历史（deadline 由派发方传入；到点=TIMEOUT，效果未知转对账）
        deadline = min(
            time.monotonic() + _TIMEOUT_S,
            ctx.deadline_at.timestamp() if ctx.deadline_at else time.monotonic() + _TIMEOUT_S,
        )
        while time.monotonic() < deadline:
            history: dict[str, Any] = await asyncio.to_thread(
                _http, "GET", self._base + f"/history/{prompt_id}", None, 10)
            entry = history.get(prompt_id)
            if entry:
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    if self._stop_after_task:
                        await asyncio.to_thread(self.stop)
                    return {"status": "failed", "value": {
                        "message": "comfyui generation error",
                        "detail": str(status.get("messages", []))[:500],
                    }}
                outputs = entry.get("outputs", {})
                images = self._collect_images(outputs)
                if images:
                    result = await self._download_to_asset(images[0], prompt_id, idem, model)
                    if self._stop_after_task:
                        await asyncio.to_thread(self.stop)  # 硬停：彻底归还显存
                    return result
            time.sleep(2.0)
        raise AgentOSError(ErrorCode.TIMEOUT, f"comfyui generation timeout: {prompt_id}")

    def _find_history(self, idem_tag: str) -> str | None:
        """对账：提交成功但响应丢失时，按 extra_data.agentos_idem 找回 prompt_id。

        （0.37 的 history entry["prompt"] = [number, graph, extra_data]——extra_data 是
        dict，提交时把幂等标签放进 extra_data 才能回查；曾误比较 dict==str 恒 False。）
        """
        try:
            history = _http("GET", self._base + "/history", timeout=10)
        except AgentOSError:
            return None
        if not isinstance(history, dict):
            return None
        for pid, entry in history.items():
            prompt_graph = entry.get("prompt", [])
            extra = prompt_graph[2] if len(prompt_graph) >= 3 else {}
            if isinstance(extra, dict) and extra.get("agentos_idem") == idem_tag:
                return str(pid)
        return None

    def _collect_images(self, outputs: dict[str, Any]) -> list[dict[str, str]]:
        found: list[dict[str, str]] = []
        for node_out in outputs.values():
            for key in ("images", "videos", "gifs", "animated"):  # 图/视频输出兼容
                for img in node_out.get(key, []):
                    # 0.37 SaveVideo 会在 animated 里放布尔标志（非文件项），跳过非 dict
                    if not isinstance(img, dict):
                        continue
                    if img.get("type") == "output" or key != "images":
                        found.append(img)
        return found

    async def _download_to_asset(
        self, image: dict[str, str], prompt_id: str, idem: str, model: str
    ) -> dict[str, Any]:
        from urllib.parse import urlencode

        url = self._base + "/view?" + urlencode({
            "filename": image["filename"],
            "subfolder": image.get("subfolder", ""),
            "type": image.get("type", "output"),
        })
        def _dl() -> bytes:
            with _NO_PROXY.open(urllib.request.Request(url), timeout=30) as r:
                got: bytes = r.read()
                return got

        try:
            data = await asyncio.to_thread(_dl)
        except (urllib.error.URLError, OSError) as exc:
            raise AgentOSError(ErrorCode.UNCERTAIN, f"output download failed: {exc}") from exc
        digest = sha256_bytes(data)
        # 后缀跟真实产物（图 png / 视频 mp4…），不能想当然——S05 视频曾因此存成 .png
        ext = Path(str(image.get("filename") or "")).suffix.lstrip(".") or "png"
        asset_ref = f"{prompt_id[:12]}_{model}_{digest[:10]}.{ext}"
        path = (self._sandbox / "assets" / asset_ref).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self._last_used = time.time()
        return {
            "status": "completed",
            "value": {
                "asset_ref": asset_ref,
                "sha256": digest,
                "size": len(data),
                "prompt_id": prompt_id,
                "model": model,
                "sandbox_path": str(path),
            },
        }
