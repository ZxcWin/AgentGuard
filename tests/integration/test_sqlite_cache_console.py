import json

import pytest
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from agentguard.api.http import create_app


def test_console_persistent_cache_survives_restart_and_clear_is_audited(tmp_path):
    database, cache = tmp_path / "runs.db", tmp_path / "cache.db"
    key = b"separate-cache-signing-key-at-least32"
    for restart in (False, True):
        with TestClient(create_app(database=database, cache_database=cache, cache_signing_key=key, demo=True)) as client:
            health = client.get("/api/v1/health").json()
            assert health["cache_backend"] == "sqlite" and health["cache_scope"] == "shared_host"
            if not restart:
                before = client.get("/api/v1/policies/current").json()
                saved = client.put("/api/v1/policies/current", json={"expected_version": before["version"],
                    "config": {"cache": {"enabled": True, "allowed_models": ["local-example"]}}})
                assert saved.status_code == 200
            created = client.post("/api/v1/demo/runs?scenario=cache").json()
            run = client.get("/api/v1/runs/" + created["run_id"]).json()
            assert run["llm_calls"] == (0 if restart else 1)
            assert run["total_tokens"] == (0 if restart else 12)
            if restart:
                cleared = client.delete("/api/v1/cache").json()
                assert cleared == {"cleared": True, "scope": "shared_host"}
                audit = client.get("/api/v1/audit").json()
                assert any(item.get("action") == "cache.invalidate" for item in audit["items"])
                created = client.post("/api/v1/demo/runs?scenario=cache").json()
                run = client.get("/api/v1/runs/" + created["run_id"]).json()
                assert run["llm_calls"] == 1
            assert key.decode() not in json.dumps(health) + json.dumps(run)


def test_console_cache_requires_separate_database_and_explicit_valid_key(tmp_path):
    for kwargs in ({"cache_database": tmp_path / "cache.db"}, {"cache_signing_key": b"x" * 32},
                   {"cache_database": tmp_path / "cache.db", "cache_signing_key": b"short"}):
        with pytest.raises(ValueError):
            create_app(database=tmp_path / "runs.db", **kwargs)
    for name in ("runs.db", "runs.audit.db", "runs.ledger.db"):
        with pytest.raises(ValueError, match="separate database"):
            create_app(database=tmp_path / "runs.db", cache_database=tmp_path / name, cache_signing_key=b"x" * 32)


def test_console_cache_clear_stops_before_invalidation_when_audit_fails(tmp_path, monkeypatch):
    with TestClient(create_app(database=tmp_path / "runs.db")) as client:
        called = []
        monkeypatch.setattr(client.app.state.guard, "clear_cache", lambda: called.append(1))
        def broken(event):
            raise OSError("PRIVATE disk error")
        monkeypatch.setattr(client.app.state.audit_store, "append", broken)
        response = client.delete("/api/v1/cache")
        assert response.status_code == 503 and "PRIVATE" not in response.text
        assert called == []


def test_console_readonly_tool_cache_policy_persists_and_reuses_after_restart(tmp_path):
    database, cache = tmp_path / "runs.db", tmp_path / "cache.db"
    for restart in (False, True):
        with TestClient(create_app(database=database, demo=True, cache_database=cache,
                                   cache_signing_key=b"tool-demo-cache-key-at-least-32bytes")) as client:
            if not restart:
                original = client.get("/api/v1/policies/current").json()
                changed = client.put("/api/v1/policies/current", json={
                    "expected_version": original["version"], "config": {
                        "cache": {"enabled": True, "allowed_tools": ["catalog_lookup"]},
                        "tools": {"side_effects": {"catalog_lookup": "read"}}}})
                assert changed.status_code == 200
            policy = client.get("/api/v1/policies/current").json()
            assert policy["config"]["cache"]["allowed_tools"] == ["catalog_lookup"]
            created = client.post("/api/v1/demo/runs?scenario=tool-cache")
            assert created.status_code == 200
            run = client.get("/api/v1/runs/" + created.json()["run_id"]).json()
            assert run["status"] == "completed"
            assert run["tool_calls"] == 2 and run["llm_calls"] == run["total_tokens"] == 0
            events = [event["event"] for event in run["events"]]
            assert events.count("cache.tool_hit") == (2 if restart else 1)
            assert events.count("cache.tool_stored") == (0 if restart else 1)
            assert client.app.state.audit_store.stats()["unresolved_operations"] == 0


def test_console_tool_cache_demo_respects_disabled_policy_and_non_demo_mode(tmp_path):
    with TestClient(create_app(database=tmp_path / "demo.db", demo=True)) as client:
        created = client.post("/api/v1/demo/runs?scenario=tool-cache").json()
        run = client.get("/api/v1/runs/" + created["run_id"]).json()
        assert run["tool_calls"] == 2
        assert not any(event["event"] == "cache.tool_hit" for event in run["events"])
        assert not client.get("/api/v1/policies/current").json()["config"]["cache"]["enabled"]
    with TestClient(create_app(database=tmp_path / "regular.db")) as client:
        assert client.post("/api/v1/demo/runs?scenario=tool-cache").status_code == 404
