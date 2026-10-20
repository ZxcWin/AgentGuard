"""Optional terminal-operation spans, with operational metadata only."""
from __future__ import annotations

from datetime import datetime
import importlib
import math
from time import time_ns

from ...domain.events import operational_event


class OpenTelemetryEventSink:
    """Stateless operation spans correlated through SDK run/request IDs.

    The application owns the TracerProvider, processors, exporters and shutdown.
    No network exporter is created or configured by this adapter.
    """
    def __init__(self, *, tracer_provider=None):
        try:
            trace = importlib.import_module("opentelemetry.trace")
        except ImportError:
            raise RuntimeError("OpenTelemetry requires agentguard[otel]") from None
        self._trace = trace
        self._tracer = (tracer_provider or trace.get_tracer_provider()).get_tracer("agentguard", "0.1.0")

    def emit(self, event):
        value = operational_event(event)
        name = value["event"]
        family, outcome = name.split(".", 1)
        if family not in {"run", "llm", "tool"} or outcome not in {
            "completed", "failed", "cancelled", "timed_out", "rejected"
        }:
            return
        # Model/tool timeout notifications also have a failed terminal event.
        if family in {"llm", "tool"} and outcome == "timed_out":
            return
        end = time_ns()
        try:
            timestamp = value.get("timestamp")
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                candidate = int(parsed.timestamp() * 10**9)
                if 0 <= candidate < 2**63:
                    end = candidate
        except (AttributeError, TypeError, ValueError, OverflowError):
            pass
        duration = value.get("duration_seconds", 0)
        duration = duration if type(duration) in {int, float} and math.isfinite(duration) and 0 <= duration <= 86400 else 0
        attributes = {"agentguard." + key: item for key, item in value.items() if item is not None and key != "timestamp"}
        span = self._tracer.start_span("agentguard." + family, start_time=max(0, end - int(duration * 10**9)), attributes=attributes)
        try:
            failed = outcome != "completed" or value.get("outcome") in {"failed", "timed_out", "cancelled"}
            span.set_status(self._trace.Status(self._trace.StatusCode.ERROR if failed else self._trace.StatusCode.OK))
        finally:
            span.end(end_time=end)

    def emit_batch(self, events):
        for event in events:
            self.emit(event)
