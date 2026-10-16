"""Image sampling defaults shared by production entry points and recipe adapters."""
from __future__ import annotations

import math
from typing import Any

REFERENCE_CLEANUP_RECIPE = 'reference-cleanup'


def default_image_steps(recipe: str) -> int:
    """Distilled presets use four steps; other Qwen image defaults stay at forty."""
    if recipe == REFERENCE_CLEANUP_RECIPE:
        raise ValueError('reference cleanup has no diffusion sampling steps')
    return 4 if recipe in {'klein-image', 'qwen-edit-2511-lightning'} else 40


def validate_edit_region(value: Any) -> list[float]:
    """A normalized ordered rectangle, with no coercion of strings or booleans."""
    if (not isinstance(value, list) or len(value) != 4 or any(
            type(point) not in (int, float) or not math.isfinite(point) or not 0 <= point <= 1 for point in value)
            or value[0] >= value[2] or value[1] >= value[3]):
        raise ValueError('edit region requires four finite ordered normalized coordinates')
    return [float(point) for point in value]
