from dataclasses import replace
import asyncio
import json

import pytest

from agentguard import (AgentGuard, AgentGuardConfig, AuditUnavailable, CacheRequest, ConfigurationError,
                        ExecutionTimeout, LoopDetected, SecurityGuard, SecurityViolation,
                        SQLiteAuditStore, SQLiteExactCache, ToolApprovalDenied, ToolCacheRequest)


IDENTITY = dict(tenant_id="tenant-a", project_id="project-a", environment="test", user_id="user-a")
OPTIONS = ToolCacheRequest(namespace="catalog", prompt_version="n/a", authorization_context="reader-v3",
                           data_classification="internal", pure=True, tool_version="v1", data_version="2026-10")


def guard(**extra):
    config = {"cache": {"enabled": True, "allowed_tools": ["lookup"]},
              "tools": {"side_effects": {"lookup": "read"}}}
    return AgentGuard(config, **extra)


def test_readonly_tool_cache_hit_still_counts_as_governed_tool_call():
    calls = []
    def lookup(key):
        calls.append(key)
        return {"value": "ok"}
    instance = guard()
    with instance.run(**IDENTITY) as run:
        assert run.invoke_tool("lookup", lookup, "item", cache_options=OPTIONS) == {"value": "ok"}
    with instance.run(**IDENTITY) as run:
        assert run.invoke_tool("lookup", lookup, "item", cache_options=OPTIONS) == {"value": "ok"}
        assert run.state.tool_calls == run.tool_guard.calls == 1
        assert any(event["event"] == "cache.tool_hit" for event in run.state.events)
    assert calls == ["item"]


@pytest.mark.parametrize("field,value", [("tool_version", "v2"), ("data_version", "2026-11"),
                                         ("authorization_context", "reader-v4")])
def test_tool_or_data_authorization_version_invalidates(field, value):
    calls = []
    def lookup(key): calls.append(1); return {"value": "ok"}
    instance = guard()
    with instance.run(**IDENTITY) as run:
        run.invoke_tool("lookup", lookup, "item", cache_options=OPTIONS)
    with instance.run(**IDENTITY) as run:
        run.invoke_tool("lookup", lookup, "item", cache_options=replace(OPTIONS, **{field: value}))
    assert len(calls) == 2


@pytest.mark.parametrize("field,value", [("tenant_id", "tenant-b"), ("project_id", "project-b"),
                                         ("user_id", "user-b")])
def test_cache_isolated_by_full_run_identity(field, value):
    calls = []
    def lookup(): calls.append(1); return {"value": len(calls)}
    instance = guard()
    with instance.run(**IDENTITY) as run:
        assert run.invoke_tool("lookup", lookup, cache_options=OPTIONS) == {"value": 1}
    with instance.run(**(IDENTITY | {field: value})) as run:
        assert run.invoke_tool("lookup", lookup, cache_options=OPTIONS) == {"value": 2}
    assert len(calls) == 2


@pytest.mark.parametrize("error", [AuditUnavailable(), SecurityViolation(stage="response", finding_count=1)])
def test_cache_security_recheck_fails_closed_and_audit_failure_propagates(monkeypatch, error):
    import agentguard.application.guards as guards
    calls = []
    def lookup(): calls.append(1); return {"value": "ok"}
    instance = guard(security_guard=SecurityGuard())
    with instance.run(**IDENTITY) as run:
        run.invoke_tool("lookup", lookup, cache_options=OPTIONS)
    def reject(*args, **kwargs): raise error
    monkeypatch.setattr(guards, "check_security", reject)
    with instance.run(**IDENTITY) as run:
        with pytest.raises(type(error)):
            run.invoke_tool("lookup", lookup, cache_options=OPTIONS)
        assert not run._pending
        assert all(limiter._active == 0 for _, limiter in run.admission._states.values())
    assert calls == [1]
    assert not instance._cache_runtime.store._entries


@pytest.mark.parametrize("enabled,allowed,pure", [(False, ["lookup"], True), (True, [], True),
                                                  (True, ["lookup"], False)])
def test_cache_requires_global_opt_in_tool_allowlist_and_pure_consent(enabled, allowed, pure):
    calls = []
    instance = AgentGuard({"cache": {"enabled": enabled, "allowed_tools": allowed},
                           "tools": {"side_effects": {"lookup": "read"}}})
    with instance.run(**IDENTITY) as run:
        for _ in range(2):
            run.invoke_tool("lookup", lambda: calls.append(1) or "ok",
                            cache_options=replace(OPTIONS, pure=pure))
    assert calls == [1, 1]


@pytest.mark.parametrize("effect", ["write", "irreversible"])
def test_side_effect_tools_never_use_cache(effect):
    calls = []
    def action(): calls.append(1); return {"value": "ok"}
    instance = AgentGuard({"cache": {"enabled": True, "allowed_tools": ["action"]},
                           "tools": {"side_effects": {"action": effect}}})
    with instance.run(**IDENTITY) as run:
        for _ in range(2): run.invoke_tool("action", action, cache_options=OPTIONS)
    assert len(calls) == 2


def test_approval_and_idempotency_are_never_bypassed_by_cache():
    calls = []
    def lookup(): calls.append(1); return {"value": "ok"}
    instance = AgentGuard({"cache": {"enabled": True, "allowed_tools": ["lookup"]},
                           "tools": {"side_effects": {"lookup": "read"},
                                     "approval_required_for": ["lookup"],
                                     "idempotency_required_for": ["lookup"]}})
    with instance.run(**IDENTITY) as run:
        with pytest.raises(ToolApprovalDenied):
            run.invoke_tool("lookup", lookup, cache_options=OPTIONS, idempotency_key="1")
        run.invoke_tool("lookup", lookup, cache_options=OPTIONS, approval=lambda: True, idempotency_key="1")
        with pytest.raises(LoopDetected):
            run.invoke_tool("lookup", lookup, cache_options=OPTIONS, approval=lambda: True, idempotency_key="1")
    assert calls == [1]


def test_sensitive_arguments_and_results_are_not_stored():
    instance = guard()
    with instance.run(**IDENTITY) as run:
        run.invoke_tool("lookup", lambda key: {"value": "ok"}, "person@example.com", cache_options=OPTIONS)
        run.invoke_tool("lookup", lambda key: {"value": "a@example.com"}, "safe", cache_options=OPTIONS)
    assert not instance._cache_runtime.store._entries
    assert any(event.get("reason_code") == "unsafe_result" for event in run.state.events)


def test_only_explicit_tool_cache_request_is_accepted():
    instance = guard()
    with instance.run(**IDENTITY) as run:
        run.invoke_tool("lookup", lambda: {"value": "ok"}, cache_options=CacheRequest(
            "x", "v1", "reader-v1", "internal", pure=True))
    assert not instance._cache_runtime.store._entries


def test_tool_quota_and_durable_audit_still_apply_on_cache_hits(tmp_path):
    audit = SQLiteAuditStore(tmp_path / "audit.db")
    instance = AgentGuard({"cache": {"enabled": True, "allowed_tools": ["lookup"]},
                           "tools": {"side_effects": {"lookup": "read"},
                                     "per_tool_limits": {"lookup": 1}}}, audit_store=audit)
    calls = []
    def lookup(): calls.append(1); return {"value": "ok"}
    with instance.run(**IDENTITY) as run:
        run.invoke_tool("lookup", lookup, cache_options=OPTIONS)
    with instance.run(**IDENTITY) as run:
        run.invoke_tool("lookup", lookup, cache_options=OPTIONS)
        with pytest.raises(LoopDetected):
            run.invoke_tool("lookup", lookup, cache_options=OPTIONS)
    assert calls == [1]
    assert audit.stats()["unresolved_operations"] == 0
    assert audit.stats()["records"] == 4
    audit.close()


def test_readonly_tool_sqlite_cache_survives_restart(tmp_path):
    from agentguard import AgentGuard
    key = b"tool-cache-key-32-bytes-for-tests!!"
    config = {"cache": {"enabled": True, "allowed_tools": ["lookup"]},
              "tools": {"side_effects": {"lookup": "read"}}}
    store = SQLiteExactCache(tmp_path / "cache.db")
    first = AgentGuard(config, cache_store=store, cache_signing_key=key)
    with first.run(**IDENTITY) as run:
        assert run.invoke_tool("lookup", lambda key: {"value": key}, "item",
                               cache_options=replace(OPTIONS, provider_id="catalog-v1")) == {"value": "item"}
    store.close()
    reopened = SQLiteExactCache(tmp_path / "cache.db")
    second = AgentGuard(config, cache_store=reopened, cache_signing_key=key)
    with second.run(**IDENTITY) as run:
        assert run.invoke_tool("lookup", lambda key: pytest.fail("provider must not execute"), "item",
                               cache_options=replace(OPTIONS, provider_id="catalog-v1")) == {"value": "item"}
        assert any(event["event"] == "cache.tool_hit" for event in run.state.events)
    reopened.close()


def test_async_cached_hit_is_awaitable_and_not_eager():
    async def scenario():
        calls = []
        async def lookup(key): calls.append(key); return [key]
        instance = guard()
        async with instance.run(**IDENTITY) as run:
            await run.invoke_tool("lookup", lookup, "item", cache_options=OPTIONS)
        async with instance.run(**IDENTITY) as run:
            pending = run.invoke_tool("lookup", lookup, "item", cache_options=OPTIONS)
            assert run.state.tool_calls == 0
            assert await pending == ["item"]
            assert run.state.tool_calls == 1
            assert not run._pending
        assert calls == ["item"]
        assert all(concurrency._active == 0 for _, concurrency in run.admission._states.values())
    asyncio.run(scenario())


@pytest.mark.parametrize("result", [None, False, 0, [], {"value": "ok"}])
def test_json_shape_preserved_and_results_do_not_share_references(result):
    calls = []
    def lookup(): calls.append(1); return result
    instance = guard()
    with instance.run(**IDENTITY) as run:
        first = run.invoke_tool("lookup", lookup, cache_options=OPTIONS)
        if type(first) is dict: first["value"] = "changed"
        second = run.invoke_tool("lookup", lookup, cache_options=OPTIONS)
        assert second == ({"value": "ok"} if type(result) is dict else result)
    assert calls == [1]


def test_tampered_envelope_is_not_returned():
    calls = []
    def lookup(): calls.append(1); return {"value": "ok"}
    instance = guard()
    with instance.run(**IDENTITY) as run:
        run.invoke_tool("lookup", lookup, cache_options=OPTIONS)
    store = instance._cache_runtime.store
    key = next(iter(store._entries))
    envelope = json.loads(store.get(key))
    envelope["response"] = json.dumps({"value": "attacker"})
    store.set(key, json.dumps(envelope).encode(), ttl_seconds=300)
    with instance.run(**IDENTITY) as run:
        assert run.invoke_tool("lookup", lookup, cache_options=OPTIONS) == {"value": "ok"}
    assert calls == [1, 1]


def test_nested_tool_activity_and_failures_are_not_cached():
    instance = guard()
    with instance.run(**IDENTITY) as run:
        def lookup():
            run.invoke_tool("child", lambda: "ok")
            return {"value": "ok"}
        run.invoke_tool("lookup", lookup, cache_options=OPTIONS)
        def fails(): raise RuntimeError("private")
        with pytest.raises(RuntimeError): run.invoke_tool("lookup", fails, cache_options=OPTIONS)
    assert not instance._cache_runtime.store._entries


def test_clear_during_tool_execution_cannot_repopulate_cache():
    instance = guard()
    def lookup(): instance.clear_cache(); return {"value": "ok"}
    with instance.run(**IDENTITY) as run:
        run.invoke_tool("lookup", lookup, cache_options=OPTIONS)
    assert not instance._cache_runtime.store._entries


def test_cache_backend_failure_falls_back_to_normal_tool_call():
    class Broken:
        def get(self, key): raise OSError("private")
        def set(self, *args, **kwargs): raise OSError("private")
    instance = guard(cache_store=Broken())
    with instance.run(**IDENTITY) as run:
        assert run.invoke_tool("lookup", lambda: "ok", cache_options=OPTIONS) == "ok"
    assert "private" not in repr(run.state.events)


def test_expiry_is_not_extended_by_hits(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("agentguard.application.caching.monotonic", lambda: now[0])
    instance, calls = guard(), []
    def lookup(): calls.append(1); return "ok"
    for delta in (0, 200, 101):
        now[0] += delta
        with instance.run(**IDENTITY) as run:
            assert run.invoke_tool("lookup", lookup, cache_options=OPTIONS) == "ok"
    assert calls == [1, 1]


@pytest.mark.parametrize("classification", ["confidential", "restricted"])
def test_confidential_and_restricted_data_never_cached(classification):
    instance = guard()
    with instance.run(**IDENTITY) as run:
        run.invoke_tool("lookup", lambda: "ok",
                        cache_options=replace(OPTIONS, data_classification=classification))
    assert not instance._cache_runtime.store._entries


def test_missing_identity_and_explicit_bypass_do_not_reuse_cache():
    calls = []
    def lookup(): calls.append(1); return "ok"
    instance = guard()
    with instance.run(**IDENTITY) as run:
        run.invoke_tool("lookup", lookup, cache_options=OPTIONS)
    for identity, options in [(IDENTITY, replace(OPTIONS, bypass=True)), ({}, OPTIONS)]:
        with instance.run(**identity) as run:
            run.invoke_tool("lookup", lookup, cache_options=options)
            assert not any(e["event"] == "cache.tool_hit" for e in run.state.events)
    assert len(calls) == 3


def test_failed_result_audit_never_publishes_cache():
    class FailResult:
        def append(self, event):
            if event["kind"] == "result": raise OSError("disk full")
    instance = guard(audit_store=FailResult())
    with instance.run(**IDENTITY) as run:
        with pytest.raises(AuditUnavailable):
            run.invoke_tool("lookup", lambda: "ok", cache_options=OPTIONS)
        assert not run._pending
        assert all(limiter._active == 0 for _, limiter in run.admission._states.values())
        with pytest.raises(AuditUnavailable):
            run.invoke_tool("lookup", lambda: pytest.fail("audit latch must block"), cache_options=OPTIONS)
    assert not instance._cache_runtime.store._entries


@pytest.mark.parametrize("cancelled", [True, False])
def test_cancelled_or_timed_out_async_tool_never_publishes(cancelled):
    async def scenario():
        started, cleaned = asyncio.Event(), []
        async def lookup():
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                cleaned.append(True)
        instance = AgentGuard({"cache": {"enabled": True, "allowed_tools": ["lookup"]},
                               "tools": {"side_effects": {"lookup": "read"}},
                               "timeout": {"tool_seconds": 0.1}})
        async with instance.run(**IDENTITY) as run:
            task = asyncio.create_task(run.invoke_tool("lookup", lookup, cache_options=OPTIONS))
            try:
                await asyncio.wait_for(started.wait(), 2)
            except BaseException:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
            if cancelled: task.cancel()
            with pytest.raises(asyncio.CancelledError if cancelled else ExecutionTimeout): await task
            assert cleaned == [True]
            assert not run._pending
            assert all(limiter._active == 0 for _, limiter in run.admission._states.values())
        assert not instance._cache_runtime.store._entries
    asyncio.run(scenario())


def test_policy_expiry_after_cache_read_cannot_return_cached_value(monkeypatch):
    calls = []
    def lookup(): calls.append(1); return "ok"
    instance = guard()
    with instance.run(**IDENTITY) as run:
        run.invoke_tool("lookup", lookup, cache_options=OPTIONS)
    store, expired = instance._cache_runtime.store, [False]
    original_get = store.get
    def read_and_expire(key):
        result = original_get(key)
        expired[0] = True
        return result
    monkeypatch.setattr(store, "get", read_and_expire)
    with instance.run(**IDENTITY) as run:
        def validate():
            if expired[0]: raise ConfigurationError("Policy expired")
        run.policy_valid = validate
        with pytest.raises(ConfigurationError):
            run.invoke_tool("lookup", lookup, cache_options=OPTIONS)
        assert all(limiter._active == 0 for _, limiter in run.admission._states.values())
    assert calls == [1]


def test_tool_cache_versions_and_whitelist_are_validated():
    assert AgentGuardConfig.from_mapping({}).cache.allowed_tools == ()
    with pytest.raises(ConfigurationError):
        AgentGuardConfig.from_mapping({"cache": {"allowed_tools": ["*"]}})
    for field in ("tool_version", "data_version"):
        for value in ("", " ", None, "x" * 257):
            with pytest.raises(ValueError): replace(OPTIONS, **{field: value})


def test_new_observed_security_finding_invalidates_cached_tool_result():
    from agentguard.security import SecurityFinding, SecurityResult
    class MutableGuard(SecurityGuard):
        flagged = False
        def inspect_response(self, response, *, stage="response"):
            if self.flagged:
                return SecurityResult(False, (SecurityFinding("custom.rule"),), "observe", stage)
            return super().inspect_response(response, stage=stage)
    security, calls = MutableGuard(), []
    instance = guard(security_guard=security)
    def lookup(): calls.append(1); return {"value": "ok"}
    with instance.run(**IDENTITY) as run:
        run.invoke_tool("lookup", lookup, cache_options=OPTIONS)
    security.flagged = True
    with instance.run(**IDENTITY) as run:
        assert run.invoke_tool("lookup", lookup, cache_options=OPTIONS) == {"value": "ok"}
        assert not any(e["event"] == "cache.tool_hit" for e in run.state.events)
        assert any(e.get("reason_code") == "current_guard_finding" for e in run.state.events)
    assert calls == [1, 1]
    assert not instance._cache_runtime.store._entries
