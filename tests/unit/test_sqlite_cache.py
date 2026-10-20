from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Barrier
from time import sleep

import pytest

from agentguard import AgentGuard, CacheRequest, SQLiteExactCache


CONFIG = {"cache": {"enabled": True, "allowed_models": ["demo"]}}
IDENTITY = dict(tenant_id="tenant", project_id="project", environment="test", user_id="user")
OPTIONS = CacheRequest(namespace="answers", prompt_version="1", authorization_context="reader-1",
                       data_classification="public", pure=True, provider_id="demo-endpoint-v1")
KEY = b"cache-key-32-bytes-must-be-secret!!"


def invoke(guard, provider, options=OPTIONS, identity=None):
    with guard.run(**(identity or IDENTITY)) as run:
        result = run.invoke(provider, model="demo", messages="hello", max_tokens=10, cache_options=options)
    return run, result


def test_restart_and_separate_process_reuse_signed_exact_cache(tmp_path):
    path = tmp_path / "cache.db"
    store = SQLiteExactCache(path)
    guard = AgentGuard(CONFIG, cache_store=store, cache_signing_key=KEY)
    first, _ = invoke(guard, lambda **kw: {"content": "safe answer"})
    assert first.state.llm_calls == 1 and store.stats()["entries"] == 1
    store.close()
    script = '''
import json, sys
from agentguard import AgentGuard, SQLiteExactCache, CacheRequest
store = SQLiteExactCache(sys.argv[1])
guard = AgentGuard(json.loads(sys.argv[2]), cache_store=store, cache_signing_key=sys.argv[3].encode())
options = CacheRequest(namespace="answers", prompt_version="1", authorization_context="reader-1", data_classification="public", pure=True, provider_id="demo-endpoint-v1")
def provider(**kwargs):
    raise AssertionError("Provider must not run for a valid persistent hit")
with guard.run(tenant_id="tenant",project_id="project",environment="test",user_id="user") as run:
    result = run.invoke(provider,model="demo",messages="hello",max_tokens=10,cache_options=options)
print(json.dumps([result,run.state.llm_calls,run.state.total_tokens]))
store.close()
'''
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    child = subprocess.run([sys.executable, "-c", script, str(path), json.dumps(CONFIG), KEY.decode()],
                           env=env, check=True, capture_output=True, text=True, timeout=20)
    assert json.loads(child.stdout) == [{"content": "safe answer"}, 0, 0]


@pytest.mark.parametrize("change", ["key", "provider", "tenant", "authorization", "prompt", "policy"])
def test_shared_cache_identity_isolation(change, tmp_path):
    store = SQLiteExactCache(tmp_path / "cache.db")
    first = AgentGuard(CONFIG, cache_store=store, cache_signing_key=KEY)
    invoke(first, lambda **kw: "answer")
    second = AgentGuard(CONFIG, cache_store=store, cache_signing_key=b"other-secret-signing-key-32-bytes!!" if change == "key" else KEY)
    options = replace(OPTIONS, **{"provider_id": "other"}) if change == "provider" else OPTIONS
    options = replace(options, authorization_context="revoked-2") if change == "authorization" else options
    options = replace(options, prompt_version="2") if change == "prompt" else options
    identity = {**IDENTITY, "tenant_id": "other"} if change == "tenant" else IDENTITY
    if change == "policy":
        second.update_config({"token": {"run_token_budget": 10000}})
    run, _ = invoke(second, lambda **kw: "fresh", options, identity)
    assert run.state.llm_calls == 1
    store.close()


def test_shared_clear_invalidates_every_instance_and_inflight_publish(tmp_path):
    path = tmp_path / "cache.db"
    one, two = SQLiteExactCache(path), SQLiteExactCache(path)
    first, second = (AgentGuard(CONFIG, cache_store=store, cache_signing_key=KEY) for store in (one,two))
    invoke(first, lambda **kw: "old")
    run, answer = invoke(second, lambda **kw: "unused")
    assert answer == "old" and run.state.llm_calls == 0
    first.clear_cache()
    run, answer = invoke(second, lambda **kw: "new")
    assert answer == "new" and run.state.llm_calls == 1
    first.clear_cache()
    def provider(**kwargs):
        second.clear_cache()
        return "in-flight"
    invoke(first, provider)
    assert one.stats()["entries"] == 0
    run, answer = invoke(second, lambda **kw: "after clear")
    assert run.state.llm_calls == 1 and answer == "after clear"
    one.close(); two.close()


def test_capacity_lru_expiry_prune_and_clock_rollback(tmp_path):
    now = [100.0]
    store = SQLiteExactCache(tmp_path / "cache.db", max_entries=2, max_bytes=5, clock=lambda: now[0])
    a,b,c = (f"{value:064x}" for value in range(3))
    store.set(a, b"aa", ttl_seconds=10)
    now[0] += 1
    store.set(b, b"bb", ttl_seconds=10)
    now[0] += 1
    assert store.get(a) == b"aa"
    now[0] += 1
    store.set(c, b"cc", ttl_seconds=1)
    assert store.get(b) is None and store.stats()["bytes"] == 4
    now[0] += 1
    assert store.get(c) is None
    store.set(c, b"too big", ttl_seconds=1)
    assert store.stats()["entries"] == 1
    now[0] = 90
    with pytest.raises(ValueError, match="clock moved backwards"):
        store.get(a)
    now[0] = 200
    assert store.prune() == 1 and store.stats()["entries"] == 0
    store.close()


def test_expired_and_stale_lease_owners_cannot_release_new_lease(tmp_path):
    now = [100.0]
    path = tmp_path / "cache.db"
    first, second = (SQLiteExactCache(path, clock=lambda: now[0]) for _ in range(2))
    key = "a" * 64
    owner = first.claim(key, lease_seconds=1)
    assert owner and second.claim(key) is None
    now[0] = 102
    new_owner = second.claim(key)
    first.release(key, owner)
    assert new_owner and first.claim(key) is None
    second.release(key, new_owner)
    assert first.claim(key)
    first.close(); second.close()


def test_two_guards_coalesce_with_shared_lease_and_capacity_is_atomic(tmp_path):
    path = tmp_path / "cache.db"
    stores = [SQLiteExactCache(path, max_entries=7) for _ in range(4)]
    guards = [AgentGuard(CONFIG, cache_store=store, cache_signing_key=KEY) for store in stores]
    ready, calls = Barrier(4), []
    def provider(**kwargs):
        calls.append(1)
        sleep(.04)
        return "answer"
    def execute(guard):
        ready.wait()
        return invoke(guard, provider)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(execute, guards))
    assert len(calls) == 1 and all(answer == "answer" for _, answer in results)
    assert sum(run.state.llm_calls for run,_ in results) == 1
    def write(item):
        stores[item % 4].set(f"{item:064x}", b"value", ttl_seconds=60)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(40)))
    assert stores[0].stats()["entries"] == 7 and stores[0].stats()["leases"] == 0
    for store in stores:
        store.close()


def test_tamper_rollback_and_locked_backend_become_cache_misses(tmp_path, monkeypatch):
    import agentguard.application.caching as caching
    now = [1000.0]
    monkeypatch.setattr(caching, "time", lambda: now[0])
    path = tmp_path / "cache.db"
    store = SQLiteExactCache(path, clock=lambda: now[0])
    guard = AgentGuard(CONFIG, cache_store=store, cache_signing_key=KEY)
    invoke(guard, lambda **kw: "answer")
    with sqlite3.connect(path) as db:
        db.execute("UPDATE exact_cache SET value=?", (b'{"response":"forged"}',))
    run, answer = invoke(guard, lambda **kw: "fresh")
    assert run.state.llm_calls == 1 and answer == "fresh"
    now[0] -= 1
    run, answer = invoke(guard, lambda **kw: "rollback fresh")
    assert answer == "rollback fresh" and run.state.llm_calls == 1
    now[0] = 2000
    # A writer lock prevents get/claim/set; optional cache cannot block the LLM.
    locked = sqlite3.connect(path)
    locked.execute("BEGIN IMMEDIATE")
    try:
        run, answer = invoke(guard, lambda **kw: "locked fresh")
        assert answer == "locked fresh" and run.state.llm_calls == 1
    finally:
        locked.rollback(); locked.close()
    store.close()


def test_persistent_cache_requires_explicit_stable_secret_and_provider_identity(tmp_path):
    store = SQLiteExactCache(tmp_path / "cache.db")
    for secret in (b"short", "not bytes"):
        with pytest.raises(ValueError):
            AgentGuard(CONFIG, cache_store=store, cache_signing_key=secret)
    with pytest.raises(ValueError):
        AgentGuard(CONFIG, cache_signing_key=KEY)
    # Default per-process identities deliberately miss after Guard replacement.
    for signing_key, options in ((None, OPTIONS), (KEY, replace(OPTIONS, provider_id=None))):
        first = AgentGuard(CONFIG, cache_store=store, cache_signing_key=signing_key)
        second = AgentGuard(CONFIG, cache_store=store, cache_signing_key=signing_key)
        invoke(first, lambda **kw: "answer", options)
        run, answer = invoke(second, lambda **kw: "fresh", options)
        assert run.state.llm_calls == 1 and answer == "fresh"
    store.close()
