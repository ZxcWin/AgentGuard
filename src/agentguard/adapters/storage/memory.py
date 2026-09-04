from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from threading import RLock
from time import monotonic
from typing import Any

__all__ = ["CacheEntry", "MemoryCache", "make_cache_key"]


def make_cache_key(model: str, messages: Any, parameters: dict[str, Any] | None = None, version: str = "1") -> str:
    payload = repr((version, model, messages, parameters or {})).encode("utf-8")
    return sha256(payload).hexdigest()


@dataclass
class CacheEntry:
    value: Any
    expires_at: float | None = None

    def valid(self) -> bool:
        return self.expires_at is None or monotonic() < self.expires_at


class MemoryCache:
    def __init__(self, ttl_seconds: float | None = None) -> None:
        self.ttl_seconds = ttl_seconds
        self._entries: dict[str, CacheEntry] = {}
        self._lock = RLock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if not entry.valid():
                self._entries.pop(key, None)
                return None
            return entry.value

    def set(self, key: str, value: Any, ttl_seconds: float | None = None) -> None:
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        expires_at = None if ttl is None else monotonic() + ttl
        with self._lock:
            self._entries[key] = CacheEntry(value, expires_at)

    def delete(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
