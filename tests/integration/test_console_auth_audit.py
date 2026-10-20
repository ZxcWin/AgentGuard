import hashlib
import secrets

import pytest
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient
from agentguard.api.auth import ConsoleCredentials
from agentguard.api.http import create_app


def test_server_roles_protect_reads_writes_and_audit(tmp_path):
    tokens = {role: secrets.token_urlsafe(32) for role in ("viewer", "operator", "admin")}
    credentials = ConsoleCredentials([{"subject": role, "role": role, "token_sha256": hashlib.sha256(token.encode()).hexdigest()} for role, token in tokens.items()])
    with TestClient(create_app(database=tmp_path / "db.sqlite", credentials=credentials, public_hosts=("console.example",))) as client:
        assert client.get("/api/v1/health").json()["authentication"] == "bearer"
        assert client.get("/api/v1/runs").status_code == 401
        spec = client.get("/openapi.json")
        assert spec.status_code == 200
        assert spec.json()["components"]["securitySchemes"]["WorkspaceToken"]["scheme"] == "bearer"
        for role in tokens:
            headers = {"Authorization": "Bearer " + tokens[role]}
            current = client.get("/api/v1/policies/current", headers=headers).json()
            assert client.get("/api/v1/session", headers=headers).json()["role"] == role
            change = {"config": {"token": {"run_token_budget": 1000}}, "expected_version": current["version"]}
            result = client.put("/api/v1/policies/current", json=change, headers=headers)
            assert result.status_code == (200 if role == "admin" else 403)
            assert client.get("/api/v1/audit", headers=headers).status_code == (200 if role == "admin" else 403)
            assert client.post("/api/v1/policies/validate", json=change, headers=headers).status_code == (403 if role == "viewer" else 200)
        admin = {"Authorization": "Bearer " + tokens["admin"]}
        audit = client.get("/api/v1/audit", headers=admin).json()
        assert audit["total"] > 2
        assert audit["items"][0]["outcome"] == "published"
        assert "admin" == audit["items"][1]["actor_id"]
        assert client.get("/api/v1/runs", headers={**admin,"Origin":"https://attacker.example"}).status_code == 403
        assert client.get("http://console.example/api/v1/runs", headers=admin).status_code == 200
        assert client.get("http://other.example/api/v1/runs", headers=admin).status_code == 403
        assert client.get("/api/v1/billing",headers={"Authorization":"Bearer "+tokens["viewer"]}).status_code == 403


def test_policy_audit_failure_rejects_publish_before_changing_version(tmp_path, monkeypatch):
    with TestClient(create_app(database=tmp_path / "db.sqlite")) as client:
        current = client.get("/api/v1/policies/current").json()
        def broken(event): raise OSError("SECRET")
        monkeypatch.setattr(client.app.state.audit_store, "append", broken)
        response = client.put("/api/v1/policies/current", json={"expected_version":current["version"], "config":{"token":{"run_token_budget":100}}})
        assert response.status_code == 503
        assert "SECRET" not in response.text
        assert client.get("/api/v1/policies/current").json()["version"] == current["version"]


def test_remote_host_configuration_requires_credentials(tmp_path):
    with pytest.raises(ValueError): create_app(database=tmp_path / "db.sqlite", public_hosts=("console.example",))


def test_exact_cors_preflight_and_bearer_api_access(tmp_path):
    token = secrets.token_urlsafe(32)
    credentials = ConsoleCredentials([{"subject":"admin", "role":"admin", "token_sha256":hashlib.sha256(token.encode()).hexdigest()}])
    origin = "https://console.example"
    with TestClient(create_app(database=tmp_path / "db.sqlite",credentials=credentials,allowed_origins=(origin,))) as client:
        response = client.options("/api/v1/policies/current",headers={"Origin":origin,"Access-Control-Request-Method":"PUT","Access-Control-Request-Headers":"authorization,content-type"})
        assert response.status_code == 200 and response.headers["access-control-allow-origin"] == origin
        response = client.get("/api/v1/session",headers={"Origin":origin,"Authorization":"Bearer "+token})
        assert response.json()["role"] == "admin" and response.headers["access-control-allow-origin"] == origin
        assert client.options("/api/v1/session",headers={"Origin":"https://other.example","Access-Control-Request-Method":"GET"}).status_code == 400
