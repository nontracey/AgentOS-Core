"""Private project snapshots; immutable execution versions remain in WorkflowService."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentos.domains.aigc.production import Blocking, LipSync, ProductionPlan
from agentos.domains.aigc.sampling import validate_edit_region


class Shot(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,40}$')
    label: str = Field(default='', max_length=80)
    role: str = Field(default='', max_length=80)
    scene: str = Field(default='', max_length=200)
    input_asset_id: str = Field(default='', max_length=128)
    keyframe_reference_asset_ids: list[str] = Field(default_factory=list, max_length=3)
    keyframe_reference_notes: str = Field(default='', max_length=600)
    end_keyframe_reference_asset_ids: list[str] = Field(default_factory=list, max_length=3)
    end_keyframe_reference_notes: str = Field(default='', max_length=600)
    keyframe_edit_region: list[float] | None = Field(default=None, min_length=4, max_length=4)
    end_keyframe_edit_region: list[float] | None = Field(default=None, min_length=4, max_length=4)
    image_lora_id: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_-]{1,40}$')
    image_lora_strength: float = Field(default=1, ge=0, le=1.5, allow_inf_nan=False, strict=True)
    video_recipe: Literal['story-shot', 'story-shot-quality', 'story-shot-guided',
                          'story-shot-guided-quality'] | None = None
    end_keyframe_asset_id: str | None = Field(default=None, min_length=1, max_length=128)
    end_keyframe_review: Literal['pending', 'accepted', 'rejected'] = 'pending'
    cast: list[Blocking] = Field(default_factory=list, max_length=3)
    scene_id: str | None = Field(default=None, max_length=40)
    purpose: str = Field(default='', max_length=300)
    camera: str = Field(default='', max_length=400)
    action_start: str = Field(default='', max_length=400)
    action_end: str = Field(default='', max_length=400)
    state_before: dict[str, str] = Field(default_factory=dict)
    state_after: dict[str, str] = Field(default_factory=dict)
    duration: float | None = Field(default=None, gt=0, le=60, allow_inf_nan=False)
    trim_in: float = Field(default=0, ge=0, le=60, allow_inf_nan=False)
    retime_native: bool = Field(default=False, strict=True)
    keyframe_review: Literal['pending', 'accepted', 'rejected'] = 'pending'
    lipsync: LipSync | None = None
    prompt: str = Field(min_length=1, max_length=4000)
    negative: str = Field(default='deformed limbs, extra fingers, cropped body', max_length=4000)
    seed: int = Field(default=1, ge=0, le=2**63 - 1)
    frames: int = Field(default=65, ge=5, le=81)
    selected_asset_id: str | None = Field(default=None, max_length=128)
    selected_run_id: str | None = Field(default=None, max_length=128)
    review: Literal['pending', 'accepted', 'rejected'] = 'pending'

    @field_validator('keyframe_edit_region', 'end_keyframe_edit_region', mode='before')
    @classmethod
    def valid_edit_region(cls, value: list[float] | None) -> list[float] | None:
        return validate_edit_region(value) if value is not None else None

    @model_validator(mode='after')
    def valid_frames(self) -> Shot:
        if self.retime_native and (self.trim_in != 0 or self.duration is None):
            raise ValueError('whole native retiming requires zero trim and a planned duration')
        if self.image_lora_id and len(self.cast) != 1:
            raise ValueError('identity image LoRA requires a single-subject cast')
        if not self.image_lora_id and self.image_lora_strength != 1:
            raise ValueError('image LoRA strength requires a registered ID')
        for refs in (self.keyframe_reference_asset_ids, self.end_keyframe_reference_asset_ids):
            if len(set(refs)) != len(refs) or any(
                    not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', ref) or ref in {'.', '..'} for ref in refs):
                raise ValueError('invalid or duplicate keyframe reference IDs')
        if self.end_keyframe_review == 'accepted' and not self.end_keyframe_asset_id:
            raise ValueError('approved end keyframe requires an image')
        if len({c.character_id for c in self.cast}) != len(self.cast):
            raise ValueError('duplicate character in shot')
        if self.lipsync and self.lipsync.character_id not in {c.character_id for c in self.cast}:
            raise ValueError('lip-sync speaker must appear in the shot')
        for state in (self.state_before, self.state_after):
            if len(state) > 20 or any(not k or len(k) > 80 or len(v) > 200 for k, v in state.items()):
                raise ValueError('invalid continuity state')
        if self.keyframe_review == 'accepted' and not self.input_asset_id:
            raise ValueError('approved keyframe requires an image')
        if (self.frames - 1) % 4:
            raise ValueError('frames must be 4n+1')
        if self.review == 'accepted' and (not self.selected_asset_id or not self.selected_run_id):
            raise ValueError('accepted shot requires a selected asset and its production run')
        return self


class Subtitle(BaseModel):
    model_config = ConfigDict(extra='forbid')
    start: float = Field(ge=0, le=3600, allow_inf_nan=False)
    end: float = Field(gt=0, le=3600, allow_inf_nan=False)
    text: str = Field(min_length=1, max_length=1000)

    @model_validator(mode='after')
    def ordered(self) -> Subtitle:
        if self.end <= self.start:
            raise ValueError('subtitle end must follow start')
        return self


class Project(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=120)
    kind: Literal['pet', 'story']
    node_id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,64}$')
    width: int = Field(default=512, ge=256, le=1024, multiple_of=16)
    height: int = Field(default=800, ge=256, le=1024, multiple_of=16)
    fps: int = Field(default=16, ge=1, le=30)
    output_width: int | None = Field(default=None, ge=256, le=1920, multiple_of=2)
    output_height: int | None = Field(default=None, ge=256, le=1920, multiple_of=2)
    output_fps: int | None = Field(default=None, ge=1, le=30)
    shots: list[Shot] = Field(min_length=1, max_length=30)
    audio_asset_id: str | None = Field(default=None, max_length=128)
    voice_asset_id: str | None = Field(default=None, max_length=128)
    narration: str = Field(default='', max_length=2000)
    subtitles: list[Subtitle] = Field(default_factory=list, max_length=500)
    production: ProductionPlan | None = None
    pet_layout: dict[Literal['bodyHeight', 'anchorX', 'floor'], float] | None = None

    @model_validator(mode='after')
    def unique_shots(self) -> Project:
        if (self.output_width is None) != (self.output_height is None):
            raise ValueError('output width and height must be supplied together')
        if len({s.id for s in self.shots}) != len(self.shots):
            raise ValueError('duplicate shot IDs')
        if self.production is not None:
            if self.kind != 'story':
                raise ValueError('production planning applies to stories')
            chars = {c.id for c in self.production.characters}
            scenes = {s.id for s in self.production.scenes}
            for shot in self.shots:
                if any(c.character_id not in chars for c in shot.cast):
                    raise ValueError('unknown shot character')
                if shot.scene_id is not None and shot.scene_id not in scenes:
                    raise ValueError('unknown shot scene')
            total = sum(s.duration or s.frames / self.fps for s in self.shots)
            if any(d.end > total for d in self.production.dialogue):
                raise ValueError('dialogue extends beyond planned timeline')
        elif any(not s.input_asset_id for s in self.shots):
            raise ValueError('legacy shots need input references')
        if self.pet_layout is not None:
            layout = self.pet_layout
            if (set(layout) != {'bodyHeight', 'anchorX', 'floor'}
                    or not 0 < layout['bodyHeight'] <= self.height
                    or not 0 <= layout['anchorX'] <= self.width
                    or not 0 < layout['floor'] <= self.height):
                raise ValueError('invalid fixed character layout')
        return self
