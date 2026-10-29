"""Director rules: script writing, script → cast/sets → storyboard, and shot prompts.

The storyboard pass makes every cut show what happens at that moment of the script. A common
failure of generated storyboards is new dialogue over a recycled composition; the rules below
forbid it and the continuity check flags repeats a model still produces. The language model is
injected (``CompleteJson``); nothing here calls a model or stores anything.
"""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from difflib import SequenceMatcher
from typing import Any

from agentos.domains.aigc.studio.models import EMOTIONS, Character, Line, Location, Project, Shot

CompleteJson = Callable[[list[dict[str, Any]], dict[str, Any]], Awaitable[dict[str, Any]]]

SIZES = ["远景", "全景", "中景", "近景", "特写"]
CAMERAS = ["static", "push_in", "pull_out", "pan_left", "pan_right", "tilt_up", "tilt_down", "shake"]
FORMAT_NAMES = {"9:16": "竖屏 9:16", "16:9": "横屏 16:9", "1:1": "方屏 1:1"}

SCRIPT_RULES = """写作要求：
1. 开头 3 秒内抛出冲突、异常或悬念；结尾留下钩子。
2. 每 20–40 秒安排一次反转、揭示或情绪落差；整集只围绕一个核心冲突。
3. 台词口语化，一句不超过 20 个字；不同角色说话方式要能区分；少用旁白。
4. 【画面】只写能拍出来的具体动作、表情和环境变化，不写心理活动。
5. 适配短视频：场景不超过 3 个，主要角色不超过 4 个。

输出格式（严格遵守，用 Markdown）：
# 剧名
## 场1 地点 · 时间
【画面】……
角色名（情绪）：台词
【画面】……
## 场2 ……"""

CAST_RULES = """从剧本中提取角色和场景，输出 JSON。
- 角色 description：中文，外貌（年龄感、发型发色、眼睛、体型）、服装、标志性物件、性格，60–120 字。
- 角色 visual_prompt：英文，只写外观，可直接放进生图提示词，例如 "middle-aged man, short gray hair, round glasses, navy wool coat, red scarf"。不写画风词。
- 同一角色在全剧外观不变；配角之间外观要明显不同（发色、服装颜色、体型）。
- role 只能是 主角/配角/群演/旁白；有台词的旁白也列为一个角色，名字叫“旁白”。
- voice：中文一句话描述声线，例如“二十岁女声，清亮，语速偏快，带点嘲讽”。
- 场景 visual_prompt：英文，写建筑、空间布局、时间与光线，不写人物。"""

STORYBOARD_RULES = """把这场戏拆成分镜，输出 JSON。规则：
1. 每个镜头的画面必须表现这一刻剧情：说话的人正在做与台词相符的动作和表情；不要让新台词配一个与之无关的画面。
2. 不要连续两个镜头使用相同的景别、人物和构图。对话戏交替使用正反打（说话人 / 听话人反应），关键台词给近景或特写，换场先给远景或全景交代环境。
3. 一个镜头只承载一个清晰动作；最多 2 句台词，合计不超过 30 个字。
4. duration：有台词按每秒 4.5 个字再加 0.8 秒；无台词的动作或反应镜头 2–4 秒。
5. motion：有明显肢体动作、变身、特效、物体运动的镜头填 i2v，其余填 still（静帧加运镜）。i2v 不超过这场戏镜头数的三分之一。
6. image_prompt：英文，描述这一刻画面里看得见的内容：谁、姿势、表情、正在做的动作、前后景物体、光线。用剧中角色名指代人物。不要写画风和画质词。
7. motion_prompt：英文，只写这几秒内的运动，例如 "he turns toward the window, steam rises from the cup, the curtain sways"。still 镜头写轻微动态即可。
8. camera 从 static/push_in/pull_out/pan_left/pan_right/tilt_up/tilt_down/shake 中选，冲击时刻可用 shake。
9. speaker 用角色名；emotion 从 高兴/愤怒/悲伤/恐惧/反感/低落/惊讶/平静 中选，intensity 0.3–0.9。
10. characters 列出画面里出现的角色名；location 写场景名。"""

CAST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "logline": {"type": "string"},
        "characters": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "role": {"type": "string", "enum": ["主角", "配角", "群演", "旁白"]},
            "description": {"type": "string"}, "visual_prompt": {"type": "string"}, "voice": {"type": "string"}},
            "required": ["name", "role", "description", "visual_prompt"]}},
        "locations": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "description": {"type": "string"}, "visual_prompt": {"type": "string"}},
            "required": ["name", "description", "visual_prompt"]}},
    },
    "required": ["characters", "locations"],
}

SHOTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"shots": {"type": "array", "items": {"type": "object", "properties": {
        "location": {"type": "string"},
        "characters": {"type": "array", "items": {"type": "string"}},
        "size": {"type": "string", "enum": SIZES},
        "camera": {"type": "string", "enum": CAMERAS},
        "action": {"type": "string"},
        "image_prompt": {"type": "string"},
        "motion": {"type": "string", "enum": ["still", "i2v"]},
        "motion_prompt": {"type": "string"},
        "duration": {"type": "number"},
        "lines": {"type": "array", "items": {"type": "object", "properties": {
            "speaker": {"type": "string"}, "text": {"type": "string"},
            "emotion": {"type": "string", "enum": EMOTIONS}, "intensity": {"type": "number"}},
            "required": ["speaker", "text", "emotion"]}},
    }, "required": ["size", "action", "image_prompt", "lines", "duration"]}}},
    "required": ["shots"],
}

PROMPT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"image_prompt": {"type": "string"}, "motion_prompt": {"type": "string"}},
    "required": ["image_prompt", "motion_prompt"],
}


def script_messages(project: Project, instructions: str = "") -> list[dict[str, Any]]:
    seconds = project.target_seconds
    shots = max(6, round(seconds / 3))
    cast = "\n".join(f"- {c.name}（{c.role}）：{c.description}" for c in project.characters) or "（未定，可自行设计）"
    brief = [
        f"片名：{project.title}",
        f"画幅：{FORMAT_NAMES.get(project.format, project.format)}；目标时长约 {seconds} 秒（约 {shots} 个镜头）",
        f"题材：{project.genre or '不限'}",
        f"故事梗概：{project.premise or project.logline or '（请根据片名自行构思）'}",
        f"已有角色：\n{cast}",
    ]
    if project.script.strip():
        brief.append("当前剧本：\n" + project.script.strip()[:12000])
    task = instructions.strip() or ("按要求修改当前剧本" if project.script.strip() else "写出完整剧本")
    return [{"role": "system", "content": "你是擅长竖屏漫剧和短剧的编剧。\n" + SCRIPT_RULES},
            {"role": "user", "content": "\n".join(brief) + f"\n\n任务：{task}。只输出剧本。"}]


def split_scenes(script: str) -> list[str]:
    """Scenes by `##`/`场N` headings; long unheaded text is chunked at paragraph breaks."""
    text = script.strip()
    if not text:
        return []
    parts = re.split(r"(?m)^(?=##\s|场\s*\d+|第[一二三四五六七八九十\d]+场)", text)
    scenes = [p.strip() for p in parts if p.strip() and not re.fullmatch(r"#\s.*", p.strip())]
    scenes = [s for s in scenes if not s.startswith("# ") or "\n" in s]
    if len(scenes) <= 1 and len(text) > 1500:
        chunks, current = [], ""
        for paragraph in re.split(r"\n\s*\n", text):
            if current and len(current) + len(paragraph) > 1200:
                chunks.append(current)
                current = ""
            current += paragraph + "\n\n"
        if current.strip():
            chunks.append(current)
        return [c.strip() for c in chunks]
    return scenes or [text]


def _match(name: str, options: dict[str, str]) -> str | None:
    """Fuzzy name → id (models sometimes add titles or drop a character)."""
    clean = _clean_name(name)
    if not clean:
        return None
    if clean in options:
        return options[clean]
    for key, value in options.items():
        if clean in key or key in clean:
            return value
    best = max(options, key=lambda k: SequenceMatcher(None, clean, k).ratio(), default=None)
    if best and SequenceMatcher(None, clean, best).ratio() >= 0.6:
        return options[best]
    return None


def apply_cast(project: Project, data: dict[str, Any]) -> tuple[list[Character], list[Location]]:
    """Merge extracted cast and sets, keeping existing entries (and their ids) by name."""
    existing_chars = {c.name: c for c in project.characters}
    characters: list[Character] = list(project.characters)
    for raw in data.get("characters") or []:
        name = str(raw.get("name") or "").strip()[:30]
        if not name or name in existing_chars:
            continue
        role = raw.get("role") if raw.get("role") in ("主角", "配角", "群演", "旁白") else "配角"
        character = Character(name=name, role=role, description=str(raw.get("description") or "")[:600],
                              visual_prompt=str(raw.get("visual_prompt") or "")[:600])
        character.voice.description = str(raw.get("voice") or "")[:200]
        characters.append(character)
        existing_chars[name] = character
    existing_locs = {loc.name: loc for loc in project.locations}
    locations: list[Location] = list(project.locations)
    for raw in data.get("locations") or []:
        name = str(raw.get("name") or "").strip()[:30]
        if not name or name in existing_locs:
            continue
        location = Location(name=name, description=str(raw.get("description") or "")[:600],
                            visual_prompt=str(raw.get("visual_prompt") or "")[:600])
        locations.append(location)
        existing_locs[name] = location
    return characters, locations


def _clean_name(name: str) -> str:
    return re.sub(r"[（(].*?[)）]|\s", "", name or "")[:30]


def shots_from(data: dict[str, Any], project: Project, scene_title: str
               ) -> tuple[list[Shot], list[Character], list[Location]]:
    """Storyboard JSON → shots. Speakers and places the cast pass missed are added, not dropped."""
    characters, locations = list(project.characters), list(project.locations)
    char_ids = {c.name: c.id for c in characters}
    loc_ids = {loc.name: loc.id for loc in locations}

    def character_id(name: str) -> str | None:
        if not _clean_name(name) or _clean_name(name) == "旁白":
            return None
        found = _match(name, char_ids)
        if found is None:
            created = Character(name=_clean_name(name), role="群演")
            characters.append(created)
            char_ids[created.name] = created.id
            found = created.id
        return found

    def location_id(name: str) -> str | None:
        clean = _clean_name(name)
        if not clean:
            return None
        found = _match(clean, loc_ids)
        if found is None:
            created = Location(name=clean, description=scene_title)
            locations.append(created)
            loc_ids[created.name] = created.id
            found = created.id
        return found

    shots: list[Shot] = []
    for raw in data.get("shots") or []:
        lines = []
        for item in raw.get("lines") or []:
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            emotion = item.get("emotion") if item.get("emotion") in EMOTIONS else "平静"
            intensity = item.get("intensity")
            lines.append(Line(character_id=character_id(str(item.get("speaker") or "")),
                              text=text[:200], emotion=emotion,
                              intensity=float(intensity) if isinstance(intensity, (int, float)) and
                              0 <= intensity <= 1 else 0.6))
        present = [cid for cid in (character_id(str(n)) for n in raw.get("characters") or []) if cid]
        for line in lines:
            if line.character_id and line.character_id not in present:
                present.append(line.character_id)
        spoken = sum(len(line.text) for line in lines)
        suggested = raw.get("duration")
        duration = float(suggested) if isinstance(suggested, (int, float)) else 3.0
        if spoken:
            duration = max(duration, spoken / 4.5 + 0.8)
        shots.append(Shot(
            scene=scene_title[:60], location_id=location_id(str(raw.get("location") or "")),
            character_ids=list(dict.fromkeys(present))[:4],
            size=raw.get("size") if raw.get("size") in SIZES else "中景",
            camera=raw.get("camera") if raw.get("camera") in CAMERAS else "push_in",
            action=str(raw.get("action") or "")[:400], image_prompt=str(raw.get("image_prompt") or "")[:1500],
            motion="i2v" if raw.get("motion") == "i2v" else "still",
            motion_prompt=str(raw.get("motion_prompt") or "")[:800],
            duration=round(max(1.2, min(12.0, duration)), 1), lines=lines))
    return shots, characters, locations


def scene_title(scene: str) -> str:
    first = scene.strip().splitlines()[0] if scene.strip() else ""
    return re.sub(r"^#+\s*", "", first)[:60]


def storyboard_messages(project: Project, scene: str, previous: list[Shot]) -> list[dict[str, Any]]:
    cast = "\n".join(f"- {c.name}（{c.role}）：{c.description}" for c in project.characters)
    sets = "\n".join(f"- {loc.name}：{loc.description}" for loc in project.locations)
    before = ""
    if previous:
        tail = previous[-3:]
        before = "\n上一场最后几个镜头（衔接参考，不要重复）：\n" + "\n".join(
            f"- {s.size} {s.action}" for s in tail)
    return [{"role": "system", "content": "你是动画分镜师。\n" + STORYBOARD_RULES},
            {"role": "user", "content": (
                f"画幅：{FORMAT_NAMES.get(project.format, project.format)}\n角色：\n{cast}\n场景：\n{sets}{before}\n\n"
                f"这场戏的剧本：\n{scene[:6000]}\n\n输出这场戏的全部镜头。")}]


def cast_messages(project: Project) -> list[dict[str, Any]]:
    return [{"role": "system", "content": "你是动画美术设定。\n" + CAST_RULES},
            {"role": "user", "content": f"剧本：\n{project.script[:14000]}"}]


async def breakdown(project: Project, complete_json: CompleteJson,
                    progress: Callable[[str, int, int], Awaitable[None]] | None = None) -> Project:
    """Two passes: cast and sets for the whole script, then a storyboard per scene."""
    if not project.script.strip():
        raise ValueError("剧本为空，先写剧本再拆分镜")
    scenes = split_scenes(project.script)
    total = len(scenes) + 1
    if progress:
        await progress("提取角色与场景", 0, total)
    cast = await complete_json(cast_messages(project), CAST_SCHEMA)
    characters, locations = apply_cast(project, cast)
    updated = project.model_copy(update={
        "characters": characters, "locations": locations,
        "logline": project.logline or str(cast.get("logline") or "")[:200]})
    shots: list[Shot] = []
    for index, scene in enumerate(scenes, start=1):
        if progress:
            await progress(f"拆分第 {index}/{len(scenes)} 场", index, total)
        data = await complete_json(storyboard_messages(updated, scene, shots), SHOTS_SCHEMA)
        scene_shots, cast_now, places_now = shots_from(data, updated, scene_title(scene))
        updated = updated.model_copy(update={"characters": cast_now, "locations": places_now})
        shots += scene_shots
    return updated.model_copy(update={"shots": shots})


def prompt_messages(project: Project, shot: Shot) -> list[dict[str, Any]]:
    names = {c.id: c.name for c in project.characters}
    lines = "\n".join(f"{names.get(line.character_id or '', '旁白')}（{line.emotion}）：{line.text}"
                      for line in shot.lines)
    location = project.location(shot.location_id)
    cast = "\n".join(f"- {c.name}: {c.visual_prompt}" for c in project.characters if c.id in shot.character_ids)
    return [{"role": "system", "content": "你为本地图像和视频模型写英文提示词。\n" + STORYBOARD_RULES},
            {"role": "user", "content": (
                f"镜头：{shot.size}，运镜 {shot.camera}，场景 {location.name if location else '未指定'}\n"
                f"画面描述：{shot.action}\n台词：\n{lines or '（无）'}\n画面中的角色：\n{cast or '（无）'}\n\n"
                "重写这一镜的 image_prompt 和 motion_prompt。")}]


async def refine_prompts(project: Project, shot: Shot, complete_json: CompleteJson) -> Shot:
    data = await complete_json(prompt_messages(project, shot), PROMPT_SCHEMA)
    return shot.model_copy(update={
        "image_prompt": str(data.get("image_prompt") or shot.image_prompt)[:1500],
        "motion_prompt": str(data.get("motion_prompt") or shot.motion_prompt)[:800]})


def continuity_warnings(project: Project) -> list[dict[str, str]]:
    """Flag storyboard patterns that read as recycled pictures."""
    warnings: list[dict[str, str]] = []
    previous: Shot | None = None
    for index, shot in enumerate(project.shots, start=1):
        if previous is not None and previous.size == shot.size and \
                set(previous.character_ids) == set(shot.character_ids) and previous.location_id == shot.location_id:
            warnings.append({"shot_id": shot.id, "message": f"第 {index} 镜与上一镜景别、人物、场景都相同，容易像重复画面"})
        if shot.lines and not shot.action.strip():
            warnings.append({"shot_id": shot.id, "message": f"第 {index} 镜有台词但没有写画面动作"})
        spoken = sum(len(line.text) for line in shot.lines)
        if spoken and shot.duration < spoken / 5.5:
            warnings.append({"shot_id": shot.id, "message": f"第 {index} 镜台词 {spoken} 字，时长偏短"})
        previous = shot
    moving = sum(1 for s in project.shots if s.motion != "still")
    if project.shots and moving / len(project.shots) > 0.45:
        warnings.append({"shot_id": "", "message": f"图生视频镜头占 {moving}/{len(project.shots)}，视频生成会很慢"})
    return warnings
