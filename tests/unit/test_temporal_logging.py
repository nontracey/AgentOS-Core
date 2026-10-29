"""Temporal log flood guard: identical retry noise is collapsed, distinct messages pass."""
from __future__ import annotations

import logging

from agentos.adapters.workflow.temporal import RepeatLimiter


def _record(message: str, name: str = "agentos.temporal") -> logging.LogRecord:
    return logging.LogRecord(name, logging.WARNING, __file__, 1, message, None, None)


def test_repeats_inside_window_are_held_back_and_counted(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("agentos.adapters.workflow.temporal.time.monotonic", lambda: clock[0])
    limiter = RepeatLimiter(window_s=60)
    first = _record("Error polling workflow task: transport error, attempt 1")
    assert limiter.filter(first)
    # Same message with different counters/ids is the same noise.
    for attempt in range(2, 50):
        clock[0] += 1
        assert not limiter.filter(_record(f"Error polling workflow task: transport error, attempt {attempt}"))
    clock[0] += 61
    later = _record("Error polling workflow task: transport error, attempt 99")
    assert limiter.filter(later)
    assert "48 similar messages suppressed" in later.getMessage()


def test_distinct_messages_and_loggers_are_not_merged():
    limiter = RepeatLimiter(window_s=60)
    assert limiter.filter(_record("Worker started"))
    assert limiter.filter(_record("Activity failed: model gateway unreachable"))
    assert limiter.filter(_record("Worker started", name="temporalio.worker._workflow"))


def test_memory_is_bounded():
    limiter = RepeatLimiter(window_s=60, max_keys=8)
    for index in range(40):
        limiter.filter(_record(f"unique message {'x' * index}"))
    assert len(limiter._seen) <= 8
