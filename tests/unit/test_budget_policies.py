import pytest

from agentguard import AgentGuard, BudgetExceeded, Usage


def test_missing_provider_usage_uses_conservative_estimate():
    guard = AgentGuard({"token": {"missing_usage": "estimate"}})

    with guard.run() as run:
        run.invoke(lambda **kwargs: {}, messages="abcd", max_output_tokens=4)

    assert run.state.total_tokens == 5
    assert run.state.usage_source == "estimate"
    assert any(event["event"] == "budget.usage_missing" for event in run.state.events)


def test_missing_provider_usage_can_fail_closed_and_releases_reservation():
    guard = AgentGuard({"token": {"missing_usage": "reject"}})

    with guard.run() as run:
        with pytest.raises(BudgetExceeded) as error:
            run.invoke(lambda **kwargs: {}, messages="hello", max_output_tokens=4)

    assert error.value.reason_code == "usage_missing"
    assert run.budget._reserved_tokens == 0


def test_missing_price_can_fail_closed():
    guard = AgentGuard({"token": {"missing_price": "reject"}})

    with guard.run() as run:
        with pytest.raises(BudgetExceeded) as error:
            run.invoke(
                lambda **kwargs: {"usage": {"prompt_tokens": 1, "completion_tokens": 1}},
                model="unpriced",
                messages="hello",
                max_output_tokens=1,
            )

    assert error.value.reason_code == "price_missing"
    assert run.budget._reserved_tokens == 0


def test_retry_reservations_have_an_independent_parent_budget():
    guard = AgentGuard({"token": {"run_token_budget": 100, "retry_budget_ratio": 0.2}})

    with guard.run() as run:
        reservation = run.budget.reserve(1, 10, purpose="retry")
        reservation.settle(Usage(input_tokens=1, output_tokens=10, source="estimate"))
        with pytest.raises(BudgetExceeded) as error:
            run.budget.reserve(1, 10, purpose="quality_retry")

    assert error.value.reason_code == "retry_token_budget"
