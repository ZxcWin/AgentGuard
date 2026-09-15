import pytest

from agentguard import AgentGuardConfig, RateLimitExceeded, RedisAdmissionBackend, RedisAdmissionController


class FakeRedis:
    def __init__(self, admitted=1):
        self.admitted = admitted
        self.calls = []

    def eval(self, script, numkeys, *args):
        self.calls.append((script, numkeys, args))
        return self.admitted if len(self.calls) == 1 else 1


def test_redis_admission_uses_atomic_lua_and_releases_idempotently():
    client = FakeRedis()
    backend = RedisAdmissionBackend(client, prefix="ag", window_seconds=60)

    lease = backend.acquire(key="tenant-a|model-x", tokens=12, rpm=10, tpm=100)
    lease.release()
    lease.release()

    assert len(client.calls) == 2
    reserve_script, reserve_keys, reserve_args = client.calls[0]
    release_script, release_keys, release_args = client.calls[1]
    assert reserve_keys == release_keys == 3
    assert "redis.call('TIME')" in reserve_script
    assert "ZADD" in reserve_script
    assert "ZREM" in release_script
    assert reserve_args[:3] == release_args[:3]
    assert reserve_args[3:] == (60, 10, 100, 12, reserve_args[-1])
    assert release_args[-1] == 12
    assert "{tenant-a|model-x}" in reserve_args[0]
    assert "token_values" in reserve_args[2]
    assert lease.released is True


def test_redis_admission_rejects_atomically_when_script_denies():
    client = FakeRedis(admitted=0)
    backend = RedisAdmissionBackend(client)

    with pytest.raises(RateLimitExceeded):
        backend.acquire(key="tenant-a", tokens=1, rpm=1)


def test_redis_admission_validates_inputs_without_client_call():
    client = FakeRedis()
    backend = RedisAdmissionBackend(client)

    with pytest.raises(ValueError):
        backend.acquire(key="tenant-a", tokens=-1)
    assert client.calls == []


def test_redis_controller_can_fail_closed_or_fallback_to_memory():
    class BrokenRedis:
        def eval(self, *args):
            raise OSError("redis unavailable")

    reject_config = AgentGuardConfig.from_mapping(
        {"rate_limit": {"backend": "redis", "redis_url": "redis://unused", "redis_failure_policy": "reject"}}
    )
    reject_controller = RedisAdmissionController(BrokenRedis(), reject_config.rate_limit)
    with pytest.raises(OSError):
        reject_controller.acquire(key="tenant", tokens=1)

    memory_config = AgentGuardConfig.from_mapping(
        {"rate_limit": {"backend": "redis", "redis_url": "redis://unused", "redis_failure_policy": "memory"}}
    )
    memory_controller = RedisAdmissionController(BrokenRedis(), memory_config.rate_limit)
    lease = memory_controller.acquire(key="tenant", tokens=1)
    lease.release()


def test_redis_controller_keeps_tool_limit_during_memory_fallback():
    class BrokenRedis:
        def eval(self, *args):
            raise OSError("redis unavailable")

    config = AgentGuardConfig.from_mapping(
        {
            "rate_limit": {
                "backend": "redis",
                "redis_url": "redis://unused",
                "redis_failure_policy": "memory",
                "max_tool_calls": 1,
            }
        }
    )
    controller = RedisAdmissionController(BrokenRedis(), config.rate_limit)
    lease = controller.acquire(key="tenant|tool=search", tokens=0, tool=True)
    lease.release()
    with pytest.raises(RateLimitExceeded):
        controller.acquire(key="tenant|tool=search", tokens=0, tool=True)
