"""Offline request repair and audited human decision, without supplier calls."""
from pathlib import Path
from tempfile import TemporaryDirectory

from agentguard import AgentGuard, QualityDecision, QualityRecovery, QualityRequest, SQLiteAuditStore


def repair(failure):
    return QualityRequest(kwargs={**failure.request.kwargs, "messages": "valid JSON please"})


def provider(**kwargs):
    return {"content": "{}" if kwargs.get("messages") == "valid JSON please" else "invalid",
            "usage": {"input_tokens": 2, "output_tokens": 3}}


def main():
    with TemporaryDirectory(prefix="agentguard-quality-") as directory:
        audit = SQLiteAuditStore(Path(directory) / "audit.db")
        try:
            for action in ("repair", "human"):
                guard = AgentGuard({"quality": {"enabled": True, "strict_json": True, "action": action, "max_retries": 1},
                                    "retry": {"initial_delay_seconds": 0}}, audit_store=audit,
                    quality_recovery=QualityRecovery(repair=repair,
                        human=lambda failure: QualityDecision(True, "local-example-reviewer", repair(failure))))
                with guard.run(name=action) as run:
                    result = run.invoke(provider, messages="example")
                assert result["content"] == "{}" and run.state.total_tokens == 10 and run.state.llm_calls == 2
                guard.close()
                print(f"{action}: two guarded calls, 10 billed tokens, valid JSON")
            assert audit.stats()["unresolved_operations"] == 0
        finally:
            audit.close()


if __name__ == "__main__":
    main()
