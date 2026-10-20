"""Offline schema + retry example. Install agentguard[quality]."""
from agentguard import AgentGuard, AgentGuardConfig

config = AgentGuardConfig.from_mapping({
    "token": {"max_output_tokens": 32, "run_token_budget": 1000, "retry_budget_ratio": 1},
    "retry": {"initial_delay_seconds": 0, "jitter": False},
    "quality": {"enabled": True, "action": "retry", "max_retries": 1,
                "json_schema": {"type": "object", "required": ["ok"], "properties": {"ok": {"const": True}}}},
})
attempts = 0


def provider(**kwargs):
    global attempts
    attempts += 1
    return {"content": '{}' if attempts == 1 else '{"ok":true}',
            "usage": {"input_tokens": 4, "output_tokens": 8}}


with AgentGuard(config).run(name="schema-retry") as run:
    run.invoke(provider)
print({"attempts": attempts, "tokens": run.state.total_tokens, "retries": run.state.retry_count})
