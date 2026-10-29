"""One-click story production: the project people edit and the document saved for it.

Pure business rules: no storage, model or node access. The service keeps one ``StudioDoc``
per project as a private, revisioned draft; generated media stay immutable Assets whose
provenance is the workflow run that produced them.
"""
from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

AspectFormat = Literal["9:16", "16:9", "1:1"]
ShotSize = Literal["远景", "全景", "中景", "近景", "特写"]
Camera = Literal["static", "push_in", "pull_out", "pan_left", "pan_right", "tilt_up", "tilt_down", "shake"]
Motion = Literal["still", "i2v", "flf2v"]
Transition = Literal["cut", "fade", "dissolve", "flash"]
# Every generated or uploaded medium is a take in a slot; a selection picks the take in use.
# shot: frame, end_frame, clip, lipsync · line: voice · character: char_ref, voice_ref
# location: loc_ref · project: style_ref, render, music
SLOTS = ("frame", "end_frame", "clip", "lipsync", "voice", "char_ref", "voice_ref", "loc_ref", "style_ref",
         "render", "music")

# Eight-emotion order of the speech tool; 平静 sends no vector so the reference voice leads.
EMOTIONS = ["高兴", "愤怒", "悲伤", "恐惧", "反感", "低落", "惊讶", "平静"]

STYLES: list[dict[str, str]] = [
    {"id": "jp-fantasy-cg", "name": "日系幻想 CG",
     "description": "精细发丝、多层高光的眼睛、柔光皮肤、蕾丝与金属细节、暖金色轮廓光",
     "prompt": ("highly detailed Japanese fantasy CG illustration, game key visual quality, delicate individual "
                "hair strands, layered glossy eyes with multiple highlights, soft luminous skin, intricate lace "
                "and metal ornaments, warm golden rim light, cinematic depth of field"),
     "negative": ("western cartoon, 3d render, plastic skin, chibi, flat shading, low detail, blurry, deformed "
                  "hands, extra fingers, bad anatomy, text, watermark, logo")},
    {"id": "anime-cel", "name": "赛璐璐动画",
     "description": "TV 动画质感：干净线稿、平涂与两阶阴影、明快配色",
     "prompt": ("clean anime cel shading, crisp lineart, flat colors with two-tone shadows, TV anime key frame, "
                "vibrant balanced palette, expressive faces"),
     "negative": "3d render, photorealistic, western cartoon, sketch, blurry, deformed hands, text, watermark"},
    {"id": "guoman-3d", "name": "国漫 3D",
     "description": "仙侠国漫 CG：高精度建模质感、体积光、飘逸汉服",
     "prompt": ("Chinese 3D animation style, xianxia, high detail CG render, volumetric light, flowing hanfu, "
                "elegant faces, epic atmosphere"),
     "negative": "western cartoon, low poly, plastic, chibi, blurry, deformed hands, text, watermark"},
    {"id": "cinematic-real", "name": "写实电影",
     "description": "电影剧照：35mm 质感、浅景深、自然肤质、戏剧化光线",
     "prompt": ("photorealistic cinematic film still, 35mm, shallow depth of field, natural skin texture, "
                "dramatic motivated lighting, color graded"),
     "negative": "cartoon, anime, illustration, plastic skin, oversaturated, deformed hands, text, watermark"},
    {"id": "watercolor", "name": "水彩绘本",
     "description": "柔和水彩、纸张纹理、低饱和温暖色调",
     "prompt": "soft watercolor picture book illustration, visible paper texture, gentle pastel palette, warm light",
     "negative": "3d render, photorealistic, harsh contrast, text, watermark"},
]

DEFAULT_ENGINES = {"image": "klein", "video": "wan22-fast", "voice": "indextts2", "lipsync": "joyvasa"}
# Identifiers end up in workflow metadata, take ids and URLs: keep them plain.
ID_PATTERN = r"^[A-Za-z0-9_-]{1,48}$"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class Doc(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Voice(Doc):
    description: str = ""     # 声线说明：年龄、音色、说话习惯（参考音频放在 voice_ref 槽）
    sample_text: str = ""
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


class Character(Doc):
    id: str = Field(default_factory=lambda: new_id("char"), pattern=ID_PATTERN)
    name: str
    role: Literal["主角", "配角", "群演", "旁白"] = "配角"
    description: str = ""      # 中文：外貌、服装、性格
    visual_prompt: str = ""    # English appearance used inside image prompts
    lora_id: str | None = None
    lora_strength: float = Field(default=1.0, ge=0, le=1.5)
    voice: Voice = Field(default_factory=Voice)
    color: str = ""


class Location(Doc):
    id: str = Field(default_factory=lambda: new_id("loc"), pattern=ID_PATTERN)
    name: str
    description: str = ""
    visual_prompt: str = ""


class Line(Doc):
    id: str = Field(default_factory=lambda: new_id("line"), pattern=ID_PATTERN)
    character_id: str | None = None    # None = 旁白
    text: str
    emotion: str = "平静"
    intensity: float = Field(default=0.6, ge=0, le=1)
    pause_after: float = Field(default=0.25, ge=0, le=3)


class Shot(Doc):
    id: str = Field(default_factory=lambda: new_id("shot"), pattern=ID_PATTERN)
    scene: str = ""
    location_id: str | None = None
    character_ids: list[str] = Field(default_factory=list)
    size: ShotSize = "中景"
    camera: Camera = "push_in"
    action: str = ""            # 中文：这一镜画面里正在发生什么
    image_prompt: str = ""      # English prompt for the keyframe
    motion_prompt: str = ""     # English prompt for image-to-video
    lines: list[Line] = Field(default_factory=list)
    duration: float = Field(default=3.0, ge=0.8, le=20)
    motion: Motion = "still"
    transition: Transition = "cut"
    lipsync: bool = False
    face_box: list[float] | None = None
    seed: int | None = None
    notes: str = ""


class MusicCue(Doc):
    id: str = Field(default_factory=lambda: new_id("music"), pattern=ID_PATTERN)
    asset_id: str
    name: str = ""
    gain: float = Field(default=0.35, ge=0, le=2)
    start: float = Field(default=0, ge=0)
    duck: bool = True


class SubtitleStyle(Doc):
    enabled: bool = True
    size: float = Field(default=0.042, ge=0.02, le=0.08)       # relative to frame height
    position: float = Field(default=0.80, ge=0.5, le=0.95)     # baseline of the text block
    speaker: bool = False


class EnginePrefs(Doc):
    image: str = DEFAULT_ENGINES["image"]
    video: str = DEFAULT_ENGINES["video"]
    voice: str = DEFAULT_ENGINES["voice"]
    lipsync: str = DEFAULT_ENGINES["lipsync"]


class Project(Doc):
    id: str = Field(default_factory=lambda: new_id("prj"), pattern=ID_PATTERN)
    title: str
    logline: str = ""
    format: AspectFormat = "9:16"
    genre: str = ""
    premise: str = ""
    target_seconds: int = Field(default=60, ge=10, le=1800)
    style_id: str = "anime-cel"
    style_prompt: str = ""
    style_negative: str = ""
    script: str = ""
    characters: list[Character] = Field(default_factory=list)
    locations: list[Location] = Field(default_factory=list)
    shots: list[Shot] = Field(default_factory=list)
    music: list[MusicCue] = Field(default_factory=list)
    subtitle: SubtitleStyle = Field(default_factory=SubtitleStyle)
    engines: EnginePrefs = Field(default_factory=EnginePrefs)
    smooth_motion: bool = False       # 实验：minterpolate 补帧，动作快时可能扭曲

    def style(self) -> dict[str, str]:
        preset = next((s for s in STYLES if s["id"] == self.style_id), STYLES[0])
        return {"prompt": self.style_prompt or preset["prompt"], "negative": self.style_negative or preset["negative"],
                "name": preset["name"] if not self.style_prompt else "自定义"}

    def character(self, cid: str | None) -> Character | None:
        return next((c for c in self.characters if c.id == cid), None)

    def location(self, lid: str | None) -> Location | None:
        return next((loc for loc in self.locations if loc.id == lid), None)

    def shot(self, sid: str) -> Shot | None:
        return next((s for s in self.shots if s.id == sid), None)

    def line(self, line_id: str) -> tuple[Shot, Line] | None:
        for shot in self.shots:
            for line in shot.lines:
                if line.id == line_id:
                    return shot, line
        return None


class Upload(Doc):
    """Media a person attached to a slot (reference image, reference voice, music, replacement shot)."""
    id: str = Field(default_factory=lambda: new_id("up"), pattern=ID_PATTERN)
    asset_id: str
    slot: str
    target_id: str
    name: str = ""
    created_at: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)


class StudioDoc(Doc):
    """The whole draft: the project plus choices made about its candidate media.

    ``selections`` maps ``slot:target`` to a take id; without an explicit choice the first
    candidate is used (the newest for renders), and an empty id means "use nothing". Hidden
    takes stay in history, unused. ``revision`` counts edits of the project itself, so choosing
    a take does not invalidate an edit that is still being typed.
    """
    project: Project
    revision: int = Field(default=1, ge=1)
    selections: dict[str, str] = Field(default_factory=dict)
    ratings: dict[str, int] = Field(default_factory=dict)
    hidden: list[str] = Field(default_factory=list)
    uploads: list[Upload] = Field(default_factory=list)


def slot_key(slot: str, target_id: str) -> str:
    return f"{slot}:{target_id}"
