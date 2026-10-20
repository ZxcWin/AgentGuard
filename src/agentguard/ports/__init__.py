"""Interfaces for optional infrastructure."""

from typing import Any, Mapping, Protocol

from ..domain.models import RunState


class EventSink(Protocol):
    def emit(self, event: Mapping[str, Any]) -> None: ...


class AuditStore(Protocol):
    def append(self, event: Mapping[str, Any]) -> str: ...


class RunStore(Protocol):
    def save(self, state: RunState, config_version: int) -> None: ...


class PolicyStore(Protocol):
    def load(self) -> tuple[int, dict[str, Any]] | None: ...
    def save(self, version: int, config: dict[str, Any], *, expected_version: int) -> None: ...


class ExactCacheStore(Protocol):
    """Bounded local store; implementations must never wait on network IO.

    Persistent stores may additionally implement generation/claim/release for
    invalidation and coalescing. Backend failures become ordinary cache misses.
    """
    def get(self, key: str) -> bytes | None: ...
    def set(self, key: str, value: bytes, *, ttl_seconds: int) -> None: ...
    def delete(self, key: str) -> None: ...
    def clear(self) -> None: ...
