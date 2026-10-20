import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from time import sleep

from fastapi.testclient import TestClient

from agentguard.api.http import create_app


class ProviderHandler(BaseHTTPRequestHandler):
    requests = []

    def do_POST(self):
        length = int(self.headers.get("content-length", "0"))
        payload = json.loads(self.rfile.read(length))
        self.__class__.requests.append(payload)
        if payload.get("model") == "slow-model":
            sleep(0.08)
        if payload.get("model") == "provider-error":
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"rate limited"}}')
            return
        if payload.get("stream"):
            body = (
                b'data: {"id":"chatcmpl-test","choices":[{"delta":{"content":"ok"}}]}\n\n'
                b'data: {"id":"chatcmpl-test","choices":[],"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3}}\n\n'
                b"data: [DONE]\n\n"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
        else:
            body = json.dumps({"choices": [{"message": {"content": "ok"}}],
                               "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def provider_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_openai_proxy_records_non_stream_call(tmp_path):
    server = provider_server()
    try:
        with TestClient(create_app(database=tmp_path / "runs.db",
                                   llm_upstream_url=f"http://127.0.0.1:{server.server_port}/v1")) as client:
            response = client.post("/v1/chat/completions", json={
                "model": "proxy-model", "messages": [{"role": "user", "content": "hello"}],
                "temperature": 0.73, "max_tokens": 19,
            })
            assert response.status_code == 200
            assert response.json()["choices"][0]["message"]["content"] == "ok"
            assert ProviderHandler.requests[-1]["temperature"] == 0.73
            assert ProviderHandler.requests[-1]["max_tokens"] == 19
            rows = client.get("/api/v1/runs", params={"search": "default"}).json()
            assert rows["total"] == 1 and rows["items"][0]["llm_calls"] == 1
            assert client.get(f"/api/v1/runs/{rows['items'][0]['run_id']}").json()["total_tokens"] == 3
    finally:
        server.shutdown()


def test_proxy_uses_console_model_prices_for_cost_estimates(tmp_path):
    server = provider_server()
    try:
        with TestClient(create_app(database=tmp_path / "runs.db",
                                   llm_upstream_url=f"http://127.0.0.1:{server.server_port}/v1")) as client:
            policy = client.get("/api/v1/policies/current").json()
            config = {"pricing": {"version": "test-v1", "models": {
                "proxy-model": {"input_per_million": 1, "output_per_million": 2}
            }}}
            saved = client.put("/api/v1/policies/current", json={
                "expected_version": policy["version"], "config": config,
            })
            assert saved.status_code == 200

            response = client.post("/v1/chat/completions", json={
                "model": "proxy-model", "messages": [{"role": "user", "content": "hello"}],
            })
            assert response.status_code == 200
            rows = client.get("/api/v1/runs", params={"search": "default"}).json()
            detail = client.get(f"/api/v1/runs/{rows['items'][0]['run_id']}").json()
            assert detail["usage_source"] == "provider"
            assert detail["total_tokens"] == 3
            assert detail["cost_known"] is True
            assert detail["estimated_cost_usd"] == "0.000004"
    finally:
        server.shutdown()


def test_proxy_duration_includes_upstream_latency(tmp_path):
    server = provider_server()
    try:
        with TestClient(create_app(database=tmp_path / "runs.db",
                                   llm_upstream_url=f"http://127.0.0.1:{server.server_port}/v1")) as client:
            response = client.post("/proxy/default/v1/chat/completions", json={
                "model": "slow-model", "messages": [{"role": "user", "content": "hello"}],
            })
            run_id = client.get("/api/v1/runs", params={"search": "default"}).json()["items"][0]["run_id"]
            detail = client.get(f"/api/v1/runs/{run_id}").json()
            call = next(event for event in detail["events"] if event["event"] == "llm.completed")
            assert response.status_code == 200 and call["duration_seconds"] >= 0.06
    finally:
        server.shutdown()


def test_proxy_projects_are_persistent_and_project_scoped(tmp_path):
    server = provider_server()
    database = tmp_path / "runs.db"
    try:
        with TestClient(create_app(database=database)) as client:
            saved = client.put("/api/v1/proxy/projects", json={
                "name": "patent-agent", "upstream": f"http://127.0.0.1:{server.server_port}/v1",
            })
            assert saved.status_code == 200
            assert client.post("/proxy/patent-agent/v1/chat/completions", json={
                "model": "proxy-model", "messages": [{"role": "user", "content": "hello"}],
            }).status_code == 200
            rows = client.get("/api/v1/runs", params={"search": "patent-agent"}).json()
            assert rows["total"] == 1
        with TestClient(create_app(database=database)) as client:
            assert client.get("/api/v1/proxy/projects").json()["items"][0]["name"] == "patent-agent"
            assert client.delete("/api/v1/proxy/projects/patent-agent").json()["deleted"]
    finally:
        server.shutdown()


def test_provider_errors_are_forwarded_and_recorded(tmp_path):
    server = provider_server()
    try:
        with TestClient(create_app(database=tmp_path / "runs.db",
                                   llm_upstream_url=f"http://127.0.0.1:{server.server_port}/v1")) as client:
            response = client.post("/proxy/default/v1/chat/completions", json={
                "model": "provider-error", "messages": [{"role": "user", "content": "hello"}],
            })
            assert response.status_code == 429
            rows = client.get("/api/v1/runs", params={"search": "default"}).json()
            assert rows["total"] == 1 and rows["items"][0]["status"] == "failed"
    finally:
        server.shutdown()


def test_openai_proxy_preserves_sse_stream_and_records_usage(tmp_path):
    server = provider_server()
    try:
        with TestClient(create_app(database=tmp_path / "runs.db",
                                   llm_upstream_url=f"http://127.0.0.1:{server.server_port}/v1")) as client:
            response = client.post("/v1/chat/completions", json={
                "model": "proxy-model", "messages": [{"role": "user", "content": "hello"}], "stream": True,
            })
            assert response.status_code == 200
            assert "data: [DONE]" in response.text and '"content":"ok"' in response.text
            rows = client.get("/api/v1/runs", params={"search": "default"}).json()
            detail = client.get(f"/api/v1/runs/{rows['items'][0]['run_id']}").json()
            assert detail["total_tokens"] == 3
    finally:
        server.shutdown()
