import asyncio
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from agentguard import AgentGuard, SQLiteAuditStore, AuditUnavailable
from agentguard.application.audit import AuditCoordinator


def test_persistent_intent_result_and_payload_privacy(tmp_path):
    path = tmp_path / "audit.db"
    store = SQLiteAuditStore(path)
    guard = AgentGuard(audit_store=store)
    with guard.run() as run:
        run.invoke(lambda **kw: {"content": "PRIVATE RESPONSE", "usage": {"input_tokens": 2, "output_tokens": 3}}, messages="PRIVATE PROMPT")
        run.invoke_tool("write", lambda value: value, "PRIVATE TOOL ARG")
    assert store.stats()["records"] == 4
    assert store.stats()["unresolved_operations"] == 0
    assert "PRIVATE" not in json.dumps(store.list())
    store.close()
    reopened = SQLiteAuditStore(path)
    assert reopened.stats()["records"] == 4
    assert not AuditCoordinator(reopened).blocked
    reopened.close()


def test_archive_acknowledges_only_durable_deduplicated_records(tmp_path):
    from agentguard.adapters.storage.audit_archive import SQLiteAuditArchive
    from agentguard.cli.main import main
    source = tmp_path / "outbox.db"
    target = tmp_path / "archive.db"
    store = SQLiteAuditStore(source)
    event = {"event":"audit.result","event_id":"stable","kind":"result"}
    store.append(event)
    lease, events = store.claim()
    archive = SQLiteAuditArchive(target)
    archive.emit(events[0])
    archive.close()
    # Crash after durable destination commit but before source acknowledgement.
    store.release(lease)
    store.close()
    assert main(["audit-deliver","--source",str(source),"--archive",str(target)]) == 0
    store = SQLiteAuditStore(source)
    assert store.stats()["pending"] == 0
    store.close()
    import sqlite3
    with sqlite3.connect(target) as db:
        assert db.execute("SELECT COUNT(*) FROM audit_archive").fetchone()[0] == 1
    assert main(["audit-deliver","--source",str(source),"--archive",str(source)]) == 2


def test_before_failure_rejects_without_executing_and_releases_leases():
    class Broken:
        def append(self, event): raise OSError("PRIVATE DISK ERROR")
    calls = []
    guard = AgentGuard(audit_store=Broken())
    with guard.run() as run:
        with pytest.raises(AuditUnavailable) as info:
            run.invoke(lambda: calls.append(1))
        assert not info.value.execution_may_have_completed
        assert run.budget._reserved_tokens == 0
        with pytest.raises(AuditUnavailable):
            run.invoke_tool("write", lambda: calls.append(2))
    assert calls == []


def test_after_failure_preserves_billable_usage_latches_and_requires_reconciliation(tmp_path):
    store = SQLiteAuditStore(tmp_path / "audit.db")
    class FailResult:
        def append(self, event):
            if event["kind"] == "result": raise OSError("disk full")
            return store.append(event)
    guard = AgentGuard(audit_store=FailResult())
    with guard.run() as run:
        with pytest.raises(AuditUnavailable) as info:
            run.invoke(lambda: {"usage": {"input_tokens": 2, "output_tokens": 3}})
        assert info.value.execution_may_have_completed
        operation = info.value.operation_id
        assert run.state.total_tokens == 5
        assert run.budget._reserved_tokens == 0
        with pytest.raises(AuditUnavailable): run.invoke(lambda: "never")
    restarted = AgentGuard(audit_store=store)
    assert restarted._audit.blocked
    with pytest.raises(AuditUnavailable): restarted.recover_audit()
    store.reconcile(operation, outcome="uncertain_recorded", actor="operator")
    restarted.recover_audit()
    with restarted.run() as run:
        assert run.invoke(lambda: "ok") == "ok"
    assert store.stats()["unresolved_operations"] == 0
    store.close()


def test_async_cancel_and_stream_close_have_audit_results(tmp_path):
    store = SQLiteAuditStore(tmp_path / "audit.db")
    guard = AgentGuard(audit_store=store)
    async def work():
        async def provider():
            await asyncio.sleep(20)
        async with guard.run() as run:
            task = asyncio.create_task(run.invoke(provider))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(work())
    with guard.run() as run:
        stream = run.invoke(lambda **kw: iter(["one", "two"]), stream=True)
        next(stream)
        stream.close()
    assert store.stats()["unresolved_operations"] == 0
    assert store.stats()["records"] == 4
    store.close()


def test_outbox_redelivery_and_consumer_deduplication(tmp_path):
    store = SQLiteAuditStore(tmp_path / "audit.db")
    event = {"event": "audit.intent", "event_id": "one", "operation_id": "op", "kind": "intent"}
    assert store.append(event) == store.append(event) == "one"
    with pytest.raises(ValueError): store.append({**event, "action": "different"})
    seen = set()
    class Consumer:
        fail = True
        def emit(self, event):
            seen.add(event["event_id"])
            if self.fail: raise OSError("network")
    consumer = Consumer()
    with pytest.raises(OSError): store.deliver(consumer)
    assert store.stats()["pending"] == 1
    consumer.fail = False
    assert store.deliver(consumer)["delivered"] == 1
    assert len(seen) == 1 and store.stats()["pending"] == 0
    assert store.list()["items"][0]["attempts"] == 2
    store.close()


def test_concurrent_workers_claim_each_event_once_and_capacity_blocks(tmp_path):
    store = SQLiteAuditStore(tmp_path / "audit.db", max_bytes=65536)
    for index in range(50): store.append({"event": "audit.result", "event_id": str(index)})
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: store.claim(limit=20), range(4)))
    ids = [event["event_id"] for _, rows in claims for event in rows]
    assert len(ids) == len(set(ids)) == 50
    with pytest.raises(OSError):
        for _ in range(1000): store.append({"event": "audit.result", "model": "m"*256})
    assert store.stats()["bytes"] <= 65536
    store.close()


def test_security_approval_and_budget_decisions_are_durable_metadata(tmp_path):
    from agentguard import SecurityViolation, BudgetExceeded
    from agentguard.policies.tools import ToolApprovalDenied
    store = SQLiteAuditStore(tmp_path / "audit.db")
    guard = AgentGuard({"security":{"enabled":True}, "tools":{"approval_required_for":["write"]},
                        "token":{"run_token_budget":100}}, audit_store=store)
    with guard.run() as run:
        with pytest.raises(SecurityViolation): run.invoke(lambda **kw:None,messages="sk-"+"a"*48,max_tokens=1)
        with pytest.raises(ToolApprovalDenied): run.invoke_tool("write",lambda:None,approval=lambda:False)
        with pytest.raises(BudgetExceeded): run.invoke(lambda **kw:None,messages="x"*1000,max_tokens=1)
    actions = {event.get("action") for event in store.list()["items"]}
    assert {"security.check","tool.approval","tool.reject","budget.reject"} <= actions
    assert store.stats()["unresolved_operations"] == 0
    store.close()


def test_retention_keeps_undelivered_and_unresolved_groups(tmp_path):
    from datetime import datetime, timedelta, timezone
    store = SQLiteAuditStore(tmp_path / "audit.db")
    audit = AuditCoordinator(store)
    complete = audit.begin(action="done")
    audit.end(complete,outcome="completed")
    unresolved = audit.begin(action="uncertain")
    audit.decision(action="pending",outcome="denied")
    lease, events = store.claim()
    for event in events:
        if event.get("action") != "pending": store.acknowledge(lease,event["event_id"])
    store.release(lease)
    old = (datetime.now(timezone.utc)-timedelta(days=181)).isoformat()
    with store._db:
        store._db.execute("UPDATE audit_outbox SET created_at=?",(old,))
    assert store.prune() == 2
    assert store.stats()["records"] == 2 and store.stats()["unresolved_operations"] == 1
    assert any(event["operation_id"] == unresolved for event in store.list()["items"])
    store.close()
