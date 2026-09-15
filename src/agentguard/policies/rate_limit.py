from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Condition, Lock
from time import monotonic, sleep
from typing import Any, Iterator

from ..config.loader import RateLimitConfig
from ..domain.exceptions import AgentGuardError, CircuitOpen, RateLimitExceeded


class SlidingWindowLimiter:
    def __init__(self, limit: int | None, window_seconds: float = 60.0) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: deque[tuple[float, int]] = deque()
        self._lock = Lock()

    def _prune(self, now: float) -> None:
        while self._events and now - self._events[0][0] >= self.window_seconds:
            self._events.popleft()

    def try_acquire(self, amount: int = 1) -> bool:
        if self.limit is None:
            return True
        if amount < 0:
            raise ValueError("数量必须是非负数")
        now = monotonic()
        with self._lock:
            self._prune(now)
            used = sum(item[1] for item in self._events)
            if used + amount > self.limit:
                return False
            self._events.append((now, amount))
            return True


class ConcurrencyLimiter:
    def __init__(self, limit: int | None) -> None:
        self.limit = limit
        self._active = 0
        self._condition = Condition()

    @contextmanager
    def acquire(self, timeout: float | None = None) -> Iterator[None]:
        if self.limit is None:
            yield
            return
        deadline = None if timeout is None else monotonic() + timeout
        with self._condition:
            while self._active >= self.limit:
                remaining = None if deadline is None else deadline - monotonic()
                if remaining is not None and remaining <= 0:
                    raise RateLimitExceeded("并发限制已超出")
                self._condition.wait(remaining)
            self._active += 1
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify()


class RateLimiter:
    def __init__(self, config: RateLimitConfig) -> None:
        self.request_limiter = SlidingWindowLimiter(config.rpm)
        self.token_limiter = SlidingWindowLimiter(config.tpm)
        self.tool_limiter = SlidingWindowLimiter(config.max_tool_calls)
        self.concurrency = ConcurrencyLimiter(config.max_concurrency)

    def try_acquire(self, tokens: int = 1) -> bool:
        now = monotonic()
        with self.request_limiter._lock, self.token_limiter._lock:
            self.request_limiter._prune(now)
            self.token_limiter._prune(now)
            request_used = sum(item[1] for item in self.request_limiter._events)
            token_used = sum(item[1] for item in self.token_limiter._events)
            if self.request_limiter.limit is not None and request_used + 1 > self.request_limiter.limit:
                return False
            if self.token_limiter.limit is not None and token_used + tokens > self.token_limiter.limit:
                return False
            if self.request_limiter.limit is not None:
                self.request_limiter._events.append((now, 1))
            if self.token_limiter.limit is not None:
                self.token_limiter._events.append((now, tokens))
            return True

    def acquire(self, tokens: int = 1, wait: float = 0.0) -> None:
        deadline = monotonic() + wait
        while True:
            if self.try_acquire(tokens):
                return
            if monotonic() >= deadline:
                raise RateLimitExceeded("请求速率或 Token 速率限制已超出")
            sleep(min(0.05, max(0.0, deadline - monotonic())))

    def release(self, tokens: int = 1) -> None:
        with self.request_limiter._lock, self.token_limiter._lock:
            if self.request_limiter.limit is not None and self.request_limiter._events:
                self.request_limiter._events.pop()
            if self.token_limiter.limit is not None and self.token_limiter._events:
                self.token_limiter._events.pop()


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    recovery_seconds: float = 30.0
    failures: int = 0
    opened_at: float | None = None
    probe_in_flight: bool = False
    _lock: Lock = field(default_factory=Lock, repr=False, compare=False)

    def before_call(self) -> None:
        with self._lock:
            if self.opened_at is None:
                return
            if monotonic() - self.opened_at < self.recovery_seconds:
                raise CircuitOpen("熔断器已打开")
            if self.probe_in_flight:
                raise CircuitOpen("熔断器恢复探测正在执行")
            self.probe_in_flight = True

    def record_success(self) -> None:
        with self._lock:
            self.failures = 0
            self.opened_at = None
            self.probe_in_flight = False

    def record_failure(self) -> None:
        with self._lock:
            self.failures += 1
            if self.failures >= self.failure_threshold:
                self.opened_at = monotonic()
            self.probe_in_flight = False


class CircuitRegistry:
    """为受限数量的 scope key 保存相互独立的熔断器。"""

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        recovery_seconds: float = 30.0,
        max_keys: int = 10_000,
    ) -> None:
        if failure_threshold <= 0 or recovery_seconds < 0 or max_keys <= 0:
            raise ValueError("熔断配置必须是正数")
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self.max_keys = max_keys
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = Lock()

    def get(self, key: str) -> CircuitBreaker:
        with self._lock:
            breaker = self._breakers.get(key)
            if breaker is not None:
                return breaker
            if len(self._breakers) >= self.max_keys:
                raise CircuitOpen("熔断 scope key 数量已超限")
            breaker = CircuitBreaker(self.failure_threshold, self.recovery_seconds)
            self._breakers[key] = breaker
            return breaker

    def before_call(self, key: str) -> CircuitBreaker:
        breaker = self.get(key)
        breaker.before_call()
        return breaker

    @staticmethod
    def is_provider_failure(error: BaseException) -> bool:
        return not isinstance(error, AgentGuardError) and not isinstance(
            error, (ValueError, TypeError, KeyError)
        )

    def record_failure(self, key: str, error: BaseException) -> None:
        if self.is_provider_failure(error):
            self.get(key).record_failure()

    def record_success(self, key: str) -> None:
        self.get(key).record_success()

class AdmissionLease:
    def __init__(self, limiter: "AdmissionController", key: str, tool: bool = False) -> None:
        self._limiter = limiter
        self.key = key
        self.released = False
        self.tool = tool

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        self._limiter._release(self.key)


class AdmissionController:
    """按 key 以原子方式管理请求速率、Token 速率和并发准入。"""

    def __init__(self, config: RateLimitConfig) -> None:
        self.config = config
        self._states: dict[str, tuple[RateLimiter, ConcurrencyLimiter]] = {}
        self._last_used: dict[str, float] = {}
        self._lock = Lock()

    def _state(self, key: str) -> tuple[RateLimiter, ConcurrencyLimiter]:
        with self._lock:
            current_time = monotonic()
            state = self._states.get(key)
            if state is not None:
                self._last_used[key] = current_time
                return state
            max_keys = self.config.max_scope_keys
            if len(self._states) >= max_keys:
                idle_keys = [
                    candidate
                    for candidate, (_, concurrency) in self._states.items()
                    if concurrency._active == 0 and candidate != key
                ]
                if idle_keys:
                    expired_key = min(idle_keys, key=self._last_used.get)
                    self._states.pop(expired_key, None)
                    self._last_used.pop(expired_key, None)
                else:
                    raise RateLimitExceeded("准入 scope key 数量已超限")
            state = (RateLimiter(self.config), ConcurrencyLimiter(self.config.max_concurrency))
            self._states[key] = state
            self._last_used[key] = current_time
            return state

    def try_acquire(
        self, tokens: int = 1, *, key: str = "*", tool: bool = False
    ) -> AdmissionLease | None:
        limiter, concurrency = self._state(key)
        if tool:
            if not limiter.tool_limiter.try_acquire(1):
                return None
        elif not limiter.try_acquire(tokens):
            return None
        with concurrency._condition:
            if concurrency.limit is not None and concurrency._active >= concurrency.limit:
                if tool:
                    with limiter.tool_limiter._lock:
                        if limiter.tool_limiter._events:
                            limiter.tool_limiter._events.pop()
                else:
                    limiter.release(tokens)
                return None
            concurrency._active += 1
        return AdmissionLease(self, key, tool=tool)

    def acquire(
        self,
        tokens: int = 1,
        *,
        key: str = "*",
        wait: float = 0.0,
        tool: bool = False,
    ) -> AdmissionLease:
        if tokens < 0:
            raise ValueError("Token 数必须是非负数")
        deadline = None if wait is None else monotonic() + wait
        while True:
            lease = self.try_acquire(tokens, key=key, tool=tool)
            if lease is not None:
                return lease
            if deadline is not None and monotonic() >= deadline:
                raise RateLimitExceeded("准入限制已超出")
            sleep(0.01 if deadline is None else min(0.05, max(0.0, deadline - monotonic())))

    def _release(self, key: str) -> None:
        _, concurrency = self._state(key)
        with concurrency._condition:
            if concurrency._active > 0:
                concurrency._active -= 1
                concurrency._condition.notify()
        with self._lock:
            self._last_used[key] = monotonic()
