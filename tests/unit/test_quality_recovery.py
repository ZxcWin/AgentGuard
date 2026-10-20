import asyncio
import json

import pytest

from agentguard import (AgentGuard, AuditUnavailable, BaseInterceptor, BudgetExceeded,
    ConfigurationError, ExecutionTimeout, QualityDecision, QualityRecovery, QualityRequest,
    QualityViolation, SecurityViolation, SQLiteAuditStore, deep_merge)


def config(action="repair", **extra):
    return deep_merge({"quality": {"enabled": True, "strict_json": True, "action": action, "max_retries": 2},
        "retry": {"initial_delay_seconds": 0}, "token": {"run_token_budget": 1000,
        "max_output_tokens": 10, "reservation_buffer": 0, "retry_budget_ratio": .5}}, extra)


def response(content):
    return {"content": content, "usage": {"input_tokens": 2, "output_tokens": 3}}


def repaired(failure):
    assert failure.response["content"] == "PRIVATE invalid response"
    return QualityRequest(kwargs={**failure.request.kwargs, "messages": "PRIVATE repaired prompt"})


@pytest.mark.parametrize("async_mode", [False, True])
def test_repair_reenters_all_gates_and_settles_each_attempt(async_mode, tmp_path):
    calls, failures = [], []
    store = SQLiteAuditStore(tmp_path / "audit.db")
    def repair(failure):
        failures.append(failure)
        return repaired(failure)
    async def async_repair(failure):
        await asyncio.sleep(0)
        return repair(failure)
    def provider(**kwargs):
        calls.append(kwargs)
        return response("{}" if len(calls) > 1 else "PRIVATE invalid response")
    async def async_provider(**kwargs):
        return provider(**kwargs)
    guard = AgentGuard(config(), quality_recovery=QualityRecovery(repair=async_repair if async_mode else repair), audit_store=store)
    async def main():
        async with guard.run() as run:
            result = await run.invoke(async_provider, messages="PRIVATE prompt")
        return run, result
    if async_mode:
        run, result = asyncio.run(main())
    else:
        with guard.run() as run:
            result = run.invoke(provider, messages="PRIVATE prompt")
    assert result["content"] == "{}" and len(calls) == 2
    assert calls[1]["messages"] == "PRIVATE repaired prompt"
    assert run.state.total_tokens == 10 and run.budget._spent_retry_tokens == 5
    assert run.state.retry_count == 1 and run.budget._reserved_tokens == 0 and not run._pending
    assert failures[0].attempt == 1 and failures[0].run_id == run.state.run_id
    assert "PRIVATE" not in json.dumps(run.state.events) + json.dumps(store.list())
    assert store.stats()["unresolved_operations"] == 0
    assert any(e["event"] == "quality.recovery_completed" for e in run.state.events)
    store.close()


@pytest.mark.parametrize("action", ["repair", "human", "degrade"])
def test_missing_handler_and_nested_tool_never_replay(action):
    for nested in (False, True):
        calls = []
        with AgentGuard(config(action)).run() as run:
            def provider(**kwargs):
                calls.append(1)
                if nested:
                    run.invoke_tool("write", lambda: None)
                return response("bad")
            with pytest.raises(QualityViolation):
                run.invoke(provider)
        assert len(calls) == 1 and run.state.total_tokens == 5
        assert run.budget._reserved_tokens == 0


def test_repair_is_bounded_and_cannot_recurse_or_fabricate_responses():
    for handler in (lambda f: response("{}"), lambda f: QualityRequest(kwargs={"stream": True}),
                    lambda f: QualityRequest(kwargs={"model": "other"}),
                    lambda f: QualityRequest(kwargs={"budget_purpose": "primary"})):
        with AgentGuard(config(), quality_recovery=QualityRecovery(repair=handler)).run() as run:
            with pytest.raises(QualityViolation):
                run.invoke(lambda: response("bad"))
            assert run.state.llm_calls == 1
    def recursive(failure):
        run.invoke_tool("write", lambda: pytest.fail("must not execute"))
    with AgentGuard(config(), quality_recovery=QualityRecovery(repair=recursive)).run() as run:
        with pytest.raises(QualityViolation):
            run.invoke(lambda: response("bad"))
        assert run.state.tool_calls == 0
    with AgentGuard(config(), quality_recovery=QualityRecovery(repair=lambda f: f.request)).run() as run:
        with pytest.raises(QualityViolation):
            run.invoke(lambda: response("bad"))
        assert run.state.llm_calls == 3 and run.state.total_tokens == 15


def test_repaired_request_security_budget_and_quality_are_not_bypassed():
    secret = "api_key=PRIVATE-super-secret-value"
    for extra, handler, error in (
        ({"security": {"enabled": True}}, lambda f: QualityRequest(kwargs={"messages": secret}), SecurityViolation),
        ({"token": {"retry_budget_ratio": .001}}, lambda f: f.request, BudgetExceeded),
    ):
        with AgentGuard(config(**extra), quality_recovery=QualityRecovery(repair=handler)).run() as run:
            with pytest.raises(error):
                run.invoke(lambda **kw: response("bad"))
            assert run.state.llm_calls == 1 and run.state.total_tokens == 5
            assert secret not in json.dumps(run.state.events)


@pytest.mark.parametrize("approved", [True, False])
def test_human_decision_requires_actor_durable_audit_and_new_validated_request(approved, tmp_path):
    store = SQLiteAuditStore(tmp_path / "audit.db")
    calls = []
    def review(failure):
        return QualityDecision(approved, "operator-1", QualityRequest(kwargs={"messages": "corrected"}))
    def provider(**kwargs):
        calls.append(1)
        return response("{}" if len(calls) > 1 else "bad")
    with AgentGuard(config("human"), quality_recovery=QualityRecovery(human=review), audit_store=store).run() as run:
        if approved:
            assert run.invoke(provider)["content"] == "{}"
        else:
            with pytest.raises(QualityViolation):
                run.invoke(provider)
    assert len(calls) == (2 if approved else 1)
    entries = store.list()["items"]
    assert any(e.get("action") == "quality.approval" and e.get("actor_id") == "operator-1"
               and e.get("outcome") == ("approved" if approved else "denied") for e in entries)
    assert store.stats()["unresolved_operations"] == 0
    store.close()


def test_human_approval_is_single_use_and_cannot_bypass_quality(tmp_path):
    store = SQLiteAuditStore(tmp_path / "audit.db")
    handlers = []
    def review(failure):
        handlers.append(1)
        return QualityDecision(True, "reviewer", failure.request)
    with AgentGuard(config("human"), quality_recovery=QualityRecovery(human=review), audit_store=store).run() as run:
        with pytest.raises(QualityViolation):
            run.invoke(lambda: response("bad"))
        assert run.state.llm_calls == 2 and len(handlers) == 1
    assert store.stats()["unresolved_operations"] == 0
    store.close()
    handlers.clear()
    with AgentGuard(config("human"), quality_recovery=QualityRecovery(human=review)).run() as run:
        with pytest.raises(QualityViolation):
            run.invoke(lambda: response("bad"))
        assert handlers == []


def fallback_config(**extra):
    return config("degrade", **deep_merge({"degradation": {"fallback_model": "small", "allowed_regions": ["cn"],
        "model_profiles": {"small": {"capabilities": ["text"], "regions": ["cn"], "max_data_classification": "internal"}}},
        "pricing": {"models": {"small": {"input_per_million": 1, "output_per_million": 1}}}}, extra))


def test_quality_fallback_uses_same_provider_own_budget_and_stops_after_one_switch(tmp_path):
    store = SQLiteAuditStore(tmp_path / "audit.db")
    for success in (False, True):
        models = []
        def provider(**kwargs):
            models.append(kwargs["model"])
            return response("{}" if success and kwargs["model"] == "small" else "bad")
        with AgentGuard(fallback_config(), audit_store=store).run() as run:
            if success:
                assert run.invoke(provider, model="large")["content"] == "{}"
            else:
                with pytest.raises(QualityViolation):
                    run.invoke(provider, model="large")
        assert models == ["large", "small"] and run.state.total_tokens == 10
        assert run.budget._spent_retry_tokens == 5
    assert store.stats()["unresolved_operations"] == 0
    store.close()


@pytest.mark.parametrize("extra,kwargs", [
    ({"pricing": {"models": {}}}, {}),
    ({"degradation": {"allowed_regions": ["eu"]}}, {}),
    ({"degradation": {"data_classification": "restricted"}}, {}),
    ({}, {"tools": [{"name": "write"}]}),
])
def test_quality_fallback_enforces_capability_region_classification_and_price(extra, kwargs):
    cfg = fallback_config(**extra)
    if "pricing" in extra:
        cfg["pricing"]["models"] = {}
    with AgentGuard(cfg).run() as run:
        with pytest.raises(QualityViolation):
            run.invoke(lambda **kw: response("bad"), model="large", **kwargs)
        assert run.state.llm_calls == 1


def test_fallback_checks_final_interceptor_request():
    class InjectTools(BaseInterceptor):
        def before_request(self, request):
            if request["kwargs"].get("model") == "small":
                request["kwargs"]["tools"] = [{"name": "write"}]
            return request
    with AgentGuard(fallback_config()).run() as run:
        run.add_interceptor(InjectTools())
        with pytest.raises(QualityViolation):
            run.invoke(lambda **kw: response("bad"), model="large")
        assert run.state.llm_calls == 1


def test_async_handler_timeout_cancellation_and_audit_failure(tmp_path):
    async def main():
        entered = asyncio.Event()
        async def slow(failure):
            entered.set()
            await asyncio.sleep(10)
        async def provider():
            return response("bad")
        store = SQLiteAuditStore(tmp_path / "audit.db")
        guard = AgentGuard(config(quality={"handler_timeout_seconds": .01}),
                           quality_recovery=QualityRecovery(repair=slow), audit_store=store)
        async with guard.run() as run:
            with pytest.raises(ExecutionTimeout):
                await run.invoke(provider)
            assert not run._active_calls and run.budget._reserved_tokens == 0
        assert store.stats()["unresolved_operations"] == 0
        entered.clear()
        guard.update_config({"quality": {"handler_timeout_seconds": 10}})
        async with guard.run() as run:
            task = asyncio.ensure_future(run.invoke(provider))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert store.stats()["unresolved_operations"] == 0
        store.close()
    asyncio.run(main())
    class BrokenRecoveryAudit:
        def append(self, event):
            if event.get("action") == "quality.repair":
                raise OSError("PRIVATE disk")
    callbacks = []
    with AgentGuard(config(), audit_store=BrokenRecoveryAudit(),
                    quality_recovery=QualityRecovery(repair=lambda f: callbacks.append(1))).run() as run:
        with pytest.raises(AuditUnavailable):
            run.invoke(lambda: response("bad"))
        assert callbacks == [] and run.state.total_tokens == 5


def test_handler_errors_are_sanitized_and_sync_timeout_is_checked(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("agentguard.application.deadlines.monotonic", lambda: now[0])
    def broken(failure):
        raise RuntimeError("PRIVATE callback error")
    def slow(failure):
        now[0] += .01
        return failure.request
    for handler, error in ((broken, QualityViolation), (slow, ExecutionTimeout)):
        with AgentGuard(config(quality={"handler_timeout_seconds": .001}), quality_recovery=QualityRecovery(repair=handler)).run() as run:
            with pytest.raises(error) as info:
                run.invoke(lambda: response("bad"))
            assert "PRIVATE" not in str(info.value) + json.dumps(run.state.events)
            assert run.state.llm_calls == 1


def test_sync_provider_returning_coroutine_uses_async_repair_and_sync_rejects_async_handler():
    async def repair(failure):
        return QualityRequest(kwargs={"messages": "corrected"})
    with AgentGuard(config(), quality_recovery=QualityRecovery(repair=repair)).run() as run:
        with pytest.raises(QualityViolation):
            run.invoke(lambda: response("bad"))
        assert not run._pending
    async def main():
        def provider(**kwargs):
            async def result():
                return response("{}" if kwargs.get("messages") == "corrected" else "bad")
            return result()
        async with AgentGuard(config(), quality_recovery=QualityRecovery(repair=repair)).run() as run:
            assert (await run.invoke(provider))["content"] == "{}"
            assert run.state.total_tokens == 10 and not run._pending
    asyncio.run(main())


@pytest.mark.parametrize("timeout", [None, 0, True, -1, 301, float("inf")])
def test_handler_timeout_config_validation(timeout):
    with pytest.raises(ConfigurationError):
        AgentGuard(config(quality={"handler_timeout_seconds": timeout}))


@pytest.mark.parametrize("action", ["repair", "human", "degrade"])
def test_stream_overflow_always_blocks_for_recovery_actions(action):
    with AgentGuard(config(action, quality={"max_chars": 4})).run() as run:
        with pytest.raises(QualityViolation):
            list(run.invoke(lambda **kw: iter(["abc", "def"]), stream=True))
        assert run.state.llm_calls == 1
