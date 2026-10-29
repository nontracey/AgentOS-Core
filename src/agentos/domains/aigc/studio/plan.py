"""What to generate next and with which arguments: pure planning over a project and its picks.

A *pick* is the take currently used in a slot (``frame:shot_1`` → an asset). Planning turns a
request ("frames for these shots", "the voices stage") into node tool calls whose arguments
reference picked assets, so every generated candidate records exactly what it was made from.
Nothing here stores, calls a model or talks to a node.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from agentos.domains.aigc.studio.engines import ENGINES, KIND_NAMES, Engine, engine
from agentos.domains.aigc.studio.models import Character, Line, Project, slot_key
from agentos.domains.aigc.studio.prompts import (
    MOTION_NEGATIVE,
    character_ref_prompt,
    compose_frame_prompt,
    compose_motion_prompt,
    emotion_vector,
    location_ref_prompt,
    size_for,
    video_frames,
)

# Generation kinds → the slot their result fills and the engine kind that makes it.
GENERATED = {"frame": ("frame", "image"), "end_frame": ("end_frame", "image"), "char_ref": ("char_ref", "image"),
             "loc_ref": ("loc_ref", "image"), "clip": ("clip", "video"), "voice": ("voice", "voice"),
             "voice_sample": ("voice_sample", "voice"), "lipsync": ("lipsync", "lipsync")}
SHOT_SLOTS = {"frame", "end_frame", "clip", "lipsync"}
UPLOAD_SLOTS = {"char_ref": "image", "loc_ref": "image", "style_ref": "image", "frame": "image",
                "end_frame": "image", "voice_ref": "audio", "voice": "audio", "music": "audio", "clip": "video"}
# One-click production order; ``frames`` covers start frames, then end frames of guided shots.
STAGES = ("script", "breakdown", "refs", "voices", "frames", "clips", "render")
STAGE_LABELS = {"script": "写剧本", "breakdown": "拆分镜", "refs": "角色与场景定妆", "voices": "配音",
                "frames": "分镜画面", "end_frames": "首尾帧的尾帧", "clips": "动态镜头", "render": "导出"}
NARRATOR = "旁白"


class PlanError(ValueError):
    """The request cannot be prepared yet; the message says what to do first."""


@dataclass(frozen=True)
class Pick:
    take_id: str
    asset_id: str
    duration: float = 0.0


Picks = Mapping[str, Pick]


@dataclass
class NodeSpec:
    """One node tool call and what its result will be."""
    kind: str
    slot: str
    target: str
    engine: str
    tool: str
    arguments: dict[str, Any]
    label: str
    estimate: float
    meta: dict[str, Any] = field(default_factory=dict)


def asset(picks: Picks, slot: str, target: str | None) -> str | None:
    pick = picks.get(slot_key(slot, target or ""))
    return pick.asset_id if pick else None


def target_exists(project: Project, slot: str, target: str) -> bool:
    if slot in SHOT_SLOTS:
        return project.shot(target) is not None
    if slot == "voice":
        return project.line(target) is not None
    if slot in ("char_ref", "voice_ref", "voice_sample"):
        return project.character(target) is not None
    if slot == "loc_ref":
        return project.location(target) is not None
    return target == project.id


def speaker(project: Project, line: Line) -> Character | None:
    if line.character_id:
        return project.character(line.character_id)
    return next((c for c in project.characters if c.role == NARRATOR or c.name == NARRATOR), None)


def choose_engine(engine_id: str, kind: str) -> Engine:
    found = engine(engine_id)
    if found is None or found.kind != kind:
        raise PlanError(f"没有这个{KIND_NAMES.get(kind, kind)}引擎：{engine_id}")
    return found


def label(project: Project, kind: str, target: str) -> str:
    if kind in SHOT_SLOTS:
        index = next((i for i, s in enumerate(project.shots, start=1) if s.id == target), 0)
        return f"第 {index} 镜" + {"frame": "画面", "end_frame": "尾帧", "clip": "动态", "lipsync": "口型"}[kind]
    if kind == "voice":
        found = project.line(target)
        text = found[1].text if found else ""
        return f"配音：{text[:14]}{'…' if len(text) > 14 else ''}"
    if kind in ("char_ref", "voice_sample"):
        character = project.character(target)
        return f"{character.name if character else '角色'}{'定妆' if kind == 'char_ref' else '试音'}"
    if kind == "loc_ref":
        location = project.location(target)
        return f"场景：{location.name if location else ''}"
    return kind


def estimate_job(project: Project, kind: str, target: str, chosen: Engine) -> float:
    if kind in ("clip", "lipsync"):
        shot = project.shot(target)
        return chosen.estimate(min(5.0, shot.duration if shot else 3.0))
    return chosen.estimate()


# ------------------------------------------------------------------ node arguments
def prepare(project: Project, kind: str, target: str, chosen: Engine, picks: Picks, *, seed: int,
            overrides: Mapping[str, Any] | None = None) -> NodeSpec:
    """Arguments for one candidate; raises ``PlanError`` when something must be made first."""
    overrides = dict(overrides or {})
    if kind not in GENERATED:
        raise PlanError("不支持的生成类型")
    slot, engine_kind = GENERATED[kind]
    if chosen.kind != engine_kind:
        raise PlanError(f"{chosen.label} 不能用来生成{label(project, kind, target)}")
    if not target_exists(project, "char_ref" if slot == "voice_sample" else slot, target):
        raise PlanError("生成对象不存在")
    if kind in ("frame", "end_frame"):
        spec = _frame(project, kind, target, chosen, picks, seed, overrides)
    elif kind in ("char_ref", "loc_ref"):
        spec = _reference(project, kind, target, chosen, picks, seed, overrides)
    elif kind == "clip":
        spec = _clip(project, target, chosen, picks, seed, overrides)
    elif kind in ("voice", "voice_sample"):
        spec = _voice(project, kind, target, chosen, picks)
    else:
        spec = _lipsync(project, target, chosen, picks)
    spec.meta.setdefault("engine", spec.engine)
    if overrides:
        spec.meta["overrides"] = {k: v for k, v in overrides.items() if k in ("seed", "framing", "prompt",
                                                                              "motion_prompt")}
    return spec


def _node(project: Project, kind: str, target: str, chosen: Engine, arguments: dict[str, Any],
          meta: dict[str, Any]) -> NodeSpec:
    return NodeSpec(kind=kind, slot=GENERATED[kind][0], target=target, engine=chosen.id, tool=chosen.tool,
                    arguments=arguments, label=label(project, kind, target),
                    estimate=estimate_job(project, kind, target, chosen), meta=meta)


def _image_args(chosen: Engine, prompt: str, negative: str, refs: list[str], width: int, height: int,
                seed: int) -> dict[str, Any]:
    if len(refs) < chosen.min_refs:
        raise PlanError(f"{chosen.label} 需要至少 {chosen.min_refs} 张参考图："
                        "先给角色或场景定妆，或换用不需要参考图的引擎")
    args: dict[str, Any] = {"recipe": chosen.recipe, "prompt": prompt, "width": width, "height": height, "seed": seed}
    if negative:
        args["negative"] = negative
    if chosen.steps:
        args["steps"] = chosen.steps
    if refs and chosen.max_refs:
        args["reference_asset_ids"] = refs[:chosen.max_refs]
    return args


def _frame(project: Project, kind: str, target: str, chosen: Engine, picks: Picks, seed: int,
           overrides: dict[str, Any]) -> NodeSpec:
    shot = project.shot(target)
    assert shot is not None
    refs_by_character = {c.id: ref for c in project.characters if (ref := asset(picks, "char_ref", c.id))}
    location_ref = asset(picks, "loc_ref", shot.location_id)
    style_ref = asset(picks, "style_ref", project.id)
    if overrides.get("prompt"):
        shot = shot.model_copy(update={"image_prompt": str(overrides["prompt"])[:1500]})
    max_refs = chosen.max_refs
    start_ref: str | None = None
    if kind == "end_frame":
        start_ref = asset(picks, "frame", shot.id)
        if start_ref is None:
            raise PlanError("先生成并选定这一镜的起始画面，再生成尾帧")
        max_refs -= 1
    prompt = compose_frame_prompt(project, shot, refs_by_character=refs_by_character, location_ref=location_ref,
                                  style_ref=style_ref, max_refs=max(0, max_refs), end=kind == "end_frame",
                                  index_offset=1 if start_ref else 0)
    refs, text = prompt.refs, prompt.prompt
    if start_ref:
        refs = [start_ref, *refs]
        text = ("Image 1 is the first frame of this shot: keep the same characters, outfits, setting and "
                "lighting, change only the pose to the end of the action. " + text)[:3900]
    width, height = size_for(project.format, "image")
    args = _image_args(chosen, text, prompt.negative, refs, width, height, seed)
    loras = [c for c in (project.character(cid) for cid in shot.character_ids) if c and c.lora_id]
    if chosen.lora and len(loras) == 1 and loras[0].lora_id:
        args["image_lora_id"] = loras[0].lora_id
        args["image_lora_strength"] = loras[0].lora_strength
    return _node(project, kind, target, chosen, args, {
        "prompt": text, "negative": prompt.negative, "seed": seed, "refs": refs, "legend": prompt.legend,
        "width": width, "height": height})


def _reference(project: Project, kind: str, target: str, chosen: Engine, picks: Picks, seed: int,
               overrides: dict[str, Any]) -> NodeSpec:
    style_ref = asset(picks, "style_ref", project.id)
    current = asset(picks, kind, target)
    if kind == "char_ref":
        character = project.character(target)
        assert character is not None
        refs = [r for r in (current, style_ref) if r]
        prompt = character_ref_prompt(project, character.name, character.visual_prompt, character.description,
                                      str(overrides.get("framing") or "half"))
        if current:
            prompt = ("Image 1 is this character's current design: keep the identity, face, hair and outfit "
                      "exactly. " + prompt)
        # Portrait references carry identity best in every film format.
        width, height = (576, 1024) if overrides.get("framing") == "full" else (768, 1024)
    else:
        location = project.location(target)
        assert location is not None
        refs = [style_ref] if style_ref else []
        prompt = location_ref_prompt(project, location.name, location.visual_prompt, location.description)
        width, height = size_for(project.format, "image")
    if overrides.get("prompt"):
        prompt = str(overrides["prompt"])[:3900]
    if len(refs) < chosen.min_refs:
        fallback = next((e for e in ENGINES if e.kind == "image" and e.min_refs == 0), None)
        if fallback is None:
            raise PlanError("没有可用的文生图引擎")
        chosen = fallback
    args = _image_args(chosen, prompt[:3900], project.style()["negative"], refs, width, height, seed)
    return _node(project, kind, target, chosen, args, {"prompt": prompt, "seed": seed, "refs": refs,
                                                       "width": width, "height": height})


def _clip(project: Project, target: str, chosen: Engine, picks: Picks, seed: int,
          overrides: dict[str, Any]) -> NodeSpec:
    shot = project.shot(target)
    assert shot is not None
    frame = asset(picks, "frame", shot.id)
    if frame is None:
        raise PlanError("先生成并选定这一镜的画面，再生成动态")
    if overrides.get("motion_prompt"):
        shot = shot.model_copy(update={"motion_prompt": str(overrides["motion_prompt"])[:800]})
    width, height = size_for(project.format, "video")
    frames = video_frames(min(5.0, shot.duration))
    args: dict[str, Any] = {"recipe": chosen.recipe, "input_asset_id": frame,
                            "prompt": compose_motion_prompt(project, shot), "negative": MOTION_NEGATIVE,
                            "width": width, "height": height, "frames": frames, "fps": 16, "seed": seed}
    if chosen.end_frame:
        end = asset(picks, "end_frame", shot.id)
        if end is None:
            raise PlanError("首尾帧引擎需要选定的尾帧：先生成尾帧")
        args["end_asset_id"] = end
    return _node(project, "clip", target, chosen, args, {"prompt": args["prompt"], "seed": seed, "frames": frames,
                                                         "fps": 16, "width": width, "height": height})


def _voice(project: Project, kind: str, target: str, chosen: Engine, picks: Picks) -> NodeSpec:
    emotion: list[float] | None = None
    if kind == "voice_sample":
        character = project.character(target)
        assert character is not None
        text = character.voice.sample_text or f"你好，我是{character.name}。今天的事，我一定会弄清楚。"
    else:
        found = project.line(target)
        assert found is not None
        line = found[1]
        who = speaker(project, line)
        if who is None:
            raise PlanError("这句台词没有指定角色，也没有旁白角色")
        character, text = who, line.text
        emotion = emotion_vector(line.emotion, line.intensity)
    reference = asset(picks, "voice_ref", character.id)
    if reference is None:
        raise PlanError(f"{character.name} 没有参考音：在角色页上传一段 5–15 秒的干净人声")
    args: dict[str, Any] = {"text": text[:2000], "input_asset_id": reference}
    if emotion is not None:
        args["emotion"] = emotion
    return _node(project, kind, target, chosen, args, {"text": text, "emotion": emotion,
                                                       "character_id": character.id})


def _lipsync(project: Project, target: str, chosen: Engine, picks: Picks) -> NodeSpec:
    shot = project.shot(target)
    assert shot is not None
    clip = asset(picks, "clip", shot.id)
    if clip is None:
        raise PlanError("先生成并选定这一镜的动态视频")
    if not shot.face_box or len(shot.face_box) != 4:
        raise PlanError("先在画面上框出说话人的脸")
    audio_ids: list[str] = []
    timing: list[dict[str, float]] = []
    cursor = 0.25
    for line in shot.lines:
        voice = picks.get(slot_key("voice", line.id))
        duration = voice.duration if voice else 0.0
        if voice and duration:
            audio_ids.append(voice.asset_id)
            timing.append({"start": round(cursor, 3), "end": round(cursor + duration, 3)})
        cursor += (duration or len(line.text) / 4.5) + line.pause_after
    if not audio_ids:
        raise PlanError("这一镜还没有配音")
    duration = round(min(60.0, max(shot.duration, cursor + 0.35)), 3)
    args = {"input_asset_id": clip, "start": 0, "duration": duration, "retime_native": True, "fps": 25,
            "face_box": [float(x) for x in shot.face_box], "dialogue_asset_ids": audio_ids,
            "dialogue_timing": timing}
    return _node(project, "lipsync", target, chosen, args, {"duration": duration})


# ------------------------------------------------------------------ one-click stages
def stage_work(project: Project, stage: str, picks: Picks) -> tuple[list[tuple[str, str]], list[str]]:
    """(kind, target) pairs a media stage still needs, plus notes about what it has to skip."""
    planned: list[tuple[str, str]] = []
    skipped: list[str] = []
    if stage == "refs":
        planned += [("char_ref", c.id) for c in project.characters
                    if c.role != NARRATOR and slot_key("char_ref", c.id) not in picks]
        planned += [("loc_ref", loc.id) for loc in project.locations if slot_key("loc_ref", loc.id) not in picks]
    elif stage == "voices":
        for shot in project.shots:
            for line in shot.lines:
                if slot_key("voice", line.id) in picks:
                    continue
                who = speaker(project, line)
                if who is None or slot_key("voice_ref", who.id) not in picks:
                    name = who.name if who else NARRATOR
                    note = f"{name} 没有参考音，跳过配音（在角色页上传一段 5–15 秒的参考音）"
                    if note not in skipped:
                        skipped.append(note)
                    continue
                planned.append(("voice", line.id))
    elif stage == "frames":
        planned += [("frame", s.id) for s in project.shots if slot_key("frame", s.id) not in picks]
    elif stage == "end_frames":
        planned += [("end_frame", s.id) for s in project.shots if s.motion == "flf2v"
                    and slot_key("end_frame", s.id) not in picks and slot_key("frame", s.id) in picks]
    elif stage == "clips":
        for shot in project.shots:
            if shot.motion == "still" or slot_key("clip", shot.id) in picks:
                continue
            if slot_key("frame", shot.id) not in picks:
                skipped.append(f"{label(project, 'clip', shot.id)}：没有画面，跳过")
                continue
            planned.append(("clip", shot.id))
    return planned, skipped


def stage_engine(project: Project, kind: str, target: str) -> Engine:
    """The project's engine for a one-click item; guided shots switch to a guided recipe."""
    engine_kind = GENERATED[kind][1]
    chosen = engine(getattr(project.engines, engine_kind)) or next(e for e in ENGINES if e.kind == engine_kind)
    shot = project.shot(target) if kind == "clip" else None
    if shot is not None and shot.motion == "flf2v" and not chosen.end_frame:
        guided = next((e for e in ENGINES if e.kind == "video" and e.end_frame), None)
        chosen = guided or chosen
    elif shot is not None and shot.motion == "i2v" and chosen.end_frame:
        chosen = next((e for e in ENGINES if e.kind == "video" and not e.end_frame), chosen)
    return chosen


# ------------------------------------------------------------------ overview numbers
def progress(project: Project, picks: Picks) -> dict[str, Any]:
    lines = [line for shot in project.shots for line in shot.lines]
    moving = [s for s in project.shots if s.motion != "still"]
    cast = [c for c in project.characters if c.role != NARRATOR]
    return {"shots": len(project.shots),
            "frames": sum(1 for s in project.shots if slot_key("frame", s.id) in picks),
            "lines": len(lines), "voices": sum(1 for line in lines if slot_key("voice", line.id) in picks),
            "moving": len(moving), "clips": sum(1 for s in moving if slot_key("clip", s.id) in picks),
            "characters": len(cast), "refs": sum(1 for c in cast if slot_key("char_ref", c.id) in picks),
            "voice_refs": sum(1 for c in project.characters if slot_key("voice_ref", c.id) in picks),
            "locations": len(project.locations),
            "loc_refs": sum(1 for loc in project.locations if slot_key("loc_ref", loc.id) in picks),
            "seconds": round(sum(s.duration for s in project.shots), 1),
            "rendered": slot_key("render", project.id) in picks}


def estimate(project: Project, picks: Picks) -> dict[str, Any]:
    """Rough seconds of node work still ahead with the project's engines."""
    def pick_engine(engine_id: str, kind: str) -> Engine:
        return engine(engine_id) or next(e for e in ENGINES if e.kind == kind)

    image = pick_engine(project.engines.image, "image")
    video = pick_engine(project.engines.video, "video")
    voice = pick_engine(project.engines.voice, "voice")
    shots = project.shots
    planned = len(shots) or max(6, round(project.target_seconds / 3))
    frames = sum(1 for s in shots if slot_key("frame", s.id) not in picks) if shots else planned
    refs = sum(1 for c in project.characters if c.role != NARRATOR and slot_key("char_ref", c.id) not in picks) + \
        sum(1 for loc in project.locations if slot_key("loc_ref", loc.id) not in picks)
    lines = [line for s in shots for line in s.lines if slot_key("voice", line.id) not in picks]
    moving = [s for s in shots if s.motion != "still" and slot_key("clip", s.id) not in picks]
    parts = {"refs": refs * image.estimate(), "frames": frames * image.estimate(),
             "voices": len(lines) * voice.estimate(),
             "clips": sum(video.estimate(min(5.0, s.duration)) for s in moving)}
    return {"seconds": round(sum(parts.values())), "parts": {k: round(v) for k, v in parts.items()}}


# ------------------------------------------------------------------ film timeline
def timeline(project: Project, picks: Picks) -> list[dict[str, Any]]:
    """Shots in film order with the picked media to use; assets stay ids for the caller to open."""
    names = {c.id: c.name for c in project.characters}
    shots: list[dict[str, Any]] = []
    for shot in project.shots:
        source: str | None = None
        kind = "card"
        if shot.motion != "still":
            for slot in ("lipsync", "clip"):
                if (moving := asset(picks, slot, shot.id)) is not None:
                    source, kind = moving, "video"
                    break
        if source is None and (still := asset(picks, "frame", shot.id)) is not None:
            source, kind = still, "image"
        lines = []
        for line in shot.lines:
            voice = picks.get(slot_key("voice", line.id))
            lines.append({"text": line.text, "speaker": names.get(line.character_id or "", NARRATOR),
                          "audio_asset": voice.asset_id if voice else None,
                          "duration": (voice.duration if voice else 0.0) or max(0.8, len(line.text) / 4.5),
                          "pause_after": line.pause_after})
        shots.append({"id": shot.id, "duration": shot.duration, "kind": kind, "source_asset": source,
                      "camera": shot.camera, "transition": shot.transition, "seed": shot.seed or 0,
                      "card_text": shot.action or shot.scene, "lines": lines})
    return shots
