import threading
import time

import pytest

from agentguard import AgentGuard, CircuitBreaker, RateLimitExceeded
from agentguard.policies.rate_limit import AdmissionController, SlidingWindowLimiter
from agentguard.config import AgentGuardConfig


def test_rate_limiter_does_not_consume_request_slot_when_token_limit_rejects():
    from agentguard.limits import RateLimiter

    limiter = RateLimiter(AgentGuardConfig.from_mapping({"rate_limit": {"rpm": 2, "tpm": 1}}).rate_limit)
    assert limiter.try_acquire(2) is False
    assert limiter.try_acquire(1) is True
    assert limiter.try_acquire(1) is False


def test_admission_lease_releases_concurrency_idempotently():
    config = AgentGuardConfig.from_mapping({"rate_limit": {"max_concurrency": 1}})
    controller = AdmissionController(config.rate_limit)
    lease = controller.acquire(key="tenant-1")
    with pytest.raises(RateLimitExceeded):
        controller.acquire(key="tenant-1")
    lease.release()
    lease.release()
    second = controller.acquire(key="tenant-1")
    second.release()


def test_admission_wait_timeout_records_rejection_event():
    guard = AgentGuard({"rate_limit": {"max_concurrency": 1, "wait_timeout_seconds": 0.01}})
    entered = threading.Event()
    release = threading.Event()

    def provider(**kwargs):
        entered.set()
        release.wait(1)
        return {"usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    with guard.run() as first:
        worker = threading.Thread(target=lambda: first.invoke(provider, messages="x", max_output_tokens=1))
        worker.start()
        assert entered.wait(1)
        with pytest.raises(RateLimitExceeded):
            first.invoke(provider, messages="x", max_output_tokens=1)
        release.set()
        worker.join(1)

    assert any(event["event"] == "rate_limit.rejected" for event in first.state.events)


def test_circuit_breaker_allows_only_one_half_open_probe():
    breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=0)
    breaker.before_call()
    breaker.record_failure()
    breaker.before_call()
    with pytest.raises(Exception):
        breaker.before_call()
    breaker.record_success()
    breaker.before_call()


def test_tool_calls_use_scoped_limit_and_release_concurrency():
    guard = AgentGuard({"rate_limit": {"max_tool_calls": 1, "max_concurrency": 1}})

    with guard.run(tenant_id="tenant-a") as run:
        assert run.invoke_tool("search", lambda query: query, "hello") == "hello"
        with pytest.raises(RateLimitExceeded):
            run.invoke_tool("search", lambda query: query, "again")

    assert run.state.tool_calls == 1
    assert any(event["event"] == "tool.completed" for event in run.state.events)


def test_admission_cleans_idle_scope_keys_at_cardinality_limit():
    config = AgentGuardConfig.from_mapping({"rate_limit": {"max_scope_keys": 1}})
    controller = AdmissionController(config.rate_limit)
    first = controller.acquire(key="first")
    first.release()
    second = controller.acquire(key="second")
    second.release()
    assert set(controller._states) == {"second"}


def test_admission_rejects_new_key_when_all_keys_are_active():
    config = AgentGuardConfig.from_mapping({"rate_limit": {"max_scope_keys": 1, "max_concurrency": 1}})
    controller = AdmissionController(config.rate_limit)
    lease = controller.acquire(key="first")
    with pytest.raises(RateLimitExceeded, match="cardinality"):
        controller.acquire(key="second")
    lease.release()


def test_cardinality_churn_cannot_erase_live_rate_windows(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("agentguard.policies.rate_limit.monotonic",lambda:clock[0])
    config = AgentGuardConfig.from_mapping({"rate_limit":{"max_scope_keys":1,"rpm":1}})
    controller = AdmissionController(config.rate_limit)
    controller.acquire(key="first").release()
    with pytest.raises(RateLimitExceeded): controller.acquire(key="second")
    with pytest.raises(RateLimitExceeded): controller.acquire(key="first")
    clock[0] += 61
    controller.acquire(key="second").release()


def test_rejected_budget_does_not_strand_half_open_probe():
    from agentguard import BudgetExceeded
    guard = AgentGuard({"token":{"run_token_budget":0}})
    with guard.run() as run:
        key = run.circuit_key(model=None)
        breaker = run.circuits.get(key)
        breaker.recovery_seconds = 0
        breaker.opened_at = 0
        with pytest.raises(BudgetExceeded): run.invoke(lambda:None)
        assert not breaker.probe_in_flight
