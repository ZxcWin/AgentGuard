"""Offline cache reuse after Guard/store replacement with explicit identity."""
from pathlib import Path
import secrets
from tempfile import TemporaryDirectory

from agentguard import AgentGuard, CacheRequest, SQLiteExactCache


def main():
    with TemporaryDirectory(prefix="agentguard-cache-") as directory:
        key = secrets.token_bytes(32)
        options = CacheRequest(namespace="example", prompt_version="1", authorization_context="reader-1",
                               data_classification="public", pure=True, provider_id="local-demo-v1")
        for restart in (False, True):
            store = SQLiteExactCache(Path(directory) / "cache.db")
            guard = AgentGuard({"cache": {"enabled": True, "allowed_models": ["local-demo"]}},
                               cache_store=store, cache_signing_key=key)
            try:
                with guard.run(tenant_id="local", project_id="example", environment="test", user_id="reader") as run:
                    result = run.invoke(lambda **kw: "safe answer", model="local-demo", messages="hello", cache_options=options)
                assert result == "safe answer" and run.state.llm_calls == (0 if restart else 1)
                print(f"restart={restart}: provider calls={run.state.llm_calls}")
            finally:
                guard.close()
                store.close()


if __name__ == "__main__":
    main()
