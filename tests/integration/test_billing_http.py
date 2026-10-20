import pytest
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient
from agentguard.api.http import create_app


def test_billing_sync_import_adjustment_persists_and_is_audited(tmp_path):
    path = tmp_path / "db.sqlite"
    with TestClient(create_app(database=path, demo=True)) as client:
        assert client.get("/api/v1/billing").json()["entries"] == 0
        assert client.post("/api/v1/billing/sync-estimates").json()["processed"] > 0
        before = client.get("/api/v1/billing/entries").json()
        request_id = before["items"][0]["request_id"]
        payload = {"provider":"supplier","invoice_id":"october","lines":[{"line_id":"1","request_id":request_id,"amount_usd":"0.12"}]}
        assert client.post("/api/v1/billing/invoices",json=payload).json()["imported"] == 1
        assert client.post("/api/v1/billing/invoices",json=payload).json()["duplicates"] == 1
        conflict = {**payload,"lines":[{**payload["lines"][0],"amount_usd":"0.13"}]}
        assert client.post("/api/v1/billing/invoices",json=conflict).status_code == 422
        reference = client.get("/api/v1/billing/entries").json()["items"][0]["entry_id"]
        assert client.post("/api/v1/billing/adjustments",json={"adjustment_id":"credit","reference_id":reference,"amount_usd":"-0.02"}).json()["created"]
        assert client.get("/api/v1/billing").json()["authoritative_usd"] == "0.10"
        assert client.get("/api/v1/billing").json()["matched_requests"] == 1
        assert client.post("/api/v1/billing/sync-estimates").status_code == 200
        assert client.get("/api/v1/billing/entries").json()["total"] == before["total"]+2
        audit = client.get("/api/v1/audit").json()["items"]
        assert any(e.get("action") == "billing.import" for e in audit)
        assert client.get("/api/v1/audit/health").json()["unresolved_operations"] == 0
    with TestClient(create_app(database=path)) as client:
        assert client.get("/api/v1/billing").json()["authoritative_usd"] == "0.10"
