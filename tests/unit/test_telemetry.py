import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event

import pytest

from agentguard import AgentGuard, AgentGuardConfig
from agentguard.adapters.events import CallbackEventSink, JSONLEventSink, MemoryEventSink
from agentguard.adapters.storage.sqlite import SQLiteRunStore
from agentguard.application.telemetry import EventDispatcher
from agentguard.domain.events import operational_event
from agentguard.domain.models import RunState


def test_runtime_exports_versioned_metadata_without_payloads():
    sink = MemoryEventSink()
    guard = AgentGuard(event_sink=sink)
    with guard.run() as run:
        run.invoke(lambda **kw: {"content": "PRIVATE RESPONSE", "usage": {"input_tokens": 4, "output_tokens": 8}},
                   model="local", messages="PRIVATE PROMPT")
    assert guard.close()
    events = sink.snapshot()
    assert events[0]["event"] == "run.started"
    assert events[-1]["event"] == "run.completed"
    assert all(event["schema_version"] == 1 and event["run_id"] == run.state.run_id for event in events)
    assert len({event["event_id"] for event in events}) == len(events)
    assert "PRIVATE" not in json.dumps(events)
    assert guard.telemetry_stats()["exported"] == len(events)


def test_concurrent_events_have_unique_ids_and_conserved_delivery_counts():
    sink = MemoryEventSink()
    dispatcher = EventDispatcher(sink, capacity=1000)
    state = RunState(event_observer=dispatcher.emit)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: state.record("run.custom"), range(200)))
    assert dispatcher.close()
    events = sink.snapshot()
    assert len(events) == len({event["event_id"] for event in events}) == 200
    stats = dispatcher.stats()
    assert stats["received"] == stats["exported"] + stats["failed"] + stats["dropped"]


def test_sink_failures_are_counted_and_worker_continues():
    def fail(event):
        raise RuntimeError("PRIVATE SECRET")
    guard = AgentGuard(event_sink=CallbackEventSink(fail))
    with guard.run() as run:
        assert run.invoke(lambda: "ok") == "ok"
    assert guard.close()
    stats = guard.telemetry_stats()
    assert stats["failed"] == stats["received"] > 0
    assert "SECRET" not in json.dumps(stats)


def test_queue_drops_oldest_and_flush_shutdown_are_bounded():
    entered, release = Event(), Event()
    sink = MemoryEventSink()
    def blocked(event):
        entered.set()
        release.wait(5)
        sink.emit(event)
    dispatcher = EventDispatcher(CallbackEventSink(blocked), capacity=2)
    try:
        dispatcher.emit({"event": "run.started", "event_id": "0"})
        assert entered.wait(1)
        for index in range(1, 5):
            dispatcher.emit({"event": "run.completed", "event_id": str(index)})
        assert dispatcher.stats()["dropped"] == 2
        assert dispatcher.stats()["queued"] == 2
        assert not dispatcher.flush(0)
        assert not dispatcher.close(0)
        dispatcher.emit({"event": "run.started"})
        assert dispatcher.stats()["dropped"] == 3
    finally:
        release.set()
        assert dispatcher.close(2)
    assert [event["event_id"] for event in sink.snapshot()] == ["0", "3", "4"]
    assert dispatcher.flush(0)


def test_memory_capacity_copies_and_allowlist():
    sink = MemoryEventSink(1)
    sink.emit({"event": "run.started"})
    sink.emit({"event": "llm.completed", "messages": "PRIVATE", "response": "PRIVATE", "duration_seconds": float("nan")})
    result = sink.snapshot()
    assert len(result) == 1 and result[0]["event"] == "llm.completed"
    assert "PRIVATE" not in json.dumps(result)
    assert "duration_seconds" not in result[0]
    result[0]["event"] = "changed"
    assert sink.snapshot()[0]["event"] == "llm.completed"
    assert operational_event({"event": 'bad\nlabel="PRIVATE"'})["event"] == "run.custom"
    assert "total_tokens" not in operational_event({"event": "llm.failed", "total_tokens": 10**10000})


def test_jsonl_rotates_and_does_not_export_payloads(tmp_path):
    path = tmp_path / "events.jsonl"
    sink = JSONLEventSink(path, max_bytes=65536, backups=1)
    for _ in range(400):
        sink.emit({"event": "run.completed", "model": "m" * 256, "messages": "PRIVATE"})
    assert path.stat().st_size <= 65536
    assert (tmp_path / "events.jsonl.1").is_file()
    assert not (tmp_path / "events.jsonl.2").exists()
    for file in tmp_path.iterdir():
        assert "PRIVATE" not in file.read_text()
        assert all(json.loads(line)["schema_version"] == 1 for line in file.read_text().splitlines())


def test_metrics_use_only_bounded_family_labels():
    dispatcher = EventDispatcher(MemoryEventSink())
    for index in range(100):
        dispatcher.emit({"event": f"llm.custom_{index}", "model": f"PRIVATE-{index}"})
    assert dispatcher.close()
    metrics = dispatcher.prometheus()
    assert 'family="llm"} 100' in metrics
    assert "PRIVATE" not in metrics
    assert "custom_" not in metrics
    assert "agentguard_events_received_total 100" in metrics


def test_tool_cache_counters_are_separate_from_model_cache_and_usage():
    dispatcher = EventDispatcher(MemoryEventSink())
    try:
        for name in ("cache.tool_hit", "cache.tool_miss", "cache.hit", "cache.miss"):
            dispatcher.emit({"event": name, "tool": "PRIVATE-tool-name"})
        metrics = dispatcher.prometheus()
        for name in ("tool_cache_hits", "tool_cache_misses", "cache_hits", "cache_misses"):
            assert f"agentguard_{name}_total 1\n" in metrics
        assert "agentguard_tokens_total 0\n" in metrics
        assert "PRIVATE" not in metrics
    finally:
        dispatcher.close()


def test_metrics_token_cost_and_histogram_do_not_double_count_timeout():
    dispatcher = EventDispatcher(MemoryEventSink())
    try:
        for name in ("llm.timed_out", "llm.failed"):
            dispatcher.emit({"event": name, "total_tokens": 12, "estimated_cost_usd": .1, "duration_seconds": .1})
        dispatcher.emit({"event": "llm.first_token", "duration_seconds": .05})
        dispatcher.emit({"event": "llm.retry_scheduled"})
        metrics = dispatcher.prometheus()
        assert "agentguard_tokens_total 12\n" in metrics
        assert "agentguard_estimated_cost_usd_total 0.1\n" in metrics
        assert "agentguard_llm_duration_seconds_count 1\n" in metrics
        assert 'agentguard_llm_duration_seconds_bucket{le="0.1"} 1\n' in metrics
        assert 'agentguard_ttft_duration_seconds_bucket{le="0.05"} 1\n' in metrics
        assert "agentguard_retries_total 1\n" in metrics
    finally:
        dispatcher.close()


@pytest.mark.parametrize("value", [True, 0, -1, 100001, 1.5])
def test_queue_capacity_validation(value):
    with pytest.raises(ValueError):
        EventDispatcher(MemoryEventSink(), capacity=value)


@pytest.mark.parametrize("value", [True, -1, float("inf"), float("nan"), 61])
def test_flush_timeout_validation(value):
    dispatcher = EventDispatcher(MemoryEventSink())
    try:
        with pytest.raises(ValueError):
            dispatcher.flush(value)
    finally:
        dispatcher.close()


def test_retention_uses_end_time_preserves_active_and_recent(tmp_path):
    store = SQLiteRunStore(tmp_path / "runs.db")
    now = datetime.now(timezone.utc)
    try:
        ids = []
        for status, age, end_age in [("completed", 9, 8), ("failed", 9, 8), ("running", 10, None), ("completed", 9, 1)]:
            state = RunState(status=status, started_at=now - timedelta(days=age))
            state.record("run.started")
            if end_age is not None:
                state.record(f"run.{status}")
                state.events[-1]["timestamp"] = (now - timedelta(days=end_age)).isoformat()
            store.save(state, 1)
            ids.append(state.run_id)
        assert store.prune(7, now=now) == 2
        assert store.prune(7, now=now) == 0
        assert store.get(ids[0]) is None and store.get(ids[1]) is None
        assert store.get(ids[2]) is not None and store.get(ids[3]) is not None
    finally:
        store.close()


@pytest.mark.parametrize("days", [0, 31, True, 1.5, None])
def test_retention_configuration_rejects_invalid_days(days):
    from agentguard import ConfigurationError
    with pytest.raises(ConfigurationError):
        AgentGuardConfig.from_mapping({"telemetry": {"retention_days": days}})
