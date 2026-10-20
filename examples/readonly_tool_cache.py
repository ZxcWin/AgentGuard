"""Offline governed read-only tool caching; no credentials or services required."""
from agentguard import AgentGuard, ToolCacheRequest


def main():
    calls = []
    def lookup(item):
        calls.append(item)
        return {"item": item, "available": True}

    guard = AgentGuard({
        "cache": {"enabled": True, "allowed_tools": ["catalog_lookup"]},
        "tools": {"side_effects": {"catalog_lookup": "read"}},
    })
    options = ToolCacheRequest(
        namespace="local-catalog", prompt_version="n/a", pure=True,
        authorization_context="reader-permissions-v1", data_classification="public",
        tool_version="lookup-v1", data_version="catalog-snapshot-v1",
    )
    try:
        with guard.run("readonly-cache-example", tenant_id="local", project_id="example",
                       environment="dev", user_id="reader") as run:
            for _ in range(2):
                assert run.invoke_tool("catalog_lookup", lookup, "item-42", cache_options=options) == {
                    "item": "item-42", "available": True}
        assert len(calls) == 1 and run.state.tool_calls == 2
        print(f"Data-source queries: {len(calls)}; governed tool calls: {run.state.tool_calls}")
        print([event["event"] for event in run.state.events if event["event"].startswith("cache.")])
    finally:
        guard.close()


if __name__ == "__main__":
    main()
