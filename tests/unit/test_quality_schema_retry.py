import asyncio
import json

import pytest

from agentguard import AgentGuard, AgentGuardConfig, BudgetExceeded, ConfigurationError, QualityViolation


SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}


@pytest.mark.parametrize("content,passed", [('{"ok":true}', True), ('{"ok":"yes"}', False), ('{}', False),
    ('{"ok":true,"secret":"value"}', False), ('{"ok":NaN}', False), ('not JSON', False)])
def test_schema_validates_original_content_and_keeps_billing(content, passed):
    pytest.importorskip("jsonschema")
    guard = AgentGuard({"quality": {"enabled": True, "json_schema": SCHEMA}})
    with guard.run() as run:
        def provider(**kwargs): return {"content": content, "usage": {"input_tokens": 2, "output_tokens": 3}}
        if passed:
            run.invoke(provider)
        else:
            with pytest.raises(QualityViolation): run.invoke(provider)
    assert run.state.total_tokens == 5 and run.budget._reserved_tokens == 0
    assert content not in json.dumps(run.state.events)


@pytest.mark.parametrize("schema", [{"$ref": "https://example.invalid/schema"},
    {"$ref": "file:///private"}, {"$id": "https://example.invalid"}, {"type": "invalid"}])
def test_invalid_or_remote_schema_rejected_without_network(schema):
    pytest.importorskip("jsonschema")
    with pytest.raises(ConfigurationError): AgentGuardConfig.from_mapping({"quality": {"json_schema": schema}})


def test_local_definitions_and_content_rules():
    pytest.importorskip("jsonschema")
    config = {"quality": {"enabled": True, "json_schema": {"$defs": {"value": {"type": "integer"}}, "$ref": "#/$defs/value"},
                          "min_chars": 1, "required_substrings": ["2"]}}
    with AgentGuard(config).run() as run:
        assert run.invoke(lambda: '2') == '2'
        with pytest.raises(QualityViolation): run.invoke(lambda: '3')


def config(**overrides):
    from agentguard.config import deep_merge
    return deep_merge({"quality": {"enabled": True, "strict_json": True, "action": "retry", "max_retries": 2},
                       "retry": {"initial_delay_seconds": 0}, "token": {"run_token_budget": 1000,
                       "max_output_tokens": 10, "reservation_buffer": 0, "retry_budget_ratio": .5}}, overrides)


@pytest.mark.parametrize("async_mode", [False, True])
def test_quality_retries_settle_each_attempt_and_share_parent_budget(async_mode):
    calls = []
    def provider(**kwargs):
        calls.append(1)
        return {"content": "bad" if len(calls) < 3 else '{}', "usage": {"input_tokens": 2, "output_tokens": 3}}
    async def async_provider(**kwargs): return provider(**kwargs)
    async def main():
        async with AgentGuard(config()).run() as run:
            assert (await run.invoke(async_provider))["content"] == '{}'
        return run
    if async_mode:
        run = asyncio.run(main())
    else:
        with AgentGuard(config()).run() as run:
            assert run.invoke(provider)["content"] == '{}'
    assert len(calls) == 3 and run.state.total_tokens == 15 and run.state.retry_count == 2
    assert run.budget._spent_retry_tokens == 10
    assert run.budget._reserved_tokens == 0 and not run._pending


def test_quality_retry_budget_and_nested_tools_are_not_bypassed():
    calls = []
    with AgentGuard(config(token={"run_token_budget": 15, "retry_budget_ratio": .1})).run() as run:
        def provider(**kwargs): calls.append(1); return {"content": "bad", "usage": {"input_tokens": 1, "output_tokens": 1}}
        with pytest.raises(BudgetExceeded): run.invoke(provider)
    assert len(calls) == 1
    with AgentGuard(config()).run() as run:
        def provider(**kwargs): run.invoke_tool("write", lambda: None); return {"content": "bad"}
        with pytest.raises(QualityViolation): run.invoke(provider)
    assert run.state.tool_calls == run.state.llm_calls == 1


def test_transport_and_quality_retry_limits_are_independent():
    calls = []
    def provider(**kwargs):
        calls.append(1)
        if len(calls) == 1: raise ConnectionError()
        return {"content": "bad" if len(calls) == 2 else '{}'}
    with AgentGuard(config(retry={"max_retries": 1})).run() as run: run.invoke(provider)
    assert len(calls) == 3 and run.state.retry_count == 2


def test_quality_retry_exhaustion_and_streaming_never_retry():
    with AgentGuard(config(quality={"max_retries": 1})).run() as run:
        with pytest.raises(QualityViolation): run.invoke(lambda **kwargs: {"content": "bad"})
        assert run.state.llm_calls == 2
    with AgentGuard(config()).run() as run:
        with pytest.raises(QualityViolation): list(run.invoke(lambda **kwargs: iter(["bad"]), stream=True))
        assert run.state.llm_calls == 1
def test_stream_overflow_with_retry_action_still_blocks():
    from agentguard import AgentGuard, AgentGuardConfig, QualityViolation
    import pytest
    config = AgentGuardConfig.from_mapping({"quality": {"enabled": True, "action": "retry", "max_retries": 2, "max_chars": 4}})
    with AgentGuard(config).run() as run:
        with pytest.raises(QualityViolation):
            list(run.invoke(lambda **kw: iter(["abc", "def"]), stream=True))
        assert run.state.llm_calls == 1

