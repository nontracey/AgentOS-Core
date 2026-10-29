"""Generation engines: the generation node's existing recipes, described for the studio.

Each engine maps to a node tool and recipe that the node already runs behind its own admission
queue. Availability comes from the node's advertised tool schemas (tool name + recipe enum),
so the studio never offers a recipe the node does not have. ``base_seconds``/``per_second``
are rough planning hints; real duration depends on the node's hardware.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

EngineKind = Literal["image", "video", "voice", "lipsync"]


@dataclass(frozen=True)
class Engine:
    id: str
    kind: EngineKind
    label: str
    note: str
    tool: str
    recipe: str = ""
    max_refs: int = 0
    min_refs: int = 0
    steps: int | None = None
    end_frame: bool = False
    lora: bool = False
    base_seconds: float = 60.0      # fixed cost per job (queue, load, transfer)
    per_second: float = 0.0         # extra cost per second of produced media (video, lip sync)

    def estimate(self, media_seconds: float = 0.0) -> float:
        return round(self.base_seconds + self.per_second * media_seconds, 1)

    def public(self, available: bool, reason: str = "") -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "label": self.label, "note": self.note, "tool": self.tool,
                "recipe": self.recipe, "max_refs": self.max_refs, "min_refs": self.min_refs,
                "end_frame": self.end_frame, "lora": self.lora, "available": available, "reason": reason,
                "estimate_seconds": self.estimate(3.0 if self.kind in ("video", "lipsync") else 0.0)}


ENGINES: list[Engine] = [
    Engine("klein", "image", "FLUX.2 Klein · 4 步",
           "速度最快，最多 3 张参考图（角色、场景、画风），支持角色 LoRA。",
           "aigc.image", "klein-image", max_refs=3, steps=4, lora=True, base_seconds=50),
    Engine("qwen-edit-2511", "image", "Qwen-Image-Edit 2511 · 40 步",
           "保留参考图画风和细节最好，适合在定妆图上改动作、表情、构图；必须有参考图，较慢。",
           "aigc.image", "qwen-edit-2511", max_refs=3, min_refs=1, steps=40, base_seconds=420),
    Engine("qwen-edit-2511-fast", "image", "Qwen-Image-Edit 2511 · 4 步快速",
           "4 步加速版，速度接近 Klein，细节略逊于 40 步；必须有参考图。",
           "aigc.image", "qwen-edit-2511-lightning", max_refs=3, min_refs=1, steps=4, base_seconds=90),
    Engine("qwen-image", "image", "Qwen-Image · 文生图",
           "不用参考图从文字生成，适合第一次定妆和场景设定，较慢。",
           "aigc.image", "qwen-image", base_seconds=420),
    Engine("wan22-fast", "video", "Wan2.2 图生视频 · 4 步",
           "从选定画面生成 3–5 秒动作，速度优先。",
           "aigc.generate", "story-shot", base_seconds=90, per_second=150),
    Engine("wan22-quality", "video", "Wan2.2 图生视频 · 20 步",
           "动作更稳、细节更好，但耗时是 4 步的数倍，适合关键镜头。",
           "aigc.generate", "story-shot-quality", base_seconds=120, per_second=510),
    Engine("wan22-flf-fast", "video", "Wan2.2 首尾帧 · 4 步",
           "给定起始画面和结束画面，生成两者之间的动作，适合变身、转场。",
           "aigc.generate", "story-shot-guided", end_frame=True, base_seconds=90, per_second=150),
    Engine("wan22-flf-quality", "video", "Wan2.2 首尾帧 · 20 步",
           "首尾帧的高质量版本，适合关键的变身或动作镜头，耗时长。",
           "aigc.generate", "story-shot-guided-quality", end_frame=True, base_seconds=120, per_second=510),
    Engine("indextts2", "voice", "IndexTTS2 配音",
           "按角色参考音克隆音色，可按台词情绪调节语气。",
           "aigc.speak", base_seconds=25),
    Engine("joyvasa", "lipsync", "JoyVASA 口型",
           "给已生成的视频对上台词口型，只动嘴部；很慢，只建议用在正脸近景。",
           "aigc.lipsync", base_seconds=60, per_second=300),
]

KIND_NAMES = {"image": "图像", "video": "视频", "voice": "配音", "lipsync": "口型"}


def engine(engine_id: str) -> Engine | None:
    return next((e for e in ENGINES if e.id == engine_id), None)


def availability(node_tools: list[dict[str, Any]] | None, *, node_state: str) -> dict[str, tuple[bool, str]]:
    """Which engines the generation node can run, from its advertised tool schemas."""
    result: dict[str, tuple[bool, str]] = {}
    by_name = {str(t.get("name")): t for t in node_tools or []}
    for item in ENGINES:
        if node_state == "unconfigured":
            result[item.id] = (False, "部署还没有指定生成节点")
            continue
        if node_state == "unpaired":
            result[item.id] = (False, "还没有配对生成节点")
            continue
        spec = by_name.get(item.tool)
        if spec is None:
            result[item.id] = (False, "节点没有提供 " + item.tool)
            continue
        if item.recipe:
            enum = (((spec.get("input_schema") or {}).get("properties") or {}).get("recipe") or {}).get("enum")
            if isinstance(enum, list) and item.recipe not in enum:
                result[item.id] = (False, "节点未安装该配方")
                continue
        if node_state == "offline":
            result[item.id] = (False, "生成节点当前离线")
            continue
        result[item.id] = (True, "")
    return result
