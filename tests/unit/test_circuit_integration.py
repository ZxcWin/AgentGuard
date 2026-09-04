import pytest

from agentguard import AgentGuard, CircuitOpen, CircuitRegistry


def test_runtime_opens_scoped_circuit_after_provider_failures():
    guard = AgentGuard({"rate_limit": {"max_concurrency": 1}})

    def provider(**kwargs):
        raise RuntimeError("upstream unavailable")

    with guard.run(tenant_id="tenant-a") as run:
        run.circuits = CircuitRegistry(failure_threshold=1, recovery_seconds=60)
        with pytest.raises(RuntimeError):
            run.invoke(provider, model="demo", messages="hello", max_output_tokens=1)
        with pytest.raises(CircuitOpen):
            run.invoke(provider, model="demo", messages="hello", max_output_tokens=1)

    assert run.budget._reserved_tokens == 0
    assert any(event["event"] == "circuit.rejected" for event in run.state.events)


def test_circuit_keys_are_isolated_by_model_and_tenant():
    registry = CircuitRegistry(failure_threshold=1, recovery_seconds=60)
    registry.before_call("tenant-a|model-a")
    registry.record_failure("tenant-a|model-a", RuntimeError("failed"))

    with pytest.raises(CircuitOpen):
        registry.before_call("tenant-a|model-a")
    registry.before_call("tenant-a|model-b")
    registry.before_call("tenant-b|model-a")
