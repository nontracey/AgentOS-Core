"""Studio planning rules: arguments match the core node tool contracts; prerequisites are explained."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from jsonschema import validate

from agentos.adapters.aigc.tools import AigcToolAdapter
from agentos.domains.aigc.studio import plan
from agentos.domains.aigc.studio.engines import engine
from agentos.domains.aigc.studio.models import Character, Line, Location, Project, Shot, StudioDoc, slot_key
from agentos.domains.aigc.studio.plan import Pick, PlanError
from agentos.kernel.types import AuthContext, utc_now
from agentos.services.studio.service import StudioService


@pytest.fixture
def schemas(tmp_path: Path) -> dict[str, dict[str, Any]]:
    adapter = AigcToolAdapter(base_url="http://comfy.invalid", sandbox_dir=tmp_path, ffmpeg="ffmpeg",
                              speech_command=["speak"], lipsync_command=["lipsync"])
    actor = AuthContext(subject="t", tenant_id="default", scopes=["read"], policy_version="t",
                        expires_at=utc_now() + timedelta(hours=1))
    return {spec.name: spec.input_schema for spec in adapter.list_tools(actor)}


def project() -> Project:
    hero = Character(id="char_a", name="阿岚", role="主角", visual_prompt="young woman, short black hair")
    narrator = Character(id="char_n", name="旁白", role="旁白")
    hall = Location(id="loc_a", name="车站", visual_prompt="small rural train station at dusk")
    shots = [
        Shot(id="shot_1", location_id="loc_a", character_ids=["char_a"], action="她看向站台尽头",
             lines=[Line(id="line_1", character_id="char_a", text="车还会来吗？", emotion="悲伤", intensity=0.7)],
             motion="i2v", face_box=[0.3, 0.2, 0.6, 0.5]),
        Shot(id="shot_2", location_id="loc_a", character_ids=["char_a"], action="列车灯光照亮她的脸",
             lines=[Line(id="line_2", text="那天的最后一班车，晚点了十分钟。")], motion="flf2v"),
        Shot(id="shot_3", action="空荡的站台"),
    ]
    return Project(id="prj_test", title="晚点", characters=[hero, narrator], locations=[hall], shots=shots)


def pick(asset: str, duration: float = 0.0) -> Pick:
    return Pick(take_id="take_" + asset, asset_id=asset, duration=duration)


def test_arguments_validate_against_node_tool_contracts(schemas: dict[str, dict[str, Any]]) -> None:
    p = project()
    picks = {slot_key("char_ref", "char_a"): pick("asset_ref_a"), slot_key("loc_ref", "loc_a"): pick("asset_loc"),
             slot_key("style_ref", p.id): pick("asset_style"), slot_key("frame", "shot_1"): pick("asset_f1"),
             slot_key("frame", "shot_2"): pick("asset_f2"), slot_key("end_frame", "shot_2"): pick("asset_e2"),
             slot_key("voice_ref", "char_a"): pick("asset_v"), slot_key("voice_ref", "char_n"): pick("asset_n"),
             slot_key("clip", "shot_1"): pick("asset_c1"), slot_key("voice", "line_1"): pick("asset_l1", 1.4)}
    cases = [("frame", "shot_1", "klein"), ("frame", "shot_3", "qwen-image"), ("end_frame", "shot_2", "klein"),
             ("frame", "shot_1", "qwen-edit-2511-fast"), ("char_ref", "char_a", "qwen-edit-2511"),
             ("loc_ref", "loc_a", "klein"), ("clip", "shot_1", "wan22-fast"), ("clip", "shot_2", "wan22-flf-fast"),
             ("voice", "line_1", "indextts2"), ("voice", "line_2", "indextts2"),
             ("voice_sample", "char_a", "indextts2"), ("lipsync", "shot_1", "joyvasa")]
    for kind, target, engine_id in cases:
        chosen = engine(engine_id)
        assert chosen is not None
        spec = plan.prepare(p, kind, target, chosen, picks, seed=7)
        validate(spec.arguments, schemas[spec.tool])
        assert spec.slot == plan.GENERATED[kind][0] and spec.label
    frame = plan.prepare(p, "frame", "shot_1", engine("klein"), picks, seed=7)  # type: ignore[arg-type]
    assert frame.arguments["reference_asset_ids"] == ["asset_ref_a", "asset_loc", "asset_style"]
    assert frame.meta["legend"][0].startswith("image 1 shows 阿岚")
    end = plan.prepare(p, "end_frame", "shot_2", engine("klein"), picks, seed=7)  # type: ignore[arg-type]
    assert end.arguments["reference_asset_ids"][0] == "asset_f2"
    voice = plan.prepare(p, "voice", "line_1", engine("indextts2"), picks, seed=1)  # type: ignore[arg-type]
    assert voice.arguments["emotion"][2] == 0.56 and voice.arguments["input_asset_id"] == "asset_v"
    narrated = plan.prepare(p, "voice", "line_2", engine("indextts2"), picks, seed=1)  # type: ignore[arg-type]
    assert narrated.arguments["input_asset_id"] == "asset_n" and "emotion" not in narrated.arguments
    lip = plan.prepare(p, "lipsync", "shot_1", engine("joyvasa"), picks, seed=1)  # type: ignore[arg-type]
    assert lip.arguments["dialogue_timing"] == [{"start": 0.25, "end": 1.65}]
    reference = plan.prepare(p, "char_ref", "char_a", engine("qwen-edit-2511"), {}, seed=1)  # type: ignore[arg-type]
    assert reference.engine == "klein"   # nothing to edit yet: falls back to a text-to-image engine


def test_prerequisites_are_explained() -> None:
    p = project()
    klein, guided = engine("klein"), engine("wan22-flf-fast")
    assert klein and guided
    with pytest.raises(PlanError, match="起始画面"):
        plan.prepare(p, "end_frame", "shot_2", klein, {}, seed=1)
    with pytest.raises(PlanError, match="先生成并选定这一镜的画面"):
        plan.prepare(p, "clip", "shot_1", engine("wan22-fast"), {}, seed=1)  # type: ignore[arg-type]
    with pytest.raises(PlanError, match="尾帧"):
        plan.prepare(p, "clip", "shot_2", guided, {slot_key("frame", "shot_2"): pick("f")}, seed=1)
    with pytest.raises(PlanError, match="参考音"):
        plan.prepare(p, "voice", "line_1", engine("indextts2"), {}, seed=1)  # type: ignore[arg-type]
    with pytest.raises(PlanError, match="框出"):
        plan.prepare(p, "lipsync", "shot_2", engine("joyvasa"), {slot_key("clip", "shot_2"): pick("c")}, seed=1)  # type: ignore[arg-type]
    with pytest.raises(PlanError, match="参考图"):
        plan.prepare(p, "frame", "shot_3", engine("qwen-edit-2511"), {}, seed=1)  # type: ignore[arg-type]
    with pytest.raises(PlanError, match="不存在"):
        plan.prepare(p, "frame", "missing", klein, {}, seed=1)
    with pytest.raises(PlanError, match="不能用来生成"):
        plan.prepare(p, "voice", "line_1", klein, {}, seed=1)


def test_one_click_stages_plan_only_missing_work() -> None:
    p = project()
    work, skipped = plan.stage_work(p, "refs", {})
    assert work == [("char_ref", "char_a"), ("loc_ref", "loc_a")]   # the narrator needs no picture
    work, skipped = plan.stage_work(p, "voices", {slot_key("voice_ref", "char_a"): pick("v")})
    assert work == [("voice", "line_1")] and "旁白 没有参考音" in skipped[0]
    frames = {slot_key("frame", "shot_1"): pick("a"), slot_key("frame", "shot_2"): pick("b")}
    assert plan.stage_work(p, "frames", frames)[0] == [("frame", "shot_3")]
    assert plan.stage_work(p, "end_frames", frames)[0] == [("end_frame", "shot_2")]
    work, skipped = plan.stage_work(p, "clips", {slot_key("frame", "shot_1"): pick("a")})
    assert work == [("clip", "shot_1")] and "第 2 镜动态" in skipped[0]
    assert plan.stage_engine(p, "clip", "shot_2").end_frame
    p.engines.video = "wan22-flf-fast"
    assert not plan.stage_engine(p, "clip", "shot_1").end_frame
    assert plan.progress(p, frames)["frames"] == 2 and plan.estimate(p, frames)["parts"]["frames"] > 0


def test_timeline_prefers_lip_sync_then_motion_then_frame() -> None:
    p = project()
    picks = {slot_key("frame", "shot_1"): pick("f1"), slot_key("clip", "shot_1"): pick("c1"),
             slot_key("lipsync", "shot_1"): pick("l1"), slot_key("frame", "shot_2"): pick("f2"),
             slot_key("voice", "line_1"): pick("v1", 1.25)}
    shots = plan.timeline(p, picks)
    assert [(s["kind"], s["source_asset"]) for s in shots] == [("video", "l1"), ("image", "f2"), ("card", None)]
    assert shots[0]["lines"][0] == {"text": "车还会来吗？", "speaker": "阿岚", "audio_asset": "v1", "duration": 1.25,
                                    "pause_after": 0.25}
    assert shots[1]["lines"][0]["speaker"] == "旁白" and shots[1]["lines"][0]["audio_asset"] is None


def test_used_take_is_explicit_choice_or_first_candidate() -> None:
    doc = StudioDoc(project=project())
    takes = [{"id": f"t{i}", "slot": slot, "target_id": target, "asset_id": f"a{i}", "meta": {}}
             for i, (slot, target) in enumerate([("frame", "shot_1"), ("frame", "shot_1"), ("render", "prj_test"),
                                                 ("render", "prj_test"), ("music", "prj_test")])]
    picks = StudioService.picks(doc, takes)
    assert picks[slot_key("frame", "shot_1")].take_id == "t0"
    assert picks[slot_key("render", "prj_test")].take_id == "t3"   # the newest cut
    assert slot_key("music", "prj_test") not in picks
    doc.selections[slot_key("frame", "shot_1")] = "t1"
    assert StudioService.picks(doc, takes)[slot_key("frame", "shot_1")].asset_id == "a1"
    doc.selections[slot_key("frame", "shot_1")] = ""   # "use nothing" is a choice too
    assert slot_key("frame", "shot_1") not in StudioService.picks(doc, takes)
    with pytest.raises(ValueError):
        Shot(id="bad id with spaces")
