from concurrent.futures import ThreadPoolExecutor
import json

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
        change = {'expected_version': 1, 'config': {'tools': {'max_calls': 3}, 'timeout': {'run_seconds': 12},
            'security': {'enabled': True, 'detect_pii': True}, 'quality': {'enabled': True, 'strict_json': True},
            'circuit': {'failure_threshold': 2, 'recovery_seconds': 7.5, 'max_keys': 100}}}
        response = first.put('/api/v1/policies/current', json=change)
        assert response.status_code == 200
        assert response.json()['version'] == 2
    with TestClient(create_app(database=path)) as second:
        policy = second.get('/api/v1/policies/current').json()
        assert policy['version'] == 2
        assert policy['config']['tools']['max_calls'] == 3
        assert policy['config']['timeout']['run_seconds'] == 12
        assert policy['config']['security']['enabled'] is True
        assert policy['config']['quality']['strict_json'] is True
        assert policy['config']['circuit'] == {'failure_threshold': 2, 'recovery_seconds': 7.5, 'max_keys': 100}
        assert second.app.state.guard.run().circuits.failure_threshold == 2
        assert second.app.state.guard.run().security_guard.detect_pii is True
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


def test_production_profile_persists_and_invalid_relaxation_is_atomic(tmp_path):
    path = tmp_path / 'strict.db'
    with TestClient(create_app(database=path, demo=True)) as client:
        initial = client.get('/api/v1/policies/current').json()
        assert initial['config']['enforcement']['profile'] == 'development'
        patch = {'enforcement': {'profile': 'production'}, 'token': {'missing_price': 'reject'},
                 'security': {'enabled': True, 'action': 'block', 'detect_secrets': True}}
        change = {'expected_version': initial['version'], 'config': patch}
        assert client.post('/api/v1/policies/validate', json=change).status_code == 200
        saved = client.put('/api/v1/policies/current', json=change).json()
        assert saved['version'] == initial['version'] + 1
        for invalid in ({'token': {'missing_price': 'observe'}}, {'security': {'enabled': False}},
                        {'token': {'max_output_tokens': None}}, {'budget': {'run_cost_usd': None}}):
            bad = {'expected_version': saved['version'], 'config': invalid}
            assert client.post('/api/v1/policies/validate', json=bad).status_code == 422
            assert client.put('/api/v1/policies/current', json=bad).status_code == 422
            assert client.get('/api/v1/policies/current').json() == saved
    with TestClient(create_app(database=path)) as client:
        assert client.get('/api/v1/policies/current').json() == saved
        assert client.app.state.guard.run().config.enforcement.profile == 'production'


def test_cache_policy_demo_clear_and_persistence(tmp_path):
    path = tmp_path / 'cache.db'
    with TestClient(create_app(database=path, demo=True)) as client:
        assert client.get('/api/v1/policies/current').json()['config']['cache']['enabled'] is False
        saved = client.put('/api/v1/policies/current', json={'expected_version': 1, 'config': {
            'cache': {'enabled': True, 'ttl_seconds': 60, 'allowed_models': ['local-example']}}}).json()
        for invalid in (0, 3601, True):
            assert client.put('/api/v1/policies/current', json={'expected_version': saved['version'],
                'config': {'cache': {'ttl_seconds': invalid}}}).status_code == 422
        demo = client.post('/api/v1/demo/runs?scenario=cache').json()
        run = client.get('/api/v1/runs/' + demo['run_id']).json()
        assert run['llm_calls'] == 1 and run['total_tokens'] == 12
        assert {'cache.stored', 'cache.hit'} <= {e['event'] for e in run['events']}
        assert 'local cache example' not in json.dumps(run['events'])
        clear = client.delete('/api/v1/cache')
        assert clear.status_code == 200 and clear.json()['scope'] == 'local_process'
        assert not client.app.state.guard._cache_runtime.store._entries
    with TestClient(create_app(database=path)) as client:
        assert client.get('/api/v1/policies/current').json()['config']['cache'] == saved['config']['cache']
