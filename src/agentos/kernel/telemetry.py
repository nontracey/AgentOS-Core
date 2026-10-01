"""Metadata tracing port; no prompts, results, arguments or credentials allowed."""
from contextlib import AbstractContextManager
from typing import Protocol


class MetadataTracePort(Protocol):
    def span(self, operation: str, *, run_id: str, node_id: str) -> AbstractContextManager[None]: ...
