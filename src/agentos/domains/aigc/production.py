"""Reusable story assets and director intent, independent of inference backends."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Review = Literal['pending', 'accepted', 'rejected']


class CharacterCard(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,40}$')
    name: str = Field(min_length=1, max_length=80)
    appearance: str = Field(min_length=1, max_length=1200)
    fixed_traits: list[str] = Field(default_factory=list, max_length=16)
    personality: str = Field(default='', max_length=400)
    wardrobe: str = Field(default='', max_length=400)
    voice_asset_id: str | None = Field(default=None, max_length=128)
    style_content: str = Field(default='', max_length=120)
    reference_asset_ids: list[str] = Field(default_factory=list, max_length=3)
    review: Review = 'pending'

    @model_validator(mode='after')
    def ready(self) -> CharacterCard:
        if any(not x or len(x) > 200 for x in self.fixed_traits):
            raise ValueError('invalid character trait')
        if (any(not x or len(x) > 128 for x in self.reference_asset_ids) or
                len(set(self.reference_asset_ids)) != len(self.reference_asset_ids)):
            raise ValueError('invalid reference IDs')
        if self.review == 'accepted' and not self.reference_asset_ids:
            raise ValueError('approved character needs reference images')
        return self


class SceneCard(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,40}$')
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=1000)
    layout: str = Field(min_length=1, max_length=600)
    lighting: str = Field(default='', max_length=400)
    style_content: str = Field(default='', max_length=120)
    reference_asset_ids: list[str] = Field(default_factory=list, max_length=3)
    review: Review = 'pending'

    @model_validator(mode='after')
    def ready(self) -> SceneCard:
        if (any(not x or len(x) > 128 for x in self.reference_asset_ids) or
                len(set(self.reference_asset_ids)) != len(self.reference_asset_ids)):
            raise ValueError('invalid reference IDs')
        if self.review == 'accepted' and not self.reference_asset_ids:
            raise ValueError('approved scene needs reference images')
        return self


class Blocking(BaseModel):
    model_config = ConfigDict(extra='forbid')
    character_id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,40}$')
    reference_index: int = Field(default=0, ge=0, le=2)
    position: str = Field(min_length=1, max_length=160)
    eyeline: str = Field(default='', max_length=160)
    expression: str = Field(default='', max_length=160)


class Dialogue(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,40}$')
    character_id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,40}$')
    text: str = Field(min_length=1, max_length=2000)
    start: float = Field(ge=0, le=3600, allow_inf_nan=False)
    end: float = Field(gt=0, le=3600, allow_inf_nan=False)
    audio_asset_id: str | None = Field(default=None, max_length=128)
    emotion: list[Annotated[float, Field(ge=0, le=1, allow_inf_nan=False, strict=True)]] | None = Field(
        default=None, min_length=8, max_length=8)

    @model_validator(mode='after')
    def timing(self) -> Dialogue:
        if self.end <= self.start:
            raise ValueError('dialogue end must follow start')
        return self


class LipSync(BaseModel):
    model_config = ConfigDict(extra='forbid')
    character_id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,40}$')
    face_box: list[float] | None = Field(default=None, min_length=4, max_length=4)
    asset_id: str | None = Field(default=None, max_length=128)
    run_id: str | None = Field(default=None, max_length=128)
    review: Review = 'pending'

    @model_validator(mode='after')
    def region(self) -> LipSync:
        if self.face_box is None:
            if self.review == 'accepted':
                raise ValueError('approved lip sync requires a face region')
            return self
        x1, y1, x2, y2 = self.face_box
        if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
            raise ValueError('face box must be an ordered normalized region')
        if self.review == 'accepted' and (not self.asset_id or not self.run_id):
            raise ValueError('approved lip sync requires its result and production run')
        return self


class StyleEnhancers(BaseModel):
    """TeleStyle V2 prompt-enhancer output: short descriptors generated from the reference images."""
    model_config = ConfigDict(extra='forbid')
    content: str = Field(min_length=1, max_length=120)
    style: str = Field(min_length=1, max_length=120)


class ProductionPlan(BaseModel):
    model_config = ConfigDict(extra='forbid')
    script: str = Field(min_length=1, max_length=20000)
    style: str = Field(min_length=1, max_length=800)
    style_reference_asset_id: str | None = Field(default=None, min_length=1, max_length=128)
    style_constraints: str = Field(default='', max_length=1200)
    style_enhancers: StyleEnhancers | None = None
    style_transfer_recipe: Literal['klein-image', 'qwen-style', 'qwen-style-v2'] = 'klein-image'
    characters: list[CharacterCard] = Field(min_length=1, max_length=20)
    scenes: list[SceneCard] = Field(min_length=1, max_length=20)
    dialogue: list[Dialogue] = Field(default_factory=list, max_length=200)
    animatic_asset_id: str | None = Field(default=None, max_length=128)
    animatic_review: Review = 'pending'
    animatic_run_id: str | None = Field(default=None, max_length=128)
    image_width: int = Field(default=640, ge=256, le=1024, multiple_of=16)
    image_height: int = Field(default=1024, ge=256, le=1024, multiple_of=16)
    image_recipe: Literal['klein-image', 'qwen-edit', 'qwen-edit-2511',
                          'qwen-edit-2511-lightning', 'reference-cleanup'] = 'klein-image'
    video_recipe: Literal['story-shot', 'story-shot-quality'] = 'story-shot-quality'
    quality_notes: str = Field(default='', max_length=4000)

    @model_validator(mode='after')
    def links(self) -> ProductionPlan:
        for entries in (self.characters, self.scenes, self.dialogue):
            if len({x.id for x in entries}) != len(entries):
                raise ValueError('duplicate production IDs')
        if len({c.id for c in self.characters} | {s.id for s in self.scenes}) != (
                len(self.characters) + len(self.scenes)):
            raise ValueError('character and scene IDs must be distinct')
        characters = {c.id for c in self.characters}
        if any(d.character_id not in characters for d in self.dialogue):
            raise ValueError('unknown dialogue character')
        if self.animatic_review == 'accepted' and (not self.animatic_asset_id or not self.animatic_run_id):
            raise ValueError('approved animatic requires a video')
        return self
