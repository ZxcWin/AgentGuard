import pytest

from agentguard import AgentGuardConfig, ConfigurationError


def test_config_defaults_are_safe():
    config = AgentGuardConfig.from_mapping({})

    assert config.token.on_exceeded == "reject"
    assert config.budget.max_cost_usd is None
    assert config.rate_limit.rpm is None


def test_config_rejects_invalid_exceeded_action():
    with pytest.raises(ConfigurationError):
        AgentGuardConfig.from_mapping({"token": {"on_exceeded": "ignore"}})


def test_run_summary_tracks_usage():
    from agentguard import RunState, Usage

    run = RunState(name="demo")
    run.add_usage(Usage(input_tokens=3, output_tokens=5), cost_usd=0.01)

    assert run.summary()["total_tokens"] == 8
    assert run.summary()["estimated_cost_usd"] == 0.01
