import asyncio

import pytest

from agentguard import AgentGuard, BudgetExceeded


def test_budget_exceeded_can_downgrade_to_configured_fallback_model():
    calls = []
    guard = AgentGuard(
        {
            "token": {"on_exceeded": "downgrade"},
            "budget": {"run_cost_usd": 0.001},
            "degradation": {"fallback_model": "cheap"},
            "pricing": {
                "models": {
                    "expensive": {"input_per_million": 100, "output_per_million": 100},
                    "cheap": {"input_per_million": 1, "output_per_million": 1},
                }
            },
        }
    )

    def provider(**kwargs):
        calls.append(kwargs["model"])
        return {"usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    with guard.run() as run:
        run.invoke(provider, model="expensive", messages="hello", max_output_tokens=100)

    assert calls == ["cheap"]
    assert any(event["event"] == "budget.downgraded" for event in run.state.events)


def test_sync_stream_settles_only_consumed_estimated_tokens():
    guard = AgentGuard({"token": {"run_token_budget": 100}})

    def provider(**kwargs):
        yield "abcd"
        yield "efgh"

    with guard.run() as run:
        stream = run.invoke(provider, messages="prompt", max_output_tokens=20, stream=True)
        assert next(stream) == "abcd"
        stream.close()

    assert run.state.output_tokens == 1
    assert run.state.usage_source == "estimate"
    assert any(event["event"] == "llm.completed" for event in run.state.events)


def test_sync_stream_stops_when_run_budget_is_exhausted():
    guard = AgentGuard({"token": {"run_token_budget": 5, "on_exceeded": "stop_stream"}})

    def provider(**kwargs):
        yield "abcdefgh"
        yield "ijklmnop"

    with guard.run() as run:
        stream = run.invoke(provider, messages="prompt", max_output_tokens=20, stream=True)
        with pytest.raises(BudgetExceeded) as error:
            list(stream)

    assert error.value.reason_code == "stream_token_budget"
    assert any(event["event"] == "budget.stream_stopped" for event in run.state.events)
    assert run.budget._reserved_tokens == 0


def test_async_stream_is_finalized_after_partial_consumption():
    guard = AgentGuard()

    async def provider(**kwargs):
        yield "abcd"
        yield "efgh"

    async def consume() -> None:
        with guard.run() as run:
            stream = run.invoke(provider, messages="prompt", max_output_tokens=20, stream=True)
            assert await anext(stream) == "abcd"
            await stream.aclose()

        assert run.state.output_tokens == 1
        assert run.budget._reserved_tokens == 0

    asyncio.run(consume())
