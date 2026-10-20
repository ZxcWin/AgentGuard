"""Monitor a small, real Agent loop from a separate SDK process.

Run with --database .agentguard/demo.db while the local demo console is open.
No provider credentials or external model requests are needed by default.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any

from agentguard import AgentGuard
from agentguard.adapters.storage.sqlite import SQLiteRunStore
from agentguard.adapters.storage.sqlite_policy import SQLitePolicyStore
from agentguard.config.manager import ConfigManager


def lookup_knowledge(query: str) -> dict[str, Any]:
    return {"matches": ["AgentGuard records model and tool lifecycle metadata."], "query_length": len(query)}


def local_model(**request: Any) -> dict[str, Any]:
    """Deterministic provider stand-in; replace with your provider callable."""
    messages = request.get("messages", "")
    tool_result = request.get("tool_result")
    content = (f"Answer based on monitored tool result: {tool_result['matches'][0]}"
               if tool_result else f"I will look this up: {messages}")
    return {"content": content, "usage": {"input_tokens": 24, "output_tokens": 18}}


def run_agent(database: str | Path, query: str, *, provider=local_model) -> dict[str, Any]:
    database = Path(database)
    database.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteRunStore(database)
    policy_store = SQLitePolicyStore(database)
    manager = ConfigManager({
        "token": {"run_token_budget": 4000, "max_output_tokens": 256},
        "budget": {"run_cost_usd": 0.2},
        "pricing": {"version": "local-agent-example-v1", "models": {
            "local-example": {"input_per_million": 0.15, "output_per_million": 0.6},
        }},
        "tools": {"max_calls": 8, "side_effects": {"knowledge_lookup": "read"}},
    }, policy_store=policy_store, environ={})
    guard = AgentGuard(config_manager=manager, run_store=store)
    try:
        with guard.run(name="monitored-python-agent", tenant_id="local-test",
                       project_id="agentguard-demo", environment="local",
                       user_id="developer") as run:
            route = run.invoke(provider, model="local-example", messages=f"Plan a lookup for: {query}",
                               max_output_tokens=128)
            tool_result = run.invoke_tool("knowledge_lookup", lookup_knowledge, query)
            response = run.invoke(provider, model="local-example", messages="Write the final answer",
                                  tool_result=tool_result, max_output_tokens=128)
        return {"run_id": run.state.run_id, "summary": run.state.summary(),
                "tool_result": tool_result, "answer": response["content"],
                "planner_output": route["content"]}
    finally:
        guard.close()
        store.close()
        policy_store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=".agentguard/runs.db")
    parser.add_argument("--query", default="How does AgentGuard monitor an agent?")
    args = parser.parse_args()
    result = run_agent(args.database, args.query)
    print(f"Run ID: {result['run_id']}")
    print(f"Summary: {result['summary']}")
    print(f"Answer: {result['answer']}")
    print("Open the Run ID in the console to inspect both model calls and the tool event.")


if __name__ == "__main__":
    main()
