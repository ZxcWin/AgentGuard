"""Local content checks; no model service or real credentials required."""
from agentguard import (AgentGuard, AgentGuardConfig, QualityViolation, SecurityViolation)


def main():
    guard = AgentGuard(AgentGuardConfig.from_mapping({
        "security": {"enabled": True, "detect_pii": True},
        "quality": {"enabled": True, "strict_json": True},
    }))
    for name, message, content in (
        ("safe-json", "hello", '{"ok": true}'),
        ("blocked-input", "api_key=local-demo-placeholder", '{"ok": true}'),
        ("invalid-json", "hello", "invalid JSON"),
    ):
        run = guard.run(name)
        try:
            with run:
                result = run.invoke(lambda **kwargs: {"content": content, "usage": {
                    "input_tokens": 2, "output_tokens": 4}}, messages=message)
            print(name, result["content"])
        except (SecurityViolation, QualityViolation) as error:
            print(name, error.reason_code)
        print("status:", run.state.status, "billed tokens:", run.state.total_tokens)


if __name__ == "__main__":
    main()
