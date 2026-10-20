import json
from threading import Event

import pytest

from agentguard import AgentGuard, CallbackBatchEventSink, EventSampling, MemoryEventSink, SQLiteAuditStore
from agentguard.application.telemetry import EventDispatcher


def test_sampling_keeps_errors_denials_slow_events_and_all_metrics():
    sink = MemoryEventSink()
    dispatcher = EventDispatcher(sink, sampling=EventSampling(success_rate=0, slow_seconds=1))
    events = [
        {"event": "llm.completed", "duration_seconds": .1, "total_tokens": 4},
        {"event": "llm.completed", "duration_seconds": 2, "total_tokens": 5},
        {"event": "llm.failed", "total_tokens": 6},
        {"event": "quality.rejected"}, {"event": "security.finding"},
        {"event": "tool.approval_resolved", "outcome": "denied"},
        {"event": "audit.decision"},
    ]
    for event in events:
        dispatcher.emit({**event, "trace_id": "trace", "messages": "PRIVATE"})
    assert dispatcher.close()
    assert len(sink.snapshot()) == 6
    stats = dispatcher.stats()
    assert stats["received"] == 7 and stats["sampled_out"] == 1
    assert stats["received"] == stats["sampled_out"] + stats["exported"] + stats["failed"] + stats["dropped"]
    assert "agentguard_tokens_total 15\n" in dispatcher.prometheus()
    assert "PRIVATE" not in json.dumps(sink.snapshot())


def test_trace_sampling_is_deterministic_with_bounded_private_tenant_override():
    policy = EventSampling(success_rate=.5, tenant_rates={"PRIVATE tenant": 0, "enabled": 1})
    selected = []
    for index in range(100):
        event = {"event": "llm.completed", "trace_id": f"trace-{index}"}
        accepted = policy.accepts(event)
        assert accepted == policy.accepts({**event, "event": "run.started"})
        assert not policy.accepts(event, "PRIVATE tenant")
        assert policy.accepts(event, "enabled")
        selected.append(accepted)
    assert 20 < sum(selected) < 80
    assert policy.accepts({"event": "run.custom"})  # Unknown identity is retained.
    assert "PRIVATE tenant" not in repr(policy)


def test_sampling_does_not_remove_local_run_history_or_required_audit(tmp_path):
    sink, audit = MemoryEventSink(), SQLiteAuditStore(tmp_path / "audit.db")
    guard = AgentGuard(event_sink=sink, event_sampling=EventSampling(success_rate=0), audit_store=audit)
    with guard.run(tenant_id="PRIVATE tenant") as run:
        run.invoke(lambda: {"content": "PRIVATE response", "usage": {"input_tokens": 1, "output_tokens": 2}})
    assert guard.close() and sink.snapshot() == []
    assert {"run.started", "llm.requested", "llm.completed", "run.completed"} <= {event["event"] for event in run.state.events}
    assert audit.stats()["records"] == 2
    assert guard.telemetry_stats()["sampled_out"] == len(run.state.events)
    assert "PRIVATE" not in json.dumps(audit.list())
    audit.close()


def test_batched_delivery_is_bounded_ordered_and_conserved():
    entered, release = Event(), Event()
    batches = []
    def callback(batch):
        batches.append(batch)
        if len(batches) == 1:
            entered.set()
            release.wait(3)
    dispatcher = EventDispatcher(CallbackBatchEventSink(callback), batch_size=3, capacity=20)
    try:
        dispatcher.emit({"event": "run.started", "event_id": "0"})
        assert entered.wait(1)
        for index in range(1, 11):
            dispatcher.emit({"event": "run.completed", "event_id": str(index), "response": "PRIVATE"})
    finally:
        release.set()
        assert dispatcher.close()
    assert [len(batch) for batch in batches] == [1,3,3,3,1]
    assert [event["event_id"] for batch in batches for event in batch] == [str(index) for index in range(11)]
    assert "PRIVATE" not in json.dumps(batches)
    assert dispatcher.stats()["exported"] == 11


def test_batch_failure_counts_every_event_and_does_not_replay():
    calls = []
    def fail(batch):
        calls.append(len(batch))
        raise SystemExit("PRIVATE exporter error")
    dispatcher = EventDispatcher(CallbackBatchEventSink(fail), batch_size=2)
    for index in range(10):
        dispatcher.emit({"event": "run.started", "event_id": str(index)})
    assert dispatcher.close()
    assert sum(calls) == 10 and dispatcher.stats()["failed"] == 10
    assert dispatcher.stats()["exported"] == 0


@pytest.mark.parametrize("kwargs", [{"success_rate": True}, {"success_rate": -.1}, {"success_rate": float("nan")},
    {"slow_seconds": 0}, {"tenant_rates": {"tenant": 2}}, {"tenant_rates": {str(i): 1 for i in range(1001)}}])
def test_sampling_validation(kwargs):
    with pytest.raises(ValueError):
        EventSampling(**kwargs)


@pytest.mark.parametrize("size", [0, True, 1001, 1.1])
def test_batch_size_validation(size):
    with pytest.raises(ValueError):
        EventDispatcher(MemoryEventSink(), batch_size=size)
