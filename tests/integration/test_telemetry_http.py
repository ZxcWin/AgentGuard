import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from agentguard.api.http import create_app


def test_telemetry_health_metrics_opt_in_and_policy_persistence(tmp_path):
    path = tmp_path / "console.db"
    with TestClient(create_app(database=path, demo=True)) as client:
        initial = client.get("/api/v1/telemetry").json()
        assert initial["enabled"] and initial["received"] > 0
        assert not initial["audit_durable"] and initial["retention_days"] == 7
        assert client.get("/api/v1/metrics").status_code == 404
        current = client.get("/api/v1/policies/current").json()
        result = client.put("/api/v1/policies/current", json={"expected_version": current["version"],
            "config": {"telemetry": {"enable_metrics_export": True, "retention_days": 3}}})
        assert result.status_code == 200
        metrics = client.get("/api/v1/metrics")
        assert metrics.status_code == 200 and "agentguard_events_received_total" in metrics.text
        assert client.get("/api/v1/telemetry").json()["retention_days"] == 3
        assert client.get("/api/v1/metrics", headers={"origin": "https://untrusted.example"}).status_code == 403
    with TestClient(create_app(database=path)) as client:
        assert client.get("/api/v1/metrics").status_code == 200
        assert client.get("/api/v1/telemetry").json()["retention_days"] == 3
