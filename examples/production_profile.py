"""Offline example: strict local enforcement, without provider credentials."""
from agentguard import AgentGuard, AgentGuardConfig, BudgetExceeded


config = AgentGuardConfig.for_production({
    "token": {"max_output_tokens": 128, "run_token_budget": 2000},
    "budget": {"run_cost_usd": 0.01},
    # Example rates only; replace with your provider's current model prices.
    "pricing": {"version": "example-v1", "models": {
        "local-example": {"input_per_million": 1, "output_per_million": 2},
    }},
})


def provider(**kwargs):
    return {"content": "safe example", "usage": {"input_tokens": 4, "output_tokens": 8}}


with AgentGuard(config).run("strict-example") as run:
    run.invoke(provider, model="local-example", messages="hello")
    try:
        run.invoke(provider, model="unpriced-model", messages="hello")
    except BudgetExceeded as error:
        print(f"Blocked before provider: {error.reason_code}")

print(f"Consumed: {run.state.total_tokens} tokens, estimated USD {run.state.estimated_cost_usd}")
