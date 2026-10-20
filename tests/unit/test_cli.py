import json
from pathlib import Path
import subprocess
import sys

import pytest

from agentguard.cli import main


def invoke(capsys, *args):
    status = main(list(args))
    return status, json.loads(capsys.readouterr().out)


def test_init_check_simulate_and_no_overwrite(tmp_path, capsys):
    path = str(tmp_path / "agentguard.json")
    assert invoke(capsys, "init", "--output", path)[0] == 0
    initial = Path(path).read_bytes()
    assert invoke(capsys, "init", "--output", path)[0] == 2
    assert Path(path).read_bytes() == initial
    status, checked = invoke(capsys, "check", "--config", path)
    assert status == 0 and checked["valid"]
    status, result = invoke(capsys, "simulate", "--config", path)
    assert status == 0 and result["offline"]
    assert result["summary"]["total_tokens"] == 12
    assert result["decisions"] == [{"index": 0, "allowed": True}]


def test_explain_blocked_request_without_secrets(tmp_path, capsys):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"security": {"enabled": True}}))
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"messages": "api_key=PRIVATE_VALUE"}))
    status, result = invoke(capsys, "explain", "--config", str(config), "--fixture", str(fixture))
    assert status == 1
    assert result["decisions"][0]["reason"] == "SecurityViolation"
    assert result["summary"]["llm_calls"] == 0
    assert "PRIVATE_VALUE" not in json.dumps(result)


def test_replay_shadow_shares_budget_and_never_executes_tools(tmp_path, capsys):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"token": {"run_token_budget": 45, "retry_budget_ratio": 1}}))
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps([{}, {}, {}]))
    status, result = invoke(capsys, "replay", str(fixture), "--config", str(config))
    assert status == 1 and result["shadow"]
    assert result["summary"]["llm_calls"] == 1
    assert result["summary"]["tool_calls"] == 0
    fixture.write_text(json.dumps([{"tool": "execute"}]))
    assert invoke(capsys, "replay", str(fixture), "--config", str(config))[0] == 2


def test_offline_rejects_redis_before_connecting(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("AGENTGUARD_RATE_LIMIT__BACKEND", "redis")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"rate_limit": {"backend": "redis", "redis_url": "redis://127.0.0.1:1"}}))
    assert invoke(capsys, "simulate", "--config", str(config))[0] == 2
    config.write_text("{}")
    assert invoke(capsys, "simulate", "--config", str(config))[0] == 0


@pytest.mark.parametrize("raw", ['{"secret":"PRIVATE"}', '[', '{"token":{"run_token_budget":-1}}', '{"x":NaN}'])
def test_invalid_config_is_sanitized(tmp_path, capsys, raw):
    path = tmp_path / "bad.json"
    path.write_text(raw)
    status, result = invoke(capsys, "check", "--config", str(path))
    assert status == 2 and "PRIVATE" not in json.dumps(result)


def test_python_module_entrypoint():
    result = subprocess.run([sys.executable, "-m", "agentguard", "--version"], capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout.strip() == "agentguard 0.1.0"


def test_invalid_yaml_does_not_emit_traceback_or_values(tmp_path, capsys):
    path = tmp_path / "bad.yaml"
    path.write_text("token: [PRIVATE,\n")
    status, result = invoke(capsys, "check", "--config", str(path))
    assert status == 2 and "PRIVATE" not in json.dumps(result)
