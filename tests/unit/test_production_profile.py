from dataclasses import asdict, replace
from decimal import Decimal

import pytest

from agentguard import AgentGuard, AgentGuardConfig, BaseInterceptor, BudgetExceeded, ConfigurationError, ConfigManager, load_config


def production(**overrides):
    from agentguard.config import deep_merge
    return AgentGuardConfig.for_production(deep_merge({
        "token": {"max_output_tokens": 20, "run_token_budget": 1000},
        "budget": {"run_cost_usd": 1},
        "pricing": {"models": {"demo": {"input_per_million": 1, "output_per_million": 2}}},
    }, overrides))


def test_production_defaults_roundtrip_and_environment_cannot_weaken():
    config = production()
    assert config.enforcement.profile == "production"
    assert config.token.missing_price == "reject"
    assert config.security.enabled and config.security.action == "block"
    assert AgentGuardConfig.from_mapping(asdict(config)) == config
    with pytest.raises(ConfigurationError, match="missing_price"):
        load_config(asdict(config), environ={"AGENTGUARD_TOKEN__MISSING_PRICE": "observe"})
    assert AgentGuardConfig().token.missing_price == "observe"


@pytest.mark.parametrize("override", [
    {"token": {"max_output_tokens": None}}, {"token": {"max_output_tokens": 0}},
    {"token": {"run_token_budget": None}}, {"budget": {"run_cost_usd": None}},
    {"token": {"missing_price": "observe"}}, {"token": {"missing_usage": "unknown"}},
    {"security": {"enabled": False}}, {"security": {"action": "observe"}},
    {"security": {"detect_secrets": False}}, {"rate_limit": {"redis_failure_policy": "memory"}},
    {"pricing": {"models": {"*": {"input_per_million": 1, "output_per_million": 2}}}},
])
def test_unsafe_production_settings_are_rejected(override):
    with pytest.raises(ConfigurationError, match="production"):
        production(**override)


def test_missing_and_partial_price_tables_are_rejected():
    values = asdict(production())
    for prices in ({}, {"demo": {}}, {"demo": {"input_per_million": 1}}):
        values["pricing"]["models"] = prices
        with pytest.raises(ConfigurationError, match="production"):
            AgentGuardConfig.from_mapping(values)


def test_invalid_update_preserves_version_and_run_snapshot():
    manager = ConfigManager(production())
    old_run = AgentGuard(config_manager=manager).run()
    with pytest.raises(ConfigurationError):
        manager.update({"security": {"enabled": False}})
    assert manager.snapshot().version == old_run.config_version == 1
    with pytest.raises(ConfigurationError):
        replace(manager.config, token=replace(manager.config.token, missing_price="observe"))
    with pytest.raises(ConfigurationError):
        replace(manager.config, budget=replace(manager.config.budget, run_cost_usd=-1))
    with pytest.raises(ConfigurationError):
        replace(manager.config, token=replace(manager.config.token, max_output_tokens=True))


@pytest.mark.parametrize("profile", ["development", "production"])
def test_missing_price_rejected_before_provider_and_admission(profile):
    config = production() if profile == "production" else {"token": {"missing_price": "reject"}}
    calls = []
    with AgentGuard(config).run() as run:
        with pytest.raises(BudgetExceeded, match="价格"):
            run.invoke(lambda **kw: calls.append(kw), model="unknown", messages="hi")
        assert not calls and not run.admission._states
        assert run.state.total_tokens == 0 and run.state.llm_calls == 0


@pytest.mark.parametrize("key", ["max_output_tokens", "max_completion_tokens", "max_tokens"])
@pytest.mark.parametrize("value", [None, True, -1, 0, "10", 2.5])
def test_invalid_request_limits_never_reach_provider(key, value):
    calls = []
    with AgentGuard(production()).run() as run:
        with pytest.raises(BudgetExceeded) as error:
            run.invoke(lambda **kw: calls.append(kw), model="demo", **{key: value})
        assert error.value.reason_code == "output_limit_invalid"
        assert not calls


def test_output_limit_capped_after_interceptor():
    class Change(BaseInterceptor):
        def before_request(self, request):
            request["kwargs"]["max_output_tokens"] = 500
            return request
    calls = []
    with AgentGuard(production()).run() as run:
        run.add_interceptor(Change())
        run.invoke(lambda **kw: calls.append(kw), model="demo")
    assert calls[0]["max_output_tokens"] == 20


@pytest.mark.parametrize("key", ["max_output_tokens", "max_completion_tokens", "max_tokens"])
def test_explicit_stricter_output_alias_is_preserved(key):
    calls = []
    with AgentGuard(production()).run() as run:
        run.invoke(lambda **kw: calls.append(kw), model="demo", **{key: 3})
    assert calls[0][key] == 3


def test_ambiguous_output_limits_rejected():
    with AgentGuard(production()).run() as run:
        with pytest.raises(BudgetExceeded) as error:
            run.invoke(lambda **kw: {}, model="demo", max_tokens=3, max_output_tokens=20)
        assert error.value.reason_code == "output_limit_ambiguous"


def test_fixed_provider_signature_gets_completion_limit():
    calls = []
    def provider(*, model, max_completion_tokens):
        calls.append(max_completion_tokens)
        return {"usage": {"input_tokens": 1, "output_tokens": 2}}
    with AgentGuard(production()).run() as run:
        run.invoke(provider, model="demo")
    assert calls == [20]


def test_provider_without_output_limit_is_rejected():
    with AgentGuard(production()).run() as run:
        with pytest.raises(BudgetExceeded) as error:
            run.invoke(lambda model: {}, model="demo")
        assert error.value.reason_code == "output_limit_unsupported"


@pytest.mark.parametrize("missing", [True, False])
def test_post_call_usage_rejection_settles_consumed_work_and_cleans_up(missing):
    config = production(token={"missing_usage": "reject", "usage_mismatch": "reject"})
    response = {} if missing else {"usage": {"input_tokens": 2, "output_tokens": 30}}
    with AgentGuard(config).run() as run:
        with pytest.raises(BudgetExceeded) as error:
            run.invoke(lambda **kw: response, model="demo", messages="abcd")
        assert error.value.reason_code == ("usage_missing" if missing else "usage_mismatch")
        assert run.state.total_tokens == (21 if missing else 32)
        assert Decimal(str(run.state.estimated_cost_usd)) == (Decimal("0.000041") if missing else Decimal("0.000062"))
        assert run.budget._reserved_tokens == 0 and run.budget._reserved_cost_usd == 0
        assert not run._active_calls
        assert len([e for e in run.state.events if e["event"] == "llm.failed"]) == 1
