from types import SimpleNamespace

import pytest
from agentguard import AgentGuard, BudgetExceeded
from agentguard.application.runtime import _extract_usage


@pytest.mark.parametrize("usage",[
    {"prompt_tokens":100,"completion_tokens":20,"prompt_tokens_details":{"cached_tokens":30},"completion_tokens_details":{"reasoning_tokens":5}},
    {"input_tokens":100,"output_tokens":20,"input_tokens_details":{"cached_tokens":30},"output_tokens_details":{"reasoning_tokens":5}},
])
def test_nested_usage_details_are_subsets_and_preserve_response(usage):
    extracted = _extract_usage({"usage":usage})
    assert (extracted.input_tokens,extracted.output_tokens,extracted.cached_tokens,extracted.reasoning_tokens) == (100,20,30,5)
    obj = SimpleNamespace(usage=SimpleNamespace(**usage))
    assert _extract_usage(obj).total_tokens == 120
    with AgentGuard().run() as run:
        assert run.invoke(lambda:obj) is obj
        assert run.state.cached_tokens == 30 and run.state.reasoning_tokens == 5


@pytest.mark.parametrize("value",[True, 1.5, "2", -1, None, 10**30])
def test_invalid_provider_usage_becomes_unknown(value):
    assert _extract_usage({"usage":{"input_tokens":value,"output_tokens":1}}).source == "unknown"


def test_invalid_usage_subsets_are_not_trusted():
    assert _extract_usage({"usage":{"input_tokens":10,"cached_tokens":11}}).source == "unknown"


@pytest.mark.parametrize("field",["input","prompt","instructions","system","tools","functions","response_format"])
def test_non_message_input_counts_toward_budget_before_execution(field):
    calls = []
    with AgentGuard({"token":{"run_token_budget":30}}).run() as run:
        with pytest.raises(BudgetExceeded):
            run.invoke(lambda **kw:calls.append(1), **{field:"x"*400,"max_tokens":1})
    assert calls == []
