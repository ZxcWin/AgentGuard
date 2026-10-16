"""Run from the project root while the console uses .agentguard/runs.db."""
from pathlib import Path

from agentguard import AgentGuard
from agentguard.adapters.storage.sqlite import SQLiteRunStore
from agentguard.adapters.storage.sqlite_policy import SQLitePolicyStore
from agentguard.config.manager import ConfigManager


def main():
    Path(".agentguard").mkdir(exist_ok=True)
    store = SQLiteRunStore(".agentguard/runs.db")
    policies = SQLitePolicyStore(".agentguard/runs.db")
    guard = AgentGuard(config_manager=ConfigManager(policy_store=policies), run_store=store)
    try:
        with guard.run(name="my-local-agent", environment="development") as run:
            result = run.invoke(
                lambda **kwargs: {"usage": {"input_tokens": 12, "output_tokens": 8}},
                model="local-example", messages="Local example, no provider request",
            )
        print(result)
        print(run.state.summary())
    finally:
        store.close()
        policies.close()


if __name__ == "__main__":
    main()
