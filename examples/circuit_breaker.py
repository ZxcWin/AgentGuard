"""Offline model/tool isolation and configured run-local circuit protection."""
from agentguard import AgentGuard, CircuitOpen


def main():
    calls = []

    def unavailable(**kwargs):
        calls.append(1)
        raise ConnectionError("Local simulated failure")

    guard = AgentGuard({"circuit": {"failure_threshold": 1, "recovery_seconds": 30}})
    try:
        with guard.run("circuit-example") as run:
            try:
                run.invoke(unavailable, model="offline")
            except ConnectionError:
                print("First provider call failed; circuit opened.")
            try:
                run.invoke(unavailable, model="offline")
            except CircuitOpen:
                print("Second call blocked before reaching the provider.")
            assert len(calls) == 1
            assert run.invoke(lambda **kw: "ok", model="healthy") == "ok"
            assert run.invoke_tool("offline", lambda: "ok") == "ok"
            print("Other model and tool remain available.")
    finally:
        guard.close()


if __name__ == "__main__":
    main()
