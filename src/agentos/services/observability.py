"""Owner-scoped execution evidence, without credentials, paths or reasoning deltas.

Only system/user instructions and public tool decisions are recorded. Model reasoning
streams are never inspected. Truncation is explicit; absence is not inferred success.
"""
from __future__ import annotations

import json
import re
from typing import Any

from agentos.kernel.types import ChatMessage

_PRIVATE_KEY = re.compile(
    r"^(?:token(?:_.*)?|.*_token(?:_.*)?|(?:.*_)?api_keys?|.*secret.*|.*password.*|.*credential.*|authorization|endpoint|(?:.*_)?path)$",
    re.I,
)
_TEXT_PRIVATE = re.compile(
    r"https?://[^\s<>\"']+|Bearer\s+[^\s,;]+|"
    r"(?:api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;]+|"
    r"(?:/Users/|/home/|/tmp/|[A-Za-z]:\\)[^\s<>\"']*", re.I
)
LIMIT = 16000


def public_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return "[嵌套内容省略]"
    if isinstance(value, dict):
        return {str(k): "[已隐藏]" if _PRIVATE_KEY.search(str(k)) else
                public_value(v, depth=depth + 1) for k, v in list(value.items())[:100]}
    if isinstance(value, list):
        return [public_value(v, depth=depth + 1) for v in value[:100]]
    if isinstance(value, str):
        text = _TEXT_PRIVATE.sub("[已隐藏]", value)
        return text if len(text) <= LIMIT else text[:LIMIT] + "\n[内容超过 16000 字符，已截断]"
    return value


def prompt_evidence(messages: list[ChatMessage], call_id: str) -> dict[str, Any]:
    return {
        "call_id": call_id,
        "scope": "system_user_only",
        "messages": [{"role": m.role, "content": public_value(m.content)}
                     for m in messages if m.role in {"system", "user"}],
        "note": "系统与用户指令；敏感字段已隐藏。工具回执与模型内部推理不在此视图。",
    }


def public_json(raw: str) -> str:
    """Read historical JSON fields defensively; return only a sanitized DTO body."""
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = {"message": raw}
    return json.dumps(public_value(value), ensure_ascii=False)
