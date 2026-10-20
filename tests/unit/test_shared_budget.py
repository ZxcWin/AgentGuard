from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
fakeredis = pytest.importorskip("fakeredis")
pytest.importorskip("lupa")

from agentguard import AgentGuard, RedisBudgetStore, SQLiteAuditStore
from agentguard.domain.exceptions import BudgetExceeded
from agentguard.domain.models import Usage


def budget(tokens=100, cost="1", client=None):
    store = RedisBudgetStore(client or fakeredis.FakeRedis(), scope="tenant/project/prod", epoch="one")
    store.provision(token_limit=tokens, cost_limit_usd=cost)
    return store


def test_atomic_reservations_under_concurrent_process_clients():
    store = budget()
    def reserve(index):
        other = RedisBudgetStore(store.client, scope="tenant/project/prod", epoch="one")
        try:
            return other.reserve(10, "0.1", reservation_id=str(index))
        except BudgetExceeded:
            return None
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(reserve, range(50)))
    assert len([r for r in results if r]) == 10
    state = store.status()
    assert state["held_tokens"] == "100" and state["held_micros"] == "1000000"
    assert store.client.ttl(store.key) == -1


def test_idempotent_hold_and_settlement_conflicts_and_conservative_rounding():
    store = budget()
    first = store.reserve(10, "0.0000001", reservation_id="one")
    duplicate = store.reserve(10, "0.0000001", reservation_id="one")
    assert store.status()["held_micros"] == "1"
    with pytest.raises(BudgetExceeded): store.reserve(11, 0, reservation_id="one")
    first.settle(Usage(input_tokens=3, output_tokens=2), Decimal("0.0000001"))
    duplicate.settle(Usage(input_tokens=3, output_tokens=2), Decimal("0.0000001"))
    duplicate.release()
    assert store.status()["spent_tokens"] == "5"
    assert store.status()["held_tokens"] == "0"
    with pytest.raises(BudgetExceeded): store.reserve(10, "0.0000001", reservation_id="one")


def test_pause_epoch_fence_and_explicit_audited_orphan_reconciliation(tmp_path):
    store = budget()
    store.reserve(20, ".2", reservation_id="crashed")
    audit = SQLiteAuditStore(tmp_path / "audit.db")
    args = dict(reservation_id="crashed", expected_tokens=20, expected_cost_usd=".2",
                actual_tokens=7, actual_cost_usd=".1", audit_store=audit, actor="operator")
    with pytest.raises(BudgetExceeded): store.reconcile(**args)
    store.pause()
    with pytest.raises(BudgetExceeded): store.reserve(1, 0)
    with pytest.raises(BudgetExceeded): store.reconcile(**{**args, "expected_tokens":19})
    assert store.status()["held_tokens"] == "20"
    store.reconcile(**args)
    store.reconcile(**args)
    assert store.status()["spent_tokens"] == "7" and store.status()["held_tokens"] == "0"
    store.resume(audit_store=audit, actor="operator")
    store.reserve(1, 0).release()
    wrong = RedisBudgetStore(store.client, scope="tenant/project/prod", epoch="two")
    with pytest.raises(BudgetExceeded): wrong.reserve(1, 0)
    with pytest.raises(ValueError): wrong.provision(token_limit=100, cost_limit_usd=1)
    assert audit.stats()["unresolved_operations"] == 0
    audit.close()


def test_unknown_reservation_commit_retains_hold_and_rejects_execution():
    store = budget(10000)
    real_eval = store.client.eval
    def ambiguous(script, *args):
        value = real_eval(script, *args)
        if "local expected" in script: raise ConnectionError("SECRET")
        return value
    store.client.eval = ambiguous
    calls = []
    with AgentGuard(shared_budget_store=store).run() as run:
        with pytest.raises(BudgetExceeded) as error:
            run.invoke(lambda **kw: calls.append(1), max_tokens=10)
        assert "SECRET" not in str(error.value)
        assert store.status(reservation_id=error.value.reservation_id)["r:"+error.value.reservation_id].startswith("held:")
        assert any(e.get("reservation_id") == error.value.reservation_id and e["event"] == "budget.shared_uncertain" for e in run.state.events)
        assert run.budget._reserved_tokens == 0
    assert calls == [] and int(store.status()["held_tokens"]) > 0


def test_runtime_settles_success_and_keeps_started_failure_hold():
    store = budget(10000)
    guard = AgentGuard(shared_budget_store=store)
    with guard.run() as run:
        run.invoke(lambda **kw: {"usage":{"input_tokens":2,"output_tokens":3}}, max_tokens=10)
    assert store.status()["spent_tokens"] == "5" and store.status()["held_tokens"] == "0"
    def fail(**kw): raise ConnectionError("provider failed")
    with guard.run() as run:
        with pytest.raises(ConnectionError): run.invoke(fail, max_tokens=10)
    assert int(store.status()["held_tokens"]) > 0


def test_actual_overrun_is_recorded_and_future_admission_denied():
    store = budget(10)
    held = store.reserve(10, 0)
    held.settle(Usage(input_tokens=20), 0)
    assert store.status()["spent_tokens"] == "20"
    with pytest.raises(BudgetExceeded): store.reserve(1, 0)


def test_micro_cost_handles_extreme_decimal_exponents_without_rounding_down():
    from agentguard.adapters.storage.redis_budget import micros
    assert micros("1e-1000000000") == 1
    assert micros("0e1000000000") == 0
    for value in ("1e1000000", True, "NaN", "Infinity", "-1"):
        with pytest.raises(ValueError): micros(value)
