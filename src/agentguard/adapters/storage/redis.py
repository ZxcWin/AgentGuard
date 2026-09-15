from __future__ import annotations

from dataclasses import dataclass, replace
from time import time
from typing import Any, Protocol

from ...domain.exceptions import RateLimitExceeded
from ...config.schema import RateLimitConfig
from ...policies.rate_limit import AdmissionController


class RedisClient(Protocol):
    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> Any: ...


_RESERVE_SCRIPT = """
local now = redis.call('TIME')[1]
local window = tonumber(ARGV[1])
local request_limit = tonumber(ARGV[2])
local token_limit = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, now - window)
local expired = redis.call('ZRANGEBYSCORE', KEYS[2], 0, now - window)
for _, member in ipairs(expired) do
  local amount = tonumber(redis.call('HGET', KEYS[3], member) or '0')
  redis.call('DECRBY', KEYS[2] .. ':total', amount)
  redis.call('HDEL', KEYS[3], member)
end
redis.call('ZREMRANGEBYSCORE', KEYS[2], 0, now - window)
local request_count = redis.call('ZCARD', KEYS[1])
local token_count = tonumber(redis.call('GET', KEYS[2] .. ':total') or '0')
if request_limit > 0 and request_count + 1 > request_limit then return 0 end
if token_limit > 0 and token_count + tonumber(ARGV[4]) > token_limit then return 0 end
local member = ARGV[5]
redis.call('ZADD', KEYS[1], now, member)
redis.call('ZADD', KEYS[2], now, member)
redis.call('HSET', KEYS[3], member, tonumber(ARGV[4]))
redis.call('INCRBY', KEYS[2] .. ':total', tonumber(ARGV[4]))
redis.call('EXPIRE', KEYS[1], math.ceil(window))
redis.call('EXPIRE', KEYS[2], math.ceil(window))
redis.call('EXPIRE', KEYS[2] .. ':total', math.ceil(window))
redis.call('EXPIRE', KEYS[3], math.ceil(window))
return 1
"""

_RELEASE_SCRIPT = """
local member = ARGV[1]
local tokens = tonumber(ARGV[2])
redis.call('ZREM', KEYS[1], member)
redis.call('ZREM', KEYS[2], member)
redis.call('HDEL', KEYS[3], member)
redis.call('DECRBY', KEYS[2] .. ':total', tokens)
local total = tonumber(redis.call('GET', KEYS[2] .. ':total') or '0')
if total <= 0 then redis.call('DEL', KEYS[2] .. ':total') end
return 1
"""


@dataclass
class RedisAdmissionLease:
    backend: "RedisAdmissionBackend"
    key: str
    member: str
    tokens: int
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        self.backend._release(self)


class RedisAdmissionBackend:
    """基于 Redis 的滑动窗口准入，通过 Lua 原子完成预留。"""

    def __init__(self, client: RedisClient, *, prefix: str = "agentguard", window_seconds: int = 60) -> None:
        if not prefix or window_seconds <= 0:
            raise ValueError("prefix 不能为空，window_seconds 必须为正数")
        self.client = client
        self.prefix = prefix
        self.window_seconds = window_seconds
        self._counter = 0

    def _keys(self, key: str) -> tuple[str, str]:
        safe_key = str(key).replace(" ", "_")
        tag = f"{{{safe_key}}}"
        return (
            f"{self.prefix}:{tag}:requests",
            f"{self.prefix}:{tag}:tokens",
            f"{self.prefix}:{tag}:token_values",
        )

    def acquire(self, *, key: str, tokens: int, rpm: int | None = None, tpm: int | None = None) -> RedisAdmissionLease:
        if tokens < 0:
            raise ValueError("Token 数必须是非负数")
        self._counter += 1
        member = f"{self._counter}-{time_ns()}"
        request_key, token_key, values_key = self._keys(key)
        admitted = self.client.eval(
            _RESERVE_SCRIPT,
            3,
            request_key,
            token_key,
            values_key,
            self.window_seconds,
            rpm or 0,
            tpm or 0,
            tokens,
            member,
        )
        if int(admitted) != 1:
            raise RateLimitExceeded("Redis 准入限制已超出")
        return RedisAdmissionLease(self, key, member, tokens)

    def _release(self, lease: RedisAdmissionLease) -> None:
        request_key, token_key, values_key = self._keys(lease.key)
        self.client.eval(_RELEASE_SCRIPT, 3, request_key, token_key, values_key, lease.member, lease.tokens)


class RedisAdmissionController:
    """组合 Redis 的 RPM/TPM 限制与进程内并发 lease。"""

    def __init__(self, client: RedisClient, config: RateLimitConfig) -> None:
        self.backend = RedisAdmissionBackend(
            client,
            prefix=config.redis_prefix,
            window_seconds=config.redis_window_seconds,
        )
        local_config = replace(config, rpm=None, tpm=None)
        self.local = AdmissionController(local_config)
        self.config = config

    def acquire(self, tokens: int = 1, *, key: str = "*", wait: float = 0.0, tool: bool = False) -> "RedisControllerLease":
        if tool:
            local_lease = self.local.acquire(tokens, key=key, wait=wait, tool=True)
            try:
                redis_lease = self.backend.acquire(key=key, tokens=0, rpm=None, tpm=None)
            except Exception:
                if self.config.redis_failure_policy == "memory":
                    return RedisControllerLease(_NoopRedisLease(), local_lease)
                local_lease.release()
                raise
        else:
            try:
                redis_lease = self.backend.acquire(
                    key=key, tokens=tokens, rpm=self.config.rpm, tpm=self.config.tpm
                )
            except Exception:
                if self.config.redis_failure_policy == "memory":
                    return RedisControllerLease(
                        _NoopRedisLease(),
                        self.local.acquire(tokens, key=key, wait=wait),
                    )
                raise
            try:
                local_lease = self.local.acquire(tokens, key=key, wait=wait)
            except BaseException:
                redis_lease.release()
                raise
        return RedisControllerLease(redis_lease, local_lease)


class _NoopRedisLease:
    def release(self) -> None:
        return None


@dataclass
class RedisControllerLease:
    redis_lease: RedisAdmissionLease
    local_lease: Any
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        self.redis_lease.release()
        self.local_lease.release()


def time_ns() -> int:
    return int(time() * 1_000_000_000)


__all__ = ["RedisAdmissionBackend", "RedisAdmissionLease", "RedisAdmissionController", "RedisControllerLease", "RedisClient"]
