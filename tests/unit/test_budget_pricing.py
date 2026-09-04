import pytest

from agentguard import AgentGuard, BudgetExceeded, Usage


def test_guarded_call_reserves_and_settles_priced_usage():
    guard = AgentGuard(
        {
            "pricing": {
                "version": "v1",
                "models": {"demo": {"input_per_million": 2, "output_per_million": 4}},
            }
        }
    )

    def provider(**kwargs):
        return {"usage": {"prompt_tokens": 1_000_000, "completion_tokens": 500_000}}

    with guard.run(name="priced") as run:
        run.invoke(provider, model="demo", messages="hello", max_output_tokens=500_000)

    assert run.state.estimated_cost_usd == 4.0
    assert run.state.usage_source == "provider"


def test_failed_call_releases_cost_reservation():
    guard = AgentGuard(
        {
            "budget": {"run_cost_usd": 3},
            "pricing": {"models": {"demo": {"input_per_million": 2, "output_per_million": 4}}},
        }
    )

    def provider(**kwargs):
        raise RuntimeError("provider failed")

    with guard.run(name="failed") as run:
        try:
            run.invoke(provider, model="demo", messages="hello", max_output_tokens=500_000)
        except RuntimeError:
            pass

    assert run.state.estimated_cost_usd == 0.0
    assert run.budget._reserved_cost_usd == 0.0


def test_usage_source_is_preserved_for_manual_usage():
    guard = AgentGuard()

    with guard.run(name="manual") as run:
        run.state.add_usage(Usage(input_tokens=2, source="tokenizer"))

    assert run.state.summary()["usage_source"] == "tokenizer"


def test_configured_output_limit_is_injected_without_overriding_stricter_user_limit():
    calls = []
    guard = AgentGuard({"token": {"max_output_tokens": 100}})

    def provider(**kwargs):
        calls.append(kwargs)
        return {"usage": {"prompt_tokens": 1, "completion_tokens": 2}}

    with guard.run(name="limits") as run:
        run.invoke(provider, model="demo", messages="hello")
        run.invoke(provider, model="demo", messages="hello", max_output_tokens=20)
        run.invoke(provider, model="demo", messages="hello", max_tokens=200)

    assert calls[0]["max_output_tokens"] == 100
    assert calls[1]["max_output_tokens"] == 20
    assert calls[2]["max_tokens"] == 100


def test_scope_key_uses_configured_dimensions():
    guard = AgentGuard(
        {
            "budget": {"key_fields": ["tenant_id", "model"]},
            "rate_limit": {"key_fields": ["user_id", "tool"]},
        }
    )

    with guard.run("scoped", user_id="user-1", tenant_id="tenant-1") as run:
        assert run.scope_key(model="demo", tool="search") == (
            "user_id=user-1",
            "tool=search",
            "tenant_id=tenant-1",
            "model=demo",
        )


def test_budget_warning_is_emitted_once_before_hard_limit():
    guard = AgentGuard({"token": {"run_token_budget": 100}, "budget": {"cost_warning_threshold": 0.5}})

    def provider(**kwargs):
        return {"usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    with guard.run(name="warning") as run:
        run.invoke(provider, messages="x", max_output_tokens=60)
        run.invoke(provider, messages="x", max_output_tokens=1)

    warnings = [event for event in run.state.events if event["event"] == "budget.warning"]
    assert len(warnings) == 1
    assert warnings[0]["metric"] == "tokens"


def test_budget_rejection_records_reason_and_action():
    guard = AgentGuard({"token": {"run_token_budget": 1, "on_exceeded": "stop_stream"}})

    with guard.run(name="rejected") as run:
        with pytest.raises(BudgetExceeded) as error:
            run.invoke(lambda **kwargs: None, messages="too long", max_output_tokens=1)

    assert error.value.reason_code == "run_token_budget"
    rejected = [event for event in run.state.events if event["event"] == "budget.rejected"]
    assert rejected[0]["action"] == "stop_stream"
