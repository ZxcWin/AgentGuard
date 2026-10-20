import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from threading import Barrier
from time import sleep

import pytest

from agentguard import (AgentGuard, AgentGuardConfig, CacheRequest, ConfigurationError, ExecutionTimeout,
                        MemoryExactCache, QualityAction, QualityGuard, QualityResult, QualityViolation)


OPTIONS = CacheRequest(namespace="provider-v1", prompt_version="v1", authorization_context="reader-v1",
                       data_classification="internal", pure=True)
IDENTITY = dict(tenant_id="tenant-a", project_id="project-a", environment="test", user_id="user-a")


def guard(**kwargs):
    return AgentGuard({"cache": {"enabled": True, "allowed_models": ["demo"]},
                       "pricing": {"models": {"demo": {"input_per_million": 1, "output_per_million": 2}}}}, **kwargs)


def invoke(run, provider, **kwargs):
    return run.invoke(provider, model="demo", messages="hello", max_tokens=20, cache_options=OPTIONS, **kwargs)


def response():
    return {"content": "safe answer", "usage": {"input_tokens": 2, "output_tokens": 3}}


def events(run, name):
    return [e for e in run.state.events if e["event"] == "cache." + name]


def test_hit_preserves_shape_without_provider_admission_or_double_billing():
    calls = []
    def provider(**kw):
        calls.append(kw)
        return response()
    instance = guard()
    with instance.run(**IDENTITY) as first:
        result = invoke(first, provider)
        result["content"] = "caller mutation"
    with instance.run(**IDENTITY) as second:
        assert invoke(second, provider) == response()
        assert not second.admission._states
    assert len(calls) == 1 and "cache_options" not in calls[0]
    assert first.state.total_tokens == 5 and first.state.estimated_cost_usd == .000008
    assert second.state.total_tokens == second.state.llm_calls == second.state.estimated_cost_usd == 0
    assert events(first, "miss") and events(first, "stored") and events(second, "hit")
    assert not instance._cache_runtime.owners


@pytest.mark.parametrize("identity", [{**IDENTITY, key: "other"} for key in IDENTITY])
def test_scope_isolation(identity):
    instance, calls = guard(), []
    def provider(**kw): calls.append(1); return response()
    with instance.run(**IDENTITY) as run: invoke(run, provider)
    with instance.run(**identity) as run: invoke(run, provider)
    assert len(calls) == 2


@pytest.mark.parametrize("field,value", [("authorization_context", "revoked-v2"), ("prompt_version", "v2"),
                                        ("namespace", "provider-v2"), ("data_classification", "public")])
def test_request_version_and_authorization_isolation(field, value):
    instance, calls = guard(), []
    def provider(**kw): calls.append(1); return response()
    with instance.run(**IDENTITY) as run:
        invoke(run, provider)
        run.invoke(provider, model="demo", messages="hello", max_tokens=20, cache_options=replace(OPTIONS, **{field: value}))
    assert len(calls) == 2


def test_policy_version_change_clear_and_provider_identity_invalidate():
    instance, calls = guard(), []
    def provider(**kw): calls.append(1); return response()
    def other(**kw): calls.append(1); return response()
    with instance.run(**IDENTITY) as run: invoke(run, provider)
    instance.update_config({"token": {"run_token_budget": 10000}})
    with instance.run(**IDENTITY) as run:
        invoke(run, provider)
        invoke(run, other)
    instance.clear_cache()
    with instance.run(**IDENTITY) as run: invoke(run, provider)
    assert len(calls) == 4


def test_default_disabled_and_unlisted_models_do_not_cache():
    for settings in ({}, {"cache": {"enabled": True}}):
        instance, calls = AgentGuard(settings), []
        def provider(**kw): calls.append(1); return response()
        with instance.run(**IDENTITY) as run:
            for _ in range(2): invoke(run, provider)
        assert len(calls) == 2 and not instance._cache_runtime.store._entries


def test_expiry_is_enforced_even_if_store_does_not_expire(monkeypatch):
    import agentguard.application.caching as caching
    now = [100]
    monkeypatch.setattr(caching, "monotonic", lambda: now[0])
    class Store:
        entries = {}
        def get(self, key): return self.entries.get(key)
        def set(self, key, value, **kwargs): self.entries[key] = value
        def delete(self, key): self.entries.pop(key, None)
    instance, calls = guard(cache_store=Store()), []
    def provider(**kw): calls.append(1); return response()
    with instance.run(**IDENTITY) as run:
        invoke(run, provider)
        now[0] += 301
        invoke(run, provider)
    assert len(calls) == 2


def test_clear_during_provider_cannot_repopulate_cache():
    instance = guard()
    def provider(**kw): instance.clear_cache(); return response()
    with instance.run(**IDENTITY) as run: invoke(run, provider)
    assert not instance._cache_runtime.store._entries and not instance._cache_runtime.owners


@pytest.mark.parametrize("options", [None, replace(OPTIONS, pure=False), replace(OPTIONS, bypass=True),
                                    replace(OPTIONS, data_classification="confidential"),
                                    replace(OPTIONS, data_classification="restricted")])
def test_explicit_eligibility_required(options):
    instance, calls = guard(), []
    def provider(**kw): calls.append(1); return response()
    with instance.run(**IDENTITY) as run:
        for _ in range(2):
            run.invoke(provider, model="demo", messages="hello", cache_options=options)
    assert len(calls) == 2


@pytest.mark.parametrize("messages", ["a@example.com", "api_key=1234567890123", "sk-" + "a" * 25,
                                      {"access_token": "short"}, {"accessToken": "short"},
                                      {"role": "tool", "content": "safe"}, ["sk-", "a" * 25],
                                      {"data_classification": "confidential", "content": "safe"}])
def test_sensitive_inputs_never_cached_even_with_runtime_security_disabled(messages):
    instance, calls = guard(), []
    def provider(**kw): calls.append(1); return response()
    with instance.run(**IDENTITY) as run:
        for _ in range(2):
            run.invoke(provider, model="demo", messages=messages, cache_options=OPTIONS)
    assert len(calls) == 2 and events(run, "bypassed")
    assert not instance._cache_runtime.store._entries


@pytest.mark.parametrize("result", ["a@example.com", {"content": "sk-" + "b" * 25},
    {"access_token": "short"}, {"error": "provider error"}, {"status": "in_progress"},
    {"choices": [{"finish_reason": "length", "message": {"content": "truncated"}}]},
    {"tool_calls": []}, {"content": object()}, {"content": "a" * 200001}])
def test_prohibited_incomplete_or_unsupported_outputs_not_cached(result):
    instance, calls = guard(), []
    def provider(**kw): calls.append(1); return result
    with instance.run(**IDENTITY) as run:
        for _ in range(2): invoke(run, provider)
    assert len(calls) == 2 and not instance._cache_runtime.store._entries
    assert not instance._cache_runtime.owners


def test_stream_tool_requests_and_missing_identity_bypass():
    instance, calls = guard(), []
    def provider(**kw): calls.append(1); return response()
    with instance.run(**IDENTITY) as run:
        for _ in range(2): invoke(run, provider, tools=[])
    with instance.run() as run:
        for _ in range(2): invoke(run, provider)
    def stream(**kw): calls.append(1); return iter(["safe"])
    with instance.run(**IDENTITY) as run:
        for _ in range(2): assert list(invoke(run, stream, stream=True)) == ["safe"]
    assert len(calls) == 6 and not instance._cache_runtime.owners


def test_nested_tool_execution_prevents_parent_caching():
    instance, calls = guard(), []
    with instance.run(**IDENTITY) as run:
        def provider(**kw):
            calls.append(1)
            run.invoke_tool("write", lambda: None)
            return response()
        for _ in range(2): invoke(run, provider)
    assert len(calls) == run.state.tool_calls == 2
    assert not instance._cache_runtime.store._entries


def test_quality_observe_failure_prevents_storage_and_hit_rechecks_current_evaluator():
    quality = QualityGuard()
    instance, calls = guard(quality_guard=quality), []
    def provider(**kw): calls.append(1); return response()
    with instance.run(**IDENTITY) as run: invoke(run, provider)
    quality.add_evaluator(lambda value: QualityResult(False, action=QualityAction.BLOCK))
    with instance.run(**IDENTITY) as run:
        with pytest.raises(QualityViolation): invoke(run, provider)
    assert len(calls) == 1 and not instance._cache_runtime.store._entries
    observer = QualityGuard()
    observer.add_evaluator(lambda value: QualityResult(False, action=QualityAction.OBSERVE))
    instance = guard(quality_guard=observer)
    with instance.run(**IDENTITY) as run:
        for _ in range(2): invoke(run, provider)
    assert len(calls) == 3 and not instance._cache_runtime.store._entries


def test_hit_quality_observation_falls_back_to_provider():
    quality = QualityGuard()
    instance, calls = guard(quality_guard=quality), []
    def provider(**kw): calls.append(1); return response()
    with instance.run(**IDENTITY) as run: invoke(run, provider)
    quality.add_evaluator(lambda value: QualityResult(False, action=QualityAction.OBSERVE))
    with instance.run(**IDENTITY) as run: invoke(run, provider)
    assert len(calls) == 2 and not instance._cache_runtime.store._entries


def test_interceptors_and_retry_calls_bypass_cache():
    from agentguard import BaseInterceptor
    instance, calls = guard(), []
    def provider(**kw): calls.append(1); return response()
    with instance.run(**IDENTITY) as run:
        run.add_interceptor(BaseInterceptor())
        for _ in range(2): invoke(run, provider)
    instance.update_config({"retry": {"max_retries": 1}})
    with instance.run(**IDENTITY) as run:
        for _ in range(2): invoke(run, provider)
    assert len(calls) == 4 and not instance._cache_runtime.store._entries


def test_unawaited_sync_coroutine_provider_releases_owner_on_run_exit():
    instance, calls = guard(), []
    def provider(**kw):
        async def result(): calls.append(1); return response()
        return result()
    with instance.run(**IDENTITY) as run: invoke(run, provider)
    assert not calls and not instance._cache_runtime.owners
    assert not instance._cache_runtime.store._entries


def test_decorated_async_calls_inherit_cache_options_and_identity():
    async def main():
        instance, calls = guard(), []
        @instance.guard
        async def provider(**kw):
            calls.append(1)
            return response()
        # Decorated calls inherit an explicit identity-bearing Run.
        async with instance.run(**IDENTITY) as run:
            first = await provider(model="demo", messages="hello", cache_options=OPTIONS)
            second = await provider(model="demo", messages="hello", cache_options=OPTIONS)
            assert first == second == response()
            assert events(run, "hit")
        assert len(calls) == 1 and not instance._cache_runtime.owners
    asyncio.run(main())


def test_bounded_coalescing_timeout_uses_normal_admission(monkeypatch):
    import agentguard.application.caching as caching
    instance = guard()
    def provider(**kw): return response()
    with instance.run(**IDENTITY) as run:
        from agentguard.application.runtime import _prepare_call
        prepared = _prepare_call(run, provider, (), {"model": "demo", "messages": "hello", "max_tokens": 20})
        ticket = caching.prepare_cache(run, prepared, OPTIONS)
        owner = instance._cache_runtime.claim(ticket.key)
        now = [0]
        monkeypatch.setattr(caching, "monotonic", lambda: now[0])
        monkeypatch.setattr(caching, "sleep", lambda seconds: now.__setitem__(0, now[0] + .6))
        assert invoke(run, provider) == response()
        assert events(run, "bypassed")[-1]["reason_code"] == "coalescing_timeout"
        assert not instance._cache_runtime.store._entries
        instance._cache_runtime.release(ticket.key, owner)


def test_provider_failure_and_admission_rejection_release_owner():
    instance = guard()
    def provider(**kw): raise RuntimeError("failed")
    with instance.run(**IDENTITY) as run:
        with pytest.raises(RuntimeError): invoke(run, provider)
    assert not instance._cache_runtime.owners
    instance.update_config({"token": {"run_token_budget": 0}})
    with instance.run(**IDENTITY) as run:
        from agentguard import BudgetExceeded
        with pytest.raises(BudgetExceeded): invoke(run, provider)
    assert not instance._cache_runtime.owners


def test_cache_backend_failure_is_miss_with_sanitized_events():
    class Broken:
        def get(self, key): raise OSError("private backend address")
        def set(self, *args, **kwargs): raise OSError("private backend address")
        def delete(self, key): raise OSError("private backend address")
    instance, calls = guard(cache_store=Broken()), []
    def provider(**kw): calls.append(1); return response()
    with instance.run(**IDENTITY) as run:
        for _ in range(2): invoke(run, provider)
    assert len(calls) == 2 and events(run, "error")
    assert "private backend" not in json.dumps(run.state.events)
    assert not instance._cache_runtime.owners


def test_corrupt_signed_entry_is_rejected():
    instance, calls = guard(), []
    def provider(**kw): calls.append(1); return response()
    with instance.run(**IDENTITY) as run: invoke(run, provider)
    store = instance._cache_runtime.store
    key = next(iter(store._entries))
    assert len(key) == 64 and "hello" not in key
    store.set(key, b'{"response":"stolen","signature":"bad","expires":999999999999}', ttl_seconds=300)
    with instance.run(**IDENTITY) as run: invoke(run, provider)
    assert len(calls) == 2 and events(run, "error")


def test_memory_expiry_lru_and_byte_bound():
    now = [0]
    store = MemoryExactCache(max_entries=2, max_bytes=6, clock=lambda: now[0])
    store.set("a", b"aaa", ttl_seconds=1)
    store.set("b", b"bbb", ttl_seconds=10)
    assert store.get("a") == b"aaa"
    store.set("c", b"cc", ttl_seconds=10)
    assert store.get("b") is None
    now[0] = 1
    assert store.get("a") is None and store.get("c") == b"cc"
    store.set("large", b"1234567", ttl_seconds=10)
    assert store.get("large") is None
    store.clear()
    assert store._bytes == 0


def test_concurrent_sync_requests_coalesce():
    instance, calls = guard(), []
    start = Barrier(4)
    def provider(**kw): calls.append(1); sleep(.06); return response()
    def worker(_):
        with instance.run(**IDENTITY) as run:
            start.wait()
            return invoke(run, provider)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(worker, range(4)))
    assert results == [response()] * 4 and len(calls) == 1
    assert not instance._cache_runtime.owners


def test_async_coalescing_cancelled_waiter_and_leader_cleanup():
    async def main():
        instance, calls = guard(), []
        started, finish = asyncio.Event(), asyncio.Event()
        async def provider(**kw):
            calls.append(1); started.set(); await finish.wait(); return response()
        async with instance.run(**IDENTITY) as run:
            leader = asyncio.ensure_future(invoke(run, provider))
            await started.wait()
            waiter = asyncio.ensure_future(invoke(run, provider))
            await asyncio.sleep(.02)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError): await waiter
            assert instance._cache_runtime.owners
            finish.set()
            assert await leader == response()
            assert await invoke(run, provider) == response()
        assert len(calls) == 1 and not instance._cache_runtime.owners
        instance.clear_cache(); finish.clear(); started.clear()
        async with instance.run(**IDENTITY) as run:
            leader = asyncio.ensure_future(invoke(run, provider))
            await started.wait(); leader.cancel()
            with pytest.raises(asyncio.CancelledError): await leader
        assert not instance._cache_runtime.owners and not instance._cache_runtime.store._entries
    asyncio.run(main())


def test_waiter_deadline_is_respected_without_cancelling_leader():
    async def main():
        instance = guard()
        started, finish = asyncio.Event(), asyncio.Event()
        async def provider(**kw): started.set(); await finish.wait(); return response()
        async with instance.run(**IDENTITY) as run:
            leader = asyncio.ensure_future(invoke(run, provider)); await started.wait()
            run.deadline = run.deadline.child(.02, "run")
            with pytest.raises(ExecutionTimeout): await invoke(run, provider)
            run.deadline = type(run.deadline)()
            finish.set(); await leader
        assert not instance._cache_runtime.owners
    asyncio.run(main())


@pytest.mark.parametrize("settings", [{"ttl_seconds": 0}, {"ttl_seconds": 3601}, {"ttl_seconds": True},
    {"max_entries": 0}, {"max_bytes": -1}, {"enabled": "true"}, {"allowed_models": ["*"]}])
def test_invalid_cache_config_rejected(settings):
    with pytest.raises(ConfigurationError): AgentGuardConfig.from_mapping({"cache": settings})
