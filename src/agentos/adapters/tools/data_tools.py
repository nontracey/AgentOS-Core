"""Bounded analysis of authorized text, using Python's csv/json/decimal libraries."""
from __future__ import annotations

import csv
import io
import json
import math
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from agentos.kernel.errors import AgentOSError, ErrorCode


def reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


def finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON number exceeds finite range")
    return number


def csv_profile(text: str, delimiter: str = ",") -> dict[str, Any]:
    try:
        rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff")), delimiter=delimiter, strict=True))
    except csv.Error as exc:
        raise AgentOSError(ErrorCode.INVALID, "CSV is malformed") from exc
    if not rows or not rows[0] or len(rows) > 2001 or len(rows[0]) > 100:
        raise AgentOSError(ErrorCode.INVALID, "CSV needs a header; maximum 2000 rows and 100 columns")
    headers, data = rows[0], rows[1:]
    if any(not h.strip() for h in headers) or len(set(headers)) != len(headers):
        raise AgentOSError(ErrorCode.INVALID, "CSV headers must be nonempty and unique")
    invalid = [i + 2 for i, row in enumerate(data) if len(row) != len(headers)]
    if invalid:
        return {"valid": False, "row_count": len(data), "column_count": len(headers),
                "invalid_rows": invalid[:20], "invalid_row_count": len(invalid),
                "reason": "Row widths differ from the header; statistics were not computed."}
    columns = []
    for i, name in enumerate(headers):
        values = [r[i] for r in data]
        nonempty = [v.strip() for v in values if v.strip()]
        stats: dict[str, Any] = {"name": name, "empty_count": len(values) - len(nonempty),
                                 "distinct_count": len(set(nonempty))}
        try:
            numbers = [Decimal(v) for v in nonempty]
            if numbers and all(n.is_finite() and abs(n.adjusted()) <= 100 for n in numbers):
                # Cover every input digit and the carry of at most 2000 rows.
                precision = max(n.adjusted() for n in numbers) - min(int(n.as_tuple().exponent) for n in numbers) + 5
                with localcontext() as context:
                    context.prec = max(28, precision)
                    total = str(sum(numbers))
                stats["numeric"] = {"count": len(numbers), "min": str(min(numbers)),
                                    "max": str(max(numbers)), "sum": total}
        except InvalidOperation:
            pass  # Mixed/text columns do not silently coerce invalid values to zero.
        columns.append(stats)
    return {"valid": True, "row_count": len(data), "column_count": len(headers),
            "columns": columns, "preview": [dict(zip(headers, row, strict=True)) for row in data[:5]]}


def json_query(text: str, path: list[str | int]) -> dict[str, Any]:
    try:
        value = json.loads(text, parse_constant=reject_constant, parse_float=finite_float)
    except (ValueError, RecursionError) as exc:
        raise AgentOSError(ErrorCode.INVALID, "material is not valid JSON") from exc
    for part in path:
        if isinstance(part, str) and isinstance(value, dict) and part in value:
            value = value.get(part)
        elif type(part) is int and isinstance(value, list) and 0 <= part < len(value):
            value = value[part]
        else:
            raise AgentOSError(ErrorCode.INVALID, "requested JSON field does not exist")
    encoded = json.dumps(value, ensure_ascii=False)
    if len(encoded.encode()) > 32_000:
        raise AgentOSError(ErrorCode.INVALID, "selected JSON is too large; select a narrower field")
    return {"path": path, "value": value}
