from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from agentguard.api.http import create_app
from agentguard.config.manager import ConfigManager, ConfigVersionConflict


@pytest.fixture
def client(tmp_path):
    app = create_app(database=tmp_path / "runs.db")
    with TestClient(app) as http:
        yield http


def test_sdk_to_sqlite_to_http_and_no_payload_leaks(client):
    guard = client.app.state.guard
    with guard.run("support") as run:
        run.invoke(lambda **kwargs: {"usage": {"input_tokens": 10, "output_tokens": 5}},
                   model="local-example", messages="secret-prompt-should-not-be-stored")
        run.state.record("custom", prompt="secret-prompt", api_key="sk-secret", arguments={"password": "secret"})
    rows = client.get("/api/v1/runs").json()
    assert rows["total"] == 1
    detail = client.get("/api/v1/runs/" + run.state.run_id).json()
    assert detail["status"] == "completed"
    assert detail["total_tokens"] == 15
    assert detail["usage_source"] == "provider"
    assert all("secret" not in str(event) for event in detail["events"])
    assert len({event["id"] for event in detail["events"]}) == len(detail["events"])
    overview = client.get("/api/v1/overview").json()
    assert overview["calls"] == 1
    assert overview["tokens"] == 15
    assert Decimal(overview["estimated_cost_usd"]) > 0


def test_independent_agent_process_uses_shared_database_and_policy(tmp_path):
    from examples.console_agent import run_agent
    database = tmp_path / 'shared' / 'runs.db'
    with TestClient(create_app(database=database)) as console:
        initial = console.get('/api/v1/policies/current').json()
        policy = {'expected_version': initial['version'], 'config': {
            'token': {'run_token_budget': 1000},
            'tools': {'max_calls': 2, 'side_effects': {'knowledge_lookup': 'read'}},
        }}
        assert console.put('/api/v1/policies/current', json=policy).status_code == 200

        result = run_agent(database, 'check cross-process monitoring')
        rows = console.get('/api/v1/runs', params={'search': result['run_id']}).json()
        assert rows['total'] == 1
        assert rows['items'][0]['run_id'] == result['run_id']
        assert rows['items'][0]['config_version'] == initial['version'] + 1
        assert rows['items'][0]['llm_calls'] == 2
        assert rows['items'][0]['tool_calls'] == 1
        detail = console.get(f"/api/v1/runs/{result['run_id']}").json()
        assert detail['status'] == 'completed'
        assert {'llm.completed', 'tool.completed', 'run.completed'} <= {event['event'] for event in detail['events']}
        assert all('check cross-process monitoring' not in str(event) for event in detail['events'])
        overview = console.get('/api/v1/overview').json()
        assert overview['runs'] == 1 and overview['calls'] == 2 and overview['tokens'] > 0


def test_policy_validate_update_conflict_and_run_snapshots(client):
    guard = client.app.state.guard
    old_run = guard.run("old")
    before = client.get("/api/v1/policies/current").json()
    change = {"config": {"token": {"run_token_budget": 500}}, "expected_version": before["version"]}
    assert client.post("/api/v1/policies/validate", json=change).json()["valid"] is True
    assert guard.snapshot()[1] == 1
    assert client.put("/api/v1/policies/current", json=change).json()["version"] == 2
    assert client.put("/api/v1/policies/current", json=change).status_code == 409
    assert old_run.config.token.run_token_budget == 32000
    assert guard.run("new").config.token.run_token_budget == 500


def test_circuit_policy_real_demo_stops_before_second_provider_call(tmp_path):
    with TestClient(create_app(database=tmp_path / 'circuit.db', demo=True)) as client:
        guard = client.app.state.guard
        old = guard.run('old-policy')
        policy = client.get('/api/v1/policies/current').json()
        change = {'expected_version': policy['version'], 'config': {
            'circuit': {'failure_threshold': 1, 'recovery_seconds': 60, 'max_keys': 100},
            'retry': {'max_retries': 0}}}
        assert client.post('/api/v1/policies/validate', json=change).status_code == 200
        assert guard.snapshot()[1] == policy['version']
        saved = client.put('/api/v1/policies/current', json=change)
        assert saved.status_code == 200
        assert old.circuits.failure_threshold == 5
        assert guard.run().circuits.failure_threshold == 1
        assert client.put('/api/v1/policies/current', json=change).status_code == 409
        generated = client.post('/api/v1/demo/runs?scenario=circuit')
        assert generated.status_code == 200
        run = client.get('/api/v1/runs/' + generated.json()['run_id']).json()
        assert run['status'] == 'failed' and run['llm_calls'] == 1
        assert any(e['event'] == 'circuit.rejected' for e in run['events'])
        assert run['config_version'] == saved.json()['version']
        assert 'Local simulated provider failure' not in str(run)


def test_security_quality_policy_enforcement_and_metadata(client):
    from agentguard import SecurityViolation, QualityViolation
    saved = client.put('/api/v1/policies/current', json={'expected_version': 1, 'config': {
        'security': {'enabled': True}, 'quality': {'enabled': True, 'strict_json': True}}})
    assert saved.status_code == 200
    guard = client.app.state.guard
    with pytest.raises(SecurityViolation), guard.run('security') as run:
        run.invoke(lambda **kw: 'never', messages='api_key=local-placeholder-secret')
    row = client.get('/api/v1/runs/' + run.state.run_id).json()
    assert row['blocked'] == 1 and row['llm_calls'] == 0 and row['status'] == 'failed'
    assert 'local-placeholder-secret' not in str(row)
    assert any(e['event'] == 'security.finding' and e['reason_code'] == 'secret.assignment' and e['stage'] == 'request' for e in row['events'])
    with pytest.raises(QualityViolation), guard.run('quality') as run:
        run.invoke(lambda **kw: {'content': 'private bad content', 'usage': {'input_tokens': 2, 'output_tokens': 3}}, model='local-example')
    row = client.get('/api/v1/runs/' + run.state.run_id).json()
    assert row['blocked'] == 1 and row['total_tokens'] == 5 and row['status'] == 'failed'
    assert 'private bad content' not in str(row)
    assert any(e['event'] == 'quality.rejected' and e['action'] == 'block' and e['evaluator_version'] == 'builtin-1' for e in row['events'])


def test_tool_safety_policy_enforcement_audit_and_http_redaction(client):
    from agentguard import LoopDetected, ToolApprovalDenied
    change = {"expected_version": 1, "config": {"tools": {
        "idempotency_required_for": ["send"], "approval_required_for": ["send"],
        "approval_timeout_seconds": 1, "side_effects": {"send": "irreversible"},
    }}}
    saved = client.put("/api/v1/policies/current", json=change)
    assert saved.status_code == 200
    assert saved.json()["config"]["tools"]["approval_required_for"] == ["send"]
    effects = []
    with client.app.state.guard.run("safe-tool") as run:
        with pytest.raises(LoopDetected):
            run.invoke_tool("send", lambda: effects.append(1))
        with pytest.raises(ToolApprovalDenied):
            run.invoke_tool("send", lambda: effects.append(1), idempotency_key="private-key")
        run.invoke_tool("send", lambda payload: effects.append(payload), "private-payload",
                        idempotency_key="private-key", approval=lambda: True)
        with pytest.raises(LoopDetected):
            run.invoke_tool("send", lambda: effects.append(1), idempotency_key="private-key", approval=lambda: True)
    assert effects == ["private-payload"]
    detail = client.get("/api/v1/runs/" + run.state.run_id).json()
    assert detail["config_version"] == 2 and detail["blocked"] == 3
    assert "private-key" not in str(detail) and "private-payload" not in str(detail)
    events = detail["events"]
    assert {e["approval_outcome"] for e in events if e["event"] == "tool.approval_resolved"} == {"denied", "approved"}
    assert any(e["event"] == "tool.completed" and len(e["idempotency_key_hash"]) == 64 for e in events)
    assert client.get("/api/v1/overview").json()["blocked"] == 3


@pytest.mark.parametrize("config", [
    {"token": {"run_token_budget": -1}}, {"token": {"run_token_budget": True}},
    {"token": {"reservation_buffer": 4}}, {"rate_limit": {"redis_url": "redis://secret"}},
    {"telemetry": {"verbose": False}}, {"token": []},
    {"circuit": {"failure_threshold": 0}}, {"circuit": {"max_keys": True}},
    {"circuit": {"recovery_seconds": None}}, {"circuit": {"recovery_seconds": -1}},
])
def test_invalid_policy_does_not_change_version(client, config):
    response = client.put("/api/v1/policies/current", json={"config": config, "expected_version": 1})
    assert response.status_code == 422
    assert "error" in response.json()
    assert client.app.state.guard.snapshot()[1] == 1


def test_filters_pagination_and_missing_runs(client):
    guard = client.app.state.guard
    with guard.run("find-me"):
        pass
    with pytest.raises(RuntimeError), guard.run("failed"):
        raise RuntimeError("failure")
    assert client.get("/api/v1/runs?status=failed").json()["total"] == 1
    assert client.get("/api/v1/runs?search=find-me").json()["total"] == 1
    assert client.get("/api/v1/runs?search=%25").json()["total"] == 0
    assert len(client.get("/api/v1/runs?page_size=1&page=2").json()["items"]) == 1
    assert client.get("/api/v1/runs?page_size=101").status_code == 422
    assert client.get("/api/v1/overview?hours=169").status_code == 422
    assert client.get("/api/v1/runs/missing").status_code == 404


def test_demo_is_explicit_and_read_queries_never_seed_data(client, tmp_path):
    assert client.get("/api/v1/health").json()["demo"] is False
    assert client.get("/api/v1/runs").json()["total"] == 0
    assert client.post("/api/v1/demo/runs").status_code == 404
    with TestClient(create_app(database=tmp_path / "demo.db", demo=True)) as demo:
        assert demo.get("/api/v1/health").json()["demo"] is True
        assert demo.get("/api/v1/runs").json()["total"] == 48
        assert demo.get("/api/v1/overview").json()["blocked"] > 0
        assert demo.post("/api/v1/demo/runs").json()["created"] == 1


def test_untrusted_origins_and_hosts_denied(client):
    assert client.get("/api/v1/health", headers={"origin": "https://example.com"}).status_code == 403
    assert client.put("/api/v1/policies/current", headers={"origin": "null"}, json={}).status_code == 403
    assert client.get("/api/v1/health", headers={"host": "remote.example"}).status_code == 403


def test_retry_demo_respects_saved_policy_and_persists_attempt_events(tmp_path):
    with TestClient(create_app(database=tmp_path / 'retry.db', demo=True)) as client:
        first = client.post('/api/v1/demo/runs?scenario=retry').json()
        failed = client.get('/api/v1/runs/' + first['run_id']).json()
        assert failed['status'] == 'failed' and failed['retry_count'] == 0
        policy = client.get('/api/v1/policies/current').json()
        saved = client.put('/api/v1/policies/current', json={'expected_version': policy['version'], 'config': {'retry': {'max_retries': 1, 'initial_delay_seconds': 0}}})
        assert saved.status_code == 200
        second = client.post('/api/v1/demo/runs?scenario=retry').json()
        success = client.get('/api/v1/runs/' + second['run_id']).json()
        assert success['status'] == 'completed' and success['retry_count'] == 1
        events = success['events']
        assert [e['attempt'] for e in events if e['event'] == 'llm.requested'] == [1, 2]
        assert any(e['event'] == 'llm.retry_scheduled' and e['delay_seconds'] == 0 for e in events)
        assert 'provider secret' not in str(events)


def test_explicit_lan_frontend_origin_through_local_proxy(tmp_path):
    origin = 'http://192.168.3.185:5173'
    with TestClient(create_app(database=tmp_path / 'lan.db', allowed_origins=(origin,))) as client:
        assert client.get('/api/v1/health', headers={'origin': origin}).status_code == 200
        change = {'config': {'retry': {'max_retries': 1}}, 'expected_version': 1}
        assert client.post('/api/v1/policies/validate', headers={'origin': origin}, json=change).status_code == 200
        assert client.post('/api/v1/policies/validate', headers={'origin': 'http://192.168.3.186:5173'}, json=change).status_code == 403
        assert client.get('/api/v1/health', headers={'host': '192.168.3.185:8000'}).status_code == 403


@pytest.mark.parametrize('origin', ['*', 'http://*.example.com', 'http://user:pass@localhost', 'http://localhost/path', 'file://localhost'])
def test_invalid_allowed_origin_rejected_before_opening_storage(tmp_path, origin):
    path = tmp_path / 'invalid.db'
    with pytest.raises(ValueError): create_app(database=path, allowed_origins=(origin,))
    assert not path.exists()


def test_retry_policy_update_validation_and_fixed_run_snapshot(client):
    old = client.app.state.guard.run('old')
    before = client.get('/api/v1/policies/current').json()
    assert before['config']['retry']['max_retries'] == 0
    invalid = {'config': {'retry': {'max_retries': 11}}, 'expected_version': before['version']}
    assert client.post('/api/v1/policies/validate', json=invalid).status_code == 422
    change = {'config': {'retry': {'max_retries': 1, 'jitter': False}}, 'expected_version': before['version']}
    saved = client.put('/api/v1/policies/current', json=change)
    assert saved.status_code == 200
    assert saved.json()['config']['retry']['jitter'] is False
    assert old.config.retry.max_retries == 0
    assert client.app.state.guard.run('new').config.retry.max_retries == 1


def test_atomic_policy_compare_and_swap():
    manager = ConfigManager(environ={})
    def update(value):
        try:
            manager.update({"token": {"run_token_budget": value}}, expected_version=1)
            return True
        except ConfigVersionConflict:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(update, [100, 200])) == 1
    assert manager.snapshot().version == 2


def test_storage_failure_does_not_change_business_result():
    from agentguard import AgentGuard
    class BrokenStore:
        def save(self, state, config_version):
            raise RuntimeError("secret storage failure")
    with AgentGuard(run_store=BrokenStore()).run() as run:
        assert run.invoke(lambda **kwargs: {"value": 42})["value"] == 42
    assert run.state.status == "completed"


def test_sqlite_reopen_keeps_data(tmp_path):
    path = tmp_path / "persist.db"
    with TestClient(create_app(database=path)) as first:
        with first.app.state.guard.run("persistent"):
            pass
    with TestClient(create_app(database=path)) as second:
        assert second.get("/api/v1/runs").json()["total"] == 1


def test_event_pagination_is_bounded_stable_and_deduplicated(client):
    with client.app.state.guard.run("many-events") as run:
        for index in range(205):
            run.state.record("test.event", attempt=index)
    detail = client.get("/api/v1/runs/" + run.state.run_id).json()
    assert detail["events_total"] == 207
    assert len(detail["events"]) == 100
    second = client.get(f"/api/v1/runs/{run.state.run_id}/events?page=2&page_size=100").json()
    assert len(second["items"]) == 100
    assert not ({row["id"] for row in detail["events"]} & {row["id"] for row in second["items"]})


def test_model_filter_and_mixed_usage_and_missing_price(client):
    with client.app.state.guard.run("mixed") as run:
        run.invoke(lambda **kwargs: {"usage": {"input_tokens": 2}}, model="priced-missing")
        run.invoke(lambda **kwargs: {}, model="local-example")
    detail = client.get("/api/v1/runs/" + run.state.run_id).json()
    assert detail["usage_source"] == "mixed"
    assert detail["cost_known"] is False
    assert client.get("/api/v1/overview?model=nonexistent").json()["runs"] == 0
    overview = client.get("/api/v1/overview?model=local-example").json()
    assert overview["runs"] == 1
    assert overview["missing_price_runs"] == 1
    assert overview["sources"]["mixed"] == 1
