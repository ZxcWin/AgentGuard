import json

import pytest
pytest.importorskip("opentelemetry.sdk")
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from agentguard import AgentGuard, OpenTelemetryEventSink


def test_real_otel_spans_have_only_safe_metadata_and_one_span_per_operation():
    provider, exporter = TracerProvider(), InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    guard = AgentGuard(event_sink=OpenTelemetryEventSink(tracer_provider=provider))
    with guard.run() as run:
        run.invoke_tool("read", lambda: "PRIVATE tool output")
        run.invoke(lambda **kw: {"content": "PRIVATE response", "usage": {"input_tokens": 1, "output_tokens": 2}}, messages="PRIVATE prompt")
    assert guard.close()
    spans = exporter.get_finished_spans()
    assert [span.name for span in spans] == ["agentguard.tool", "agentguard.llm", "agentguard.run"]
    assert all(span.end_time >= span.start_time and span.status.status_code == StatusCode.OK for span in spans)
    assert all(span.attributes["agentguard.run_id"] == run.state.run_id for span in spans)
    assert "PRIVATE" not in json.dumps([dict(span.attributes) for span in spans])
    # Guard owns its dispatcher, not the caller's provider.
    with provider.get_tracer("application").start_as_current_span("still-alive"):
        pass
    assert exporter.get_finished_spans()[-1].name == "still-alive"
    provider.shutdown()


def test_otel_failed_spans_and_timeout_deduplication():
    provider, exporter = TracerProvider(), InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    sink = OpenTelemetryEventSink(tracer_provider=provider)
    for family in ("llm", "tool"):
        sink.emit({"event": family + ".timed_out", "request_id": "req", "duration_seconds": .1})
        sink.emit({"event": family + ".failed", "request_id": "req", "duration_seconds": .1,
                   "timestamp": "2026-10-16T00:00:00+00:00", "exception": "PRIVATE"})
    spans = exporter.get_finished_spans()
    assert len(spans) == 2 and all(span.status.status_code == StatusCode.ERROR for span in spans)
    assert all(span.end_time - span.start_time == 100_000_000 for span in spans)
    provider.shutdown()


def test_otel_missing_dependency_message_is_actionable(monkeypatch):
    import agentguard.adapters.telemetry.otel as adapter
    def missing(name):
        raise ImportError()
    monkeypatch.setattr(adapter.importlib, "import_module", missing)
    with pytest.raises(RuntimeError, match=r"agentguard\[otel\]"):
        OpenTelemetryEventSink()
