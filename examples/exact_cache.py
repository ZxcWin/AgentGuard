"""Offline exact-cache example; no model API or credentials required."""
from agentguard import AgentGuard, CacheRequest

guard = AgentGuard({
    "cache": {"enabled": True, "ttl_seconds": 300, "allowed_models": ["local-example"]},
    "pricing": {"version": "example-v1", "models": {
        "local-example": {"input_per_million": 1, "output_per_million": 2},
    }},
})
options = CacheRequest(namespace="local-provider-v1", prompt_version="v1",
                       authorization_context="reader-permission-v1", data_classification="public", pure=True)
provider_calls = 0


def provider(**kwargs):
    global provider_calls
    provider_calls += 1
    return {"content": "safe answer", "usage": {"input_tokens": 4, "output_tokens": 8}}


with guard.run("cache-example", tenant_id="local", project_id="example", environment="dev", user_id="reader") as run:
    for _ in range(2):
        result = run.invoke(provider, model="local-example", messages="hello", max_tokens=32, cache_options=options)

print(f"Provider calls: {provider_calls}; consumed tokens: {run.state.total_tokens}")
print("Cache events:", [e["event"] for e in run.state.events if e["event"].startswith("cache.")])
guard.clear_cache()
