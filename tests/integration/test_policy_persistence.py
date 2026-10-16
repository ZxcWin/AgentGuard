from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip('fastapi')
from fastapi.testclient import TestClient

from agentguard import AgentGuard, ConfigManager
from agentguard.api.http import create_app
from agentguard.adapters.storage.sqlite_policy import SQLitePolicyStore
from agentguard.config.manager import ConfigVersionConflict


def test_console_policy_survives_restart_with_version(tmp_path):
    path = tmp_path / 'console.db'
    with TestClient(create_app(database=path)) as first:
        change = {'expected_version': 1, 'config': {'tools': {'max_calls': 3}, 'timeout': {'run_seconds': 12}}}
        response = first.put('/api/v1/policies/current', json=change)
        assert response.status_code == 200
        assert response.json()['version'] == 2
    with TestClient(create_app(database=path)) as second:
        policy = second.get('/api/v1/policies/current').json()
        assert policy['version'] == 2
        assert policy['config']['tools']['max_calls'] == 3
        assert policy['config']['timeout']['run_seconds'] == 12
        assert policy['persistence'] == 'sqlite'


def test_explicit_shared_policy_store_updates_new_sdk_runs_only(tmp_path):
    path = tmp_path / 'shared.db'
    stores = [SQLitePolicyStore(path), SQLitePolicyStore(path)]
    try:
        first, second = [ConfigManager(policy_store=store, environ={}) for store in stores]
        guard = AgentGuard(config_manager=second)
        old = guard.run('old')
        first.update({'tools': {'max_calls': 2}}, expected_version=1)
        assert old.config.tools.max_calls is None
        assert guard.run('new').config.tools.max_calls == 2
        assert guard.snapshot()[1] == 2
    finally:
        for store in stores: store.close()


def test_cross_connection_cas_has_only_one_winner(tmp_path):
    path = tmp_path / 'race.db'
    stores = [SQLitePolicyStore(path), SQLitePolicyStore(path)]
    managers = [ConfigManager(policy_store=store, environ={}) for store in stores]
    def update(manager):
        try:
            manager.update({'tools': {'max_calls': 10}}, expected_version=1)
            return True
        except ConfigVersionConflict:
            return False
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sum(pool.map(update, managers)) == 1
        assert managers[0].snapshot().version == managers[1].snapshot().version == 2
    finally:
        for store in stores: store.close()


def test_persistence_failure_keeps_previous_snapshot():
    class Store:
        value = None
        def load(self): return self.value
        def save(self, version, config, *, expected_version):
            if version > 1: raise OSError('disk full')
            self.value = (version, config)
    manager = ConfigManager(policy_store=Store(), environ={})
    old = manager.snapshot()
    with pytest.raises(OSError): manager.update({'tools': {'max_calls': 2}})
    assert manager.snapshot() == old
