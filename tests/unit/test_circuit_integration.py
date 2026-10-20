import asyncio

import pytest

from agentguard import AgentGuard, AgentGuardConfig, CircuitConfig, CircuitOpen, CircuitRegistry, ConfigurationError


def test_runtime_opens_scoped_circuit_after_provider_failures():
    guard = AgentGuard({"rate_limit": {"max_concurrency": 1}})

    def provider(**kwargs):
        raise RuntimeError("upstream unavailable")

    with guard.run(tenant_id="tenant-a") as run:
        run.circuits = CircuitRegistry(failure_threshold=1, recovery_seconds=60)
        with pytest.raises(RuntimeError):
            run.invoke(provider, model="demo", messages="hello", max_output_tokens=1)
        with pytest.raises(CircuitOpen):
            run.invoke(provider, model="demo", messages="hello", max_output_tokens=1)

    assert run.budget._reserved_tokens == 0
    assert any(event["event"] == "circuit.rejected" for event in run.state.events)


def test_circuit_keys_are_isolated_by_model_and_tenant():
    registry = CircuitRegistry(failure_threshold=1, recovery_seconds=60)
    registry.before_call("tenant-a|model-a")
    registry.record_failure("tenant-a|model-a", RuntimeError("failed"))

    with pytest.raises(CircuitOpen):
        registry.before_call("tenant-a|model-a")
    registry.before_call("tenant-a|model-b")
    registry.before_call("tenant-b|model-a")


@pytest.mark.parametrize('field,values', [
    ('failure_threshold', [None, True, 0, -1, 1.5, '2', 1_000_001]),
    ('max_keys', [None, True, 0, -1, 1.5, '2', 100_001]),
    ('recovery_seconds', [None, True, -1, '2', float('nan'), float('inf'), 86401]),
])
def test_invalid_circuit_config_cannot_replace_live_policy(field, values):
    guard = AgentGuard()
    try:
        before = guard.snapshot()
        for value in values:
            with pytest.raises(ConfigurationError):
                guard.update_config({'circuit': {field: value}})
            with pytest.raises(ConfigurationError):
                CircuitConfig(**{field: value})
            assert guard.snapshot() == before
    finally:
        guard.close()


def test_configured_circuit_blocks_calls_and_recovery_releases_resources(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr('agentguard.policies.rate_limit.monotonic', lambda: clock[0])
    attempts = []
    def provider(**kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError('upstream unavailable')
        return {'usage': {'input_tokens': 1, 'output_tokens': 1}, 'content': 'ok'}
    guard = AgentGuard({'circuit': {'failure_threshold': 1, 'recovery_seconds': 5},
                        'rate_limit': {'max_concurrency': 1}})
    try:
        with guard.run() as run:
            with pytest.raises(RuntimeError):
                run.invoke(provider, model='test', max_tokens=1)
            with pytest.raises(CircuitOpen):
                run.invoke(provider, model='test', max_tokens=1)
            assert len(attempts) == 1
            clock[0] += 5
            assert run.invoke(provider, model='test', max_tokens=1)['content'] == 'ok'
            assert run.invoke(provider, model='test', max_tokens=1)['content'] == 'ok'
            assert run.budget._reserved_tokens == 0
            assert all(limiter._active == 0 for _, limiter in run.admission._states.values())
    finally:
        guard.close()


def test_circuit_capacity_rejects_new_scope_without_evicting_open_circuit():
    guard = AgentGuard({'circuit': {'failure_threshold': 1, 'max_keys': 1}})
    def failure(**kwargs):
        raise RuntimeError('failed')
    try:
        with guard.run() as run:
            with pytest.raises(RuntimeError):
                run.invoke(failure, model='first', max_tokens=1)
            with pytest.raises(CircuitOpen):
                run.invoke(lambda **kw: 'must not execute', model='second', max_tokens=1)
            with pytest.raises(CircuitOpen):
                run.invoke(failure, model='first', max_tokens=1)
            assert len(run.circuits._breakers) == 1
            assert run.budget._reserved_tokens == 0
    finally:
        guard.close()


def test_circuit_config_hot_update_keeps_existing_run_and_legacy_defaults():
    assert AgentGuardConfig.from_mapping({}).circuit == CircuitConfig()
    guard = AgentGuard()
    try:
        old = guard.run()
        guard.update_config({'circuit': {'failure_threshold': 2, 'recovery_seconds': 0, 'max_keys': 4}})
        new = guard.run()
        assert (old.circuits.failure_threshold, old.circuits.recovery_seconds, old.circuits.max_keys) == (5, 30, 10000)
        assert (new.circuits.failure_threshold, new.circuits.recovery_seconds, new.circuits.max_keys) == (2, 0, 4)
        assert new.config_version == old.config_version + 1
    finally:
        guard.close()


@pytest.mark.parametrize('async_mode', [False, True])
def test_configured_tool_circuit_blocks_side_effect_and_isolates_tool_scope(async_mode):
    attempts = []
    def failing():
        attempts.append(1)
        raise RuntimeError('unavailable')
    async def async_failing():
        return failing()
    guard = AgentGuard({'circuit': {'failure_threshold': 1, 'recovery_seconds': 60},
                        'rate_limit': {'max_concurrency': 1}})
    async def execute_async():
        async with guard.run() as run:
            with pytest.raises(RuntimeError):
                await run.invoke_tool('failing', async_failing)
            with pytest.raises(CircuitOpen):
                await run.invoke_tool('failing', async_failing)
            assert await run.invoke_tool('healthy', asyncio.sleep, 0, result='ok') == 'ok'
            return run
    try:
        if async_mode:
            run = asyncio.run(execute_async())
        else:
            with guard.run() as run:
                with pytest.raises(RuntimeError):
                    run.invoke_tool('failing', failing)
                with pytest.raises(CircuitOpen):
                    run.invoke_tool('failing', failing)
                assert run.invoke_tool('healthy', lambda: 'ok') == 'ok'
        assert attempts == [1]
        assert any(e['event'] == 'circuit.rejected' and e['tool'] == 'failing' for e in run.state.events)
        assert all(limiter._active == 0 for _, limiter in run.admission._states.values())
    finally:
        guard.close()


@pytest.mark.parametrize('async_mode', [False, True])
def test_model_circuit_does_not_block_other_model_or_tool(async_mode):
    def provider(**kwargs):
        if kwargs.get('model') == 'offline':
            raise ConnectionError('offline')
        return 'healthy'
    async def async_provider(**kwargs):
        return provider(**kwargs)
    guard = AgentGuard({'circuit': {'failure_threshold': 1}})
    async def execute_async():
        async with guard.run() as run:
            with pytest.raises(ConnectionError):
                await run.invoke(async_provider, model='offline')
            with pytest.raises(CircuitOpen):
                await run.invoke(async_provider, model='offline')
            assert await run.invoke(async_provider, model='online') == 'healthy'
            assert await run.invoke_tool('offline', asyncio.sleep, 0, result='healthy') == 'healthy'
    try:
        if async_mode:
            asyncio.run(execute_async())
        else:
            with guard.run() as run:
                with pytest.raises(ConnectionError):
                    run.invoke(provider, model='offline')
                with pytest.raises(CircuitOpen):
                    run.invoke(provider, model='offline')
                assert run.invoke(provider, model='online') == 'healthy'
                assert run.invoke_tool('offline', lambda: 'healthy') == 'healthy'
    finally:
        guard.close()


def test_circuit_config_environment_and_roundtrip():
    from dataclasses import asdict
    from agentguard import load_config
    config = load_config(environ={'AGENTGUARD_CIRCUIT__FAILURE_THRESHOLD': '2',
                                  'AGENTGUARD_CIRCUIT__RECOVERY_SECONDS': '0.25',
                                  'AGENTGUARD_CIRCUIT__MAX_KEYS': '10'})
    assert config.circuit == CircuitConfig(failure_threshold=2, recovery_seconds=.25, max_keys=10)
    assert AgentGuardConfig.from_mapping(asdict(config)) == config
    with pytest.raises(ConfigurationError):
        AgentGuardConfig.from_mapping({'circuit': {'misspelled_threshold': 1}})
