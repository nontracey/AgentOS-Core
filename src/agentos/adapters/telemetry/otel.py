"""OpenTelemetry SDK with metadata-only spans and opt-in exporter injection."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter


class OpenTelemetryMetadata:
    def __init__(self, exporter: SpanExporter | None = None) -> None:
        self.provider = TracerProvider()
        if exporter:
            self.provider.add_span_processor(SimpleSpanProcessor(exporter))
        self.tracer = self.provider.get_tracer('agentos.integration', '0.1.0')

    @contextmanager
    def span(self, operation: str, *, run_id: str, node_id: str) -> Iterator[None]:
        # Disable SDK exception events: exception strings may contain private upstream data.
        with self.tracer.start_as_current_span(operation, record_exception=False, set_status_on_exception=False,
             attributes={'agentos.run.id': run_id, 'agentos.node.id': node_id}):
            yield
