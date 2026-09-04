from pathlib import Path

import pytest

from agentguard import AgentGuard, ConfigManager, ConfigurationError
from agentguard.config import manager as config_manager_module


def test_update_merges_partial_config_and_increments_version():
    manager = ConfigManager({"token": {"run_token_budget": 100, "max_output_tokens": 25}})

    updated = manager.update({"token": {"run_token_budget": 200}})

    assert updated.version == 2
    assert updated.config.token.run_token_budget == 200
    assert updated.config.token.max_output_tokens == 25
    assert manager.snapshot() == updated


def test_invalid_update_keeps_previous_snapshot():
    manager = ConfigManager({"token": {"run_token_budget": 100}})
    before = manager.snapshot()

    with pytest.raises(ConfigurationError):
        manager.update({"token": {"reservation_buffer": 2}})

    assert manager.snapshot() == before


def test_existing_run_keeps_old_snapshot_after_update():
    guard = AgentGuard({"token": {"run_token_budget": 100}})
    run = guard.run(name="old")
    guard.update_config({"token": {"run_token_budget": 200}})

    assert run.config.token.run_token_budget == 100
    assert run.config_version == 1
    assert guard.config.token.run_token_budget == 200
    assert guard.snapshot()[1] == 2


def test_run_records_config_version():
    guard = AgentGuard({"token": {"run_token_budget": 100}})

    with guard.run(name="versioned") as run:
        pass

    assert run.state.events[0]["event"] == "run.started"
    assert run.state.events[0]["config_version"] == 1


def test_reload_failure_does_not_replace_snapshot(monkeypatch, tmp_path: Path):
    manager = ConfigManager({"token": {"run_token_budget": 100}})
    manager._config_path = str(tmp_path / "agentguard.yaml")
    before = manager.snapshot()

    def fail_to_load(*args, **kwargs):
        raise ConfigurationError("invalid reloaded configuration")

    monkeypatch.setattr(config_manager_module, "load_config", fail_to_load)
    with pytest.raises(ConfigurationError):
        manager.reload()

    assert manager.snapshot() == before
