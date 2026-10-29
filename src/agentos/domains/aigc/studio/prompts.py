"""Deterministic prompt composition for keyframes, references, motion and speech.

The same project state always yields the same prompt: a legend naming what each reference
image contributes, then the moment of the shot, framing, cast appearance, setting and style.
"""
from __future__ import annotations

from dataclasses import dataclass

from agentos.domains.aigc.studio.models import EMOTIONS, Project, Shot

SIZE_EN = {"远景": "extreme wide establishing shot, characters small in frame",
           "全景": "full shot, whole body visible head to toe",
           "中景": "medium shot, framed from the waist up",
           "近景": "close-up shot, framed from the chest up",
           "特写": "extreme close-up on the face and eyes"}

CAMERA_EN = {"static": "locked-off camera", "push_in": "camera slowly pushes in",
             "pull_out": "camera slowly pulls back", "pan_left": "camera pans left", "pan_right": "camera pans right",
             "tilt_up": "camera tilts up", "tilt_down": "camera tilts down", "shake": "handheld camera shake"}

MOTION_NEGATIVE = "static image, frozen, morphing face, extra limbs, flicker, text, watermark"


def emotion_vector(label: str, intensity: float) -> list[float] | None:
    """Eight-emotion vector for the speech tool; calm lines follow the reference voice instead."""
    if label not in EMOTIONS or label == "平静":
        return None
    vector = [0.0] * 8
    vector[EMOTIONS.index(label)] = round(max(0.05, min(0.8, intensity * 0.8)), 2)
    return vector


def video_frames(duration: float, fps: int = 16) -> int:
    """Video recipes need 4n+1 frames and allow 17–81 (about 1–5 s at 16 fps)."""
    frames = int(round(duration * fps / 4)) * 4 + 1
    return max(17, min(81, frames))


def size_for(fmt: str, kind: str) -> tuple[int, int]:
    """Generation size per aspect; node recipes cap each side at 1024 (multiples of 16)."""
    if kind == "video":
        return {"9:16": (480, 848), "16:9": (848, 480), "1:1": (624, 624)}.get(fmt, (480, 848))
    return {"9:16": (576, 1024), "16:9": (1024, 576), "1:1": (1024, 1024)}.get(fmt, (576, 1024))


def output_size(fmt: str, quality: str) -> tuple[int, int]:
    full = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080)}.get(fmt, (1080, 1920))
    return (full[0] // 2, full[1] // 2) if quality == "preview" else full


@dataclass
class FramePrompt:
    prompt: str
    negative: str
    refs: list[str]
    legend: list[str]


def compose_frame_prompt(project: Project, shot: Shot, *, refs_by_character: dict[str, str],
                         location_ref: str | None, style_ref: str | None, max_refs: int,
                         end: bool = False, index_offset: int = 0) -> FramePrompt:
    """Reference legend → shot content → framing → cast appearance → setting → style."""
    style = project.style()
    refs: list[str] = []
    legend: list[str] = []
    cast = [c for c in (project.character(cid) for cid in shot.character_ids) if c is not None]
    for member in cast:
        ref = refs_by_character.get(member.id)
        if ref and len(refs) < max_refs:
            refs.append(ref)
            legend.append(f"image {len(refs) + index_offset} shows {member.name}: keep the same face, hairstyle, "
                          f"eye color and outfit")
    location = project.location(shot.location_id)
    if location and location_ref and len(refs) < max_refs:
        refs.append(location_ref)
        legend.append(f"image {len(refs) + index_offset} shows the location {location.name}: keep its "
                      f"architecture, layout and lighting")
    if style_ref and len(refs) < max_refs:
        refs.append(style_ref)
        legend.append(f"image {len(refs) + index_offset} is a style reference only: match its rendering, "
                      f"palette and light, not its people or composition")
    body = shot.image_prompt.strip() or shot.action.strip() or shot.scene.strip()
    if end:
        body = (shot.motion_prompt.strip() + ". Final pose of this action. " if shot.motion_prompt else "") + body
    parts: list[str] = []
    if legend:
        parts.append("; ".join(legend) + ".")
    parts.append(body)
    parts.append(SIZE_EN.get(shot.size, "medium shot") + ".")
    looks = [f"{c.name}: {c.visual_prompt.strip()}" for c in cast if c.visual_prompt.strip()]
    if looks:
        parts.append("Characters — " + "; ".join(looks) + ".")
    if location and location.visual_prompt.strip():
        parts.append("Setting — " + location.visual_prompt.strip() + ".")
    parts.append(style["prompt"] + ".")
    if project.format == "9:16":
        parts.append("Vertical 9:16 composition with headroom for subtitles at the bottom.")
    return FramePrompt(prompt=" ".join(p for p in parts if p)[:3900], negative=style["negative"][:3900],
                       refs=refs, legend=legend)


def compose_motion_prompt(project: Project, shot: Shot) -> str:
    action = shot.motion_prompt.strip() or shot.image_prompt.strip() or shot.action.strip()
    camera = CAMERA_EN.get(shot.camera, "")
    style = project.style()["prompt"].split(",")[0]
    return f"{action}. {camera}. Smooth natural motion, consistent character identity, {style}."[:3900]


def character_ref_prompt(project: Project, name: str, visual_prompt: str, description: str,
                         framing: str = "half") -> str:
    pose = ("full body standing pose, front view, arms relaxed" if framing == "full"
            else "upper body portrait, facing the viewer, three-quarter view")
    look = visual_prompt.strip() or description.strip()
    return (f"Character design reference of {name}: {look}. {pose}, neutral calm expression, plain light gray "
            f"studio background, even soft lighting, sharp details. {project.style()['prompt']}.")[:3900]


def location_ref_prompt(project: Project, name: str, visual_prompt: str, description: str) -> str:
    look = visual_prompt.strip() or description.strip()
    return (f"Background art of {name}: {look}. Wide establishing view, no people, clear spatial layout, "
            f"atmospheric light. {project.style()['prompt']}.")[:3900]
