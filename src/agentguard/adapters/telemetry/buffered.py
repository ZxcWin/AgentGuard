"""Bounded, nonblocking delivery of ordinary telemetry with visible loss."""
from __future__ import annotations

from collections import Counter, deque
import math
from threading import Condition, Thread
from time import monotonic
from typing import Any, Mapping

from ...domain.events import EVENT_FAMILIES, operational_event
from ...ports import EventSink
from ...policies.telemetry import EventSampling


class EventDispatcher:
    def __init__(self, sink: EventSink, *, capacity: int = 1024, batch_size: int = 64,
                 sampling: EventSampling | None = None) -> None:
        if type(capacity) is not int or not 1 <= capacity <= 100000:
            raise ValueError("capacity must be between 1 and 100000")
        self.sink, self.capacity = sink, capacity
        if type(batch_size) is not int or not 1 <= batch_size <= 1000:
            raise ValueError("batch_size must be between 1 and 1000")
        if sampling is not None and not isinstance(sampling, EventSampling):
            raise ValueError("sampling must be EventSampling")
        self.batch_size, self.sampling = batch_size, sampling or EventSampling()
        self._condition = Condition()
        self._queue: deque = deque()
        self._counts: Counter = Counter()
        self._families: Counter = Counter()
        self._runtime: Counter = Counter()
        self._duration: Counter = Counter()
        self._buckets = (.01, .05, .1, .5, 1, 5, 30, 120)
        self._inflight = 0
        self._closed = False
        self._worker = Thread(target=self._work, name="agentguard-telemetry", daemon=True)
        self._worker.start()

    def emit(self, event: Mapping[str, Any], *, tenant_id: str | None = None) -> None:
        value = operational_event(event)
        with self._condition:
            self._counts["received"] += 1
            if self._closed:
                self._counts["dropped"] += 1
                return
            self._families[value["event"].split(".")[0]] += 1
            self._observe(value)
            if not self.sampling.accepts(value, tenant_id):
                self._counts["sampled_out"] += 1
                return
            if len(self._queue) >= self.capacity:
                self._queue.popleft()
                self._counts["dropped"] += 1
            self._queue.append(value)
            self._condition.notify()

    def _observe(self, event: dict[str, Any]) -> None:
        name = event["event"]
        counters = {"llm.completed": "llm_completed", "llm.failed": "llm_failed", "llm.retry_scheduled": "retries",
                    "cache.hit": "cache_hits", "cache.miss": "cache_misses", "budget.rejected": "budget_rejections",
                    "cache.tool_hit": "tool_cache_hits", "cache.tool_miss": "tool_cache_misses",
                    "quality.rejected": "quality_rejections", "security.rejected": "security_rejections"}
        if name in counters:
            self._runtime[counters[name]] += 1
        if name in {"llm.completed", "llm.failed"}:
            for field, metric in (("total_tokens", "tokens"), ("estimated_cost_usd", "estimated_cost_usd")):
                value = event.get(field)
                if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                    self._runtime[metric] += value
        kind = "llm" if name in {"llm.completed", "llm.failed"} else "ttft" if name == "llm.first_token" else None
        duration = event.get("duration_seconds")
        if kind and type(duration) in (int, float) and math.isfinite(duration) and duration >= 0:
            self._duration[f"{kind}_sum"] += duration
            self._duration[f"{kind}_count"] += 1
            for bound in self._buckets:
                if duration <= bound:
                    self._duration[(kind, bound)] += 1

    def _work(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._queue or self._closed)
                if not self._queue:
                    return
                batch = [self._queue.popleft() for _ in range(min(len(self._queue), self.batch_size))]
                self._inflight = len(batch)
            try:
                emit_batch = getattr(self.sink, "emit_batch", None)
                if callable(emit_batch):
                    emit_batch(batch)
                    with self._condition:
                        self._counts["exported"] += len(batch)
                else:
                    for event in batch:
                        try:
                            self.sink.emit(event)
                        except BaseException:
                            with self._condition:
                                self._counts["failed"] += 1
                        else:
                            with self._condition:
                                self._counts["exported"] += 1
                        finally:
                            with self._condition:
                                self._inflight -= 1
                                self._condition.notify_all()
            except BaseException:
                # Sink exceptions (including callback SystemExit) cannot kill the worker.
                # Never export exception text: it can contain prompts/credentials.
                with self._condition:
                    self._counts["failed"] += len(batch)
            finally:
                with self._condition:
                    self._inflight = 0
                    self._condition.notify_all()

    @staticmethod
    def _timeout(timeout: float) -> float:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 <= timeout <= 60:
            raise ValueError("timeout must be between 0 and 60 seconds")
        return float(timeout)

    def flush(self, timeout: float = 5.0) -> bool:
        deadline = monotonic() + self._timeout(timeout)
        with self._condition:
            return self._condition.wait_for(lambda: not self._queue and not self._inflight, max(0, deadline - monotonic()))

    def close(self, timeout: float = 5.0) -> bool:
        timeout = self._timeout(timeout)
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        # A stuck user callback cannot be forcibly killed; daemon remains visible.
        self._worker.join(timeout)
        return not self._worker.is_alive()

    def stats(self) -> dict[str, Any]:
        with self._condition:
            return {**{name: self._counts[name] for name in ("received", "exported", "failed", "dropped", "sampled_out")},
                    "queued": len(self._queue), "inflight": self._inflight, "capacity": self.capacity,
                    "closed": self._closed, "worker_alive": self._worker.is_alive(),
                    "batch_size": self.batch_size, "success_sample_rate": self.sampling.success_rate,
                    "families": {name: self._families[name] for name in sorted(EVENT_FAMILIES)}}

    def prometheus(self) -> str:
        stats = self.stats()
        lines = []
        for name in ("received", "exported", "failed", "dropped", "sampled_out"):
            lines.extend((f"# TYPE agentguard_events_{name}_total counter", f"agentguard_events_{name}_total {stats[name]}"))
        for name in ("queued", "inflight", "capacity"):
            lines.extend((f"# TYPE agentguard_events_{name} gauge", f"agentguard_events_{name} {stats[name]}"))
        lines.append("# TYPE agentguard_events_by_family_total counter")
        for name, count in stats["families"].items():
            lines.append(f'agentguard_events_by_family_total{{family="{name}"}} {count}')
        with self._condition:
            for name in ("llm_completed", "llm_failed", "retries", "cache_hits", "cache_misses", "budget_rejections",
                         "tool_cache_hits", "tool_cache_misses",
                         "quality_rejections", "security_rejections", "tokens", "estimated_cost_usd"):
                lines.extend((f"# TYPE agentguard_{name}_total counter", f"agentguard_{name}_total {self._runtime[name]}"))
            for kind in ("llm", "ttft"):
                metric = f"agentguard_{kind}_duration_seconds"
                lines.append(f"# TYPE {metric} histogram")
                for bound in self._buckets:
                    lines.append(f'{metric}_bucket{{le="{bound}"}} {self._duration[(kind, bound)]}')
                lines.append(f'{metric}_bucket{{le="+Inf"}} {self._duration[f"{kind}_count"]}')
                lines.append(f'{metric}_count {self._duration[f"{kind}_count"]}')
                lines.append(f'{metric}_sum {self._duration[f"{kind}_sum"]}')
        return "\n".join(lines) + "\n"
