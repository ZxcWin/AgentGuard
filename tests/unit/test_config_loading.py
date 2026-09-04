import pytest

from agentguard.config import AgentGuardConfig, deep_merge, environment_mapping, load_config
from agentguard.exceptions import ConfigurationError


def test_deep_merge_does_not_mutate_inputs():
    base = {"token": {"run_token_budget": 100}, "telemetry": {"verbose": True}}
    override = {"token": {"max_output_tokens": 20}}

    merged = deep_merge(base, override)

    assert merged == {
        "token": {"run_token_budget": 100, "max_output_tokens": 20},
        "telemetry": {"verbose": True},
    }
    assert base == {"token": {"run_token_budget": 100}, "telemetry": {"verbose": True}}
    assert override == {"token": {"max_output_tokens": 20}}


def test_environment_variables_create_nested_overrides():
    values = environment_mapping(
        {
            "AGENTGUARD_TOKEN__RUN_TOKEN_BUDGET": "1000",
            "AGENTGUARD_TOKEN__ON_EXCEEDED": "stop_stream",
            "AGENTGUARD_TELEMETRY__VERBOSE": "false",
            "UNRELATED": "ignored",
        }
    )

    assert values == {
        "token": {"run_token_budget": 1000, "on_exceeded": "stop_stream"},
        "telemetry": {"verbose": False},
    }


def test_load_config_merges_environment_overrides():
    config = load_config(
        {"token": {"run_token_budget": 100, "max_output_tokens": 25}},
        environ={"AGENTGUARD_TOKEN__RUN_TOKEN_BUDGET": "200"},
    )

    assert config.token.run_token_budget == 200
    assert config.token.max_output_tokens == 25


def test_unknown_fields_are_rejected():
    with pytest.raises(ConfigurationError, match="unknown token field"):
        AgentGuardConfig.from_mapping({"token": {"run_buget": 10}})


@pytest.mark.parametrize(
    "values",
    [
        {"schema_version": 2},
        {"token": {"reservation_buffer": float("nan")}},
        {"budget": {"cost_warning_threshold": 1.1}},
        {"telemetry": {"verbose": "false"}},
        {"rate_limit": []},
    ],
)
def test_invalid_configuration_is_rejected(values):
    with pytest.raises(ConfigurationError):
        AgentGuardConfig.from_mapping(values)


def test_config_is_immutable_after_creation():
    config = AgentGuardConfig.from_mapping({"token": {"run_token_budget": 100}})

    with pytest.raises(AttributeError):
        config.token = config.token


def test_pricing_configuration_is_loaded():
    config = AgentGuardConfig.from_mapping(
        {"pricing": {"version": "2026-01", "models": {"demo": {"input_per_million": 1.5}}}}
    )

    assert config.pricing.version == "2026-01"
    assert config.pricing.models["demo"].input_per_million == 1.5


def test_scope_key_fields_are_validated_and_normalized():
    config = AgentGuardConfig.from_mapping(
        {
            "budget": {"key_fields": ["tenant_id", "model"]},
            "rate_limit": {"key_fields": ("project_id", "tool")},
        }
    )

    assert config.budget.key_fields == ("tenant_id", "model")
    assert config.rate_limit.key_fields == ("project_id", "tool")


@pytest.mark.parametrize(
    "key_fields",
    [[], ["unknown"], ["tenant_id", "tenant_id"], "tenant_id"],
)
def test_invalid_scope_key_fields_are_rejected(key_fields):
    with pytest.raises(ConfigurationError):
        AgentGuardConfig.from_mapping({"budget": {"key_fields": key_fields}})
