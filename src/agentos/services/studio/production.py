"""Server-registered workflow nodes of the studio: director, final cut and one-click continuation.

Workflow runs call these through ``WorkflowDriver.custom`` with the run's refreshed identity,
so they read and write only what that person may. Failures are raised as ``AgentOSError`` with a
message meant for the person; the run records it and later nodes do not start.
"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import shutil
import time
from pathlib import Path
from typing import Any

from agentos.adapters.aigc import compositor
from agentos.domains.aigc.studio import director, plan
from agentos.domains.aigc.studio.models import Project, StudioDoc
from agentos.domains.aigc.studio.prompts import output_size
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import AuthContext
from agentos.services.studio.service import NO_FFMPEG, StudioService

PURPOSE = "studio.director"


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


class StudioNodes:
    def __init__(self, service: StudioService) -> None:
        self.service = service

    def register(self, custom: dict[str, Any]) -> None:
        custom["studio_director"] = self.director
        custom["studio_render"] = self.render
        custom["studio_next"] = self.next

    @staticmethod
    def _job(context: dict[str, Any]) -> tuple[AuthContext, str]:
        actor = context.get("actor")
        if not isinstance(actor, AuthContext):
            raise AgentOSError(ErrorCode.DENIED, "studio nodes need the run identity")
        return actor, f"{context['run_id']}.{context['node_id']}"

    def _save_project(self, actor: AuthContext, pid: str, change: Any) -> None:
        def apply(doc: StudioDoc) -> None:
            doc.project = change(doc.project)
            doc.revision += 1

        self.service.mutate(actor, pid, apply)
        self.service.publish(actor.tenant_id, actor.subject, {"type": "project", "project_id": pid})

    # ------------------------------------------------------------------ director
    async def director(self, inputs: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        actor, job_id = self._job(context)
        pid, task = str(inputs.get("project_id") or ""), str(inputs.get("task") or "")
        model = str(inputs.get("model") or "")
        service = self.service
        try:
            doc, _revision, _stamp = service.load(actor, pid)
            project = doc.project

            async def complete_json(messages: list[dict[str, Any]], schema: dict[str, Any]) -> dict[str, Any]:
                result: dict[str, Any] = await service.chat.complete_json(
                    actor, messages, schema, purpose=PURPOSE, model_ref=model, label="制作：" + task)
                return result

            async def progress(stage: str, done: int, total: int) -> None:
                service.report(job_id, stage=stage, done=done, total=total)

            if task == "script":
                return await self._script(actor, pid, project, str(inputs.get("instructions") or ""), model, job_id)
            if task == "breakdown":
                service.report(job_id, stage="读剧本")
                result = await director.breakdown(project, complete_json, progress)
                replace = bool(inputs.get("replace", True))

                def merge(latest: Project) -> Project:
                    shots = result.shots if replace or not latest.shots else [*latest.shots, *result.shots]
                    return latest.model_copy(update={"characters": result.characters, "locations": result.locations,
                                                     "shots": shots, "logline": latest.logline or result.logline})

                self._save_project(actor, pid, merge)
                return {"shots": len(result.shots), "characters": len(result.characters),
                        "locations": len(result.locations)}
            if task == "prompts":
                shot_ids = [str(s) for s in inputs.get("shot_ids") or [s.id for s in project.shots]]
                refined: dict[str, tuple[str, str]] = {}
                for index, shot_id in enumerate(shot_ids, start=1):
                    shot = project.shot(shot_id)
                    if shot is None:
                        continue
                    await progress(f"改写第 {index}/{len(shot_ids)} 镜提示词", index, len(shot_ids))
                    new = await director.refine_prompts(project, shot, complete_json)
                    refined[shot_id] = (new.image_prompt, new.motion_prompt)

                def apply_prompts(latest: Project) -> Project:
                    shots = [s.model_copy(update={"image_prompt": refined[s.id][0], "motion_prompt": refined[s.id][1]})
                             if s.id in refined else s for s in latest.shots]
                    return latest.model_copy(update={"shots": shots})

                self._save_project(actor, pid, apply_prompts)
                return {"shots": len(refined)}
            raise AgentOSError(ErrorCode.INVALID, "不支持的导演任务")
        except AgentOSError:
            raise
        except ValueError as exc:
            raise AgentOSError(ErrorCode.INVALID, f"导演助手的结果无法使用：{exc}") from exc
        except Exception as exc:  # noqa: BLE001 - model endpoints fail in many ways; report, don't retry blindly
            raise AgentOSError(ErrorCode.UNAVAILABLE, f"导演助手出错：{type(exc).__name__}: {exc}"[:300]) from exc
        finally:
            service.progress.pop(job_id, None)

    async def _script(self, actor: AuthContext, pid: str, project: Project, instructions: str, model: str,
                      job_id: str) -> dict[str, Any]:
        service = self.service
        service.report(job_id, stage="正在写剧本", text="")
        parts: list[str] = []
        last = 0.0
        async for chunk in service.chat.stream_text(actor, director.script_messages(project, instructions),
                                                     purpose=PURPOSE, model_ref=model, label="制作：写剧本"):
            parts.append(chunk)
            if time.monotonic() - last > 0.8:
                last = time.monotonic()
                service.report(job_id, text="".join(parts)[-4000:])
        script = _strip_fences("".join(parts))
        if not script.strip():
            raise AgentOSError(ErrorCode.UNAVAILABLE, "模型没有返回剧本内容")
        self._save_project(actor, pid, lambda latest: latest.model_copy(update={"script": script[:60000]}))
        return {"chars": len(script)}

    # ------------------------------------------------------------------ final cut
    async def render(self, inputs: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        actor, job_id = self._job(context)
        pid, quality = str(inputs.get("project_id") or ""), str(inputs.get("quality") or "preview")
        service = self.service
        ffmpeg = service.ffmpeg()
        if not ffmpeg:
            raise AgentOSError(ErrorCode.UNAVAILABLE, NO_FFMPEG)
        workdir = service.cache_dir / "renders" / job_id.replace(".", "-")
        try:
            service.report(job_id, stage="准备素材", fraction=0.0)
            now = service.state(actor, pid)
            project = now.doc.project
            if not project.shots:
                raise AgentOSError(ErrorCode.INVALID, "还没有分镜")
            plan_ = self._plan(actor, project, plan.timeline(project, now.picks), quality)
            output = workdir / "film.mp4"
            loop = asyncio.get_running_loop()

            def report(stage: str, fraction: float) -> None:
                loop.call_soon_threadsafe(functools.partial(service.report, job_id, stage=stage,
                                                            fraction=round(fraction, 3)))

            try:
                info = await asyncio.to_thread(compositor.render, plan_, ffmpeg, workdir / "work", output,
                                               progress=report)
            except compositor.RenderError as exc:
                raise AgentOSError(ErrorCode.INVALID, str(exc).replace(str(workdir), "…")[:300]) from exc
            label = "预览" if quality == "preview" else "成片"
            run_id = str(context["run_id"])
            film = service.assets.register_local(actor, output.read_bytes(), mime="video/mp4",
                                                 name=f"{project.title}-{label}.mp4", meta={"kind": "studio_render"},
                                                 source_node="studio", run_id=run_id)
            srt = service.assets.register_local(actor, Path(info["srt"]).read_bytes(), mime="text/plain",
                                                name=f"{project.title}.srt", meta={"kind": "studio_subtitles"},
                                                source_node="studio", run_id=run_id)
            poster = service.assets.register_local(actor, Path(info["poster"]).read_bytes(), mime="image/jpeg",
                                                   name=f"{project.title}.jpg", meta={"kind": "studio_poster"},
                                                   source_node="studio", run_id=run_id)
            return {"asset_id": film["asset_id"], "srt_asset_id": srt["asset_id"],
                    "poster_asset_id": poster["asset_id"], "quality": quality, "duration": info["duration"],
                    "width": info["width"], "height": info["height"], "fps": info["fps"], "shots": info["shots"],
                    "missing_frames": sum(1 for shot in plan_.shots if shot.kind == "card")}
        finally:
            service.progress.pop(job_id, None)
            shutil.rmtree(workdir, ignore_errors=True)

    def _plan(self, actor: AuthContext, project: Project, shots: list[dict[str, Any]],
              quality: str) -> compositor.RenderPlan:
        service = self.service

        def path(asset_id: str | None) -> Path | None:
            if not asset_id:
                return None
            with contextlib.suppress(AgentOSError):
                found = Path(str(service.assets.get_internal(actor, asset_id)["path"]))
                return found if found.is_file() else None
            return None

        for shot in shots:
            shot["source"] = path(shot.pop("source_asset"))
            if shot["source"] is None:
                shot["kind"] = "card"
            for line in shot["lines"]:
                line["audio"] = path(line.pop("audio_asset"))
        music = [compositor.MusicCueSpec(path=found, gain=cue.gain, start=cue.start, duck=cue.duck)
                 for cue in project.music if (found := path(cue.asset_id)) is not None]
        width, height = output_size(project.format, quality)
        fps, crf, preset = (24, 24, "veryfast") if quality == "preview" else (30, 18, "medium")
        return compositor.RenderPlan(width=width, height=height, fps=fps, shots=compositor.layout(shots),
                                     music=music, subtitles=project.subtitle.enabled,
                                     subtitle_size=project.subtitle.size, subtitle_position=project.subtitle.position,
                                     subtitle_speaker=project.subtitle.speaker, smooth_motion=project.smooth_motion,
                                     crf=crf, preset=preset)

    # ------------------------------------------------------------------ one-click continuation
    async def next(self, inputs: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        actor, _job_id = self._job(context)
        pid = str(inputs.get("project_id") or "")
        pipeline = inputs.get("pipeline")
        if not isinstance(pipeline, dict) or not isinstance(pipeline.get("stages"), list):
            raise AgentOSError(ErrorCode.INVALID, "一键制作的计划无效")
        view, notes, stage = await self.service.advance(actor, pid, pipeline)
        return {"started": view.run_id if view else "", "stage": stage, "skipped": notes,
                "finished": view is None}
