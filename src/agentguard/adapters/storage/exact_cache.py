"""Bounded process-local LRU with mandatory expiry and immutable payloads."""
from collections import OrderedDict
from threading import RLock
from time import monotonic


class MemoryExactCache:
    def __init__(self, *, max_entries: int = 256, max_bytes: int = 16 * 1024 * 1024,
                 clock=monotonic) -> None:
        if type(max_entries) is not int or max_entries <= 0 or type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("Cache capacities must be positive integers")
        self._entries: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._bytes = 0
        self._lock = RLock()
        self._clock = clock
        self.max_entries, self.max_bytes = max_entries, max_bytes

    def _delete(self, key: str) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._bytes -= len(entry[1])

    def _expire(self) -> None:
        now = self._clock()
        for key, (expires, _) in list(self._entries.items()):
            if now >= expires:
                self._delete(key)

    def get(self, key: str) -> bytes | None:
        with self._lock:
            self._expire()
            entry = self._entries.get(key)
            if entry is None:
                return None
            self._entries.move_to_end(key)
            return entry[1]

    def set(self, key: str, value: bytes, *, ttl_seconds: int) -> None:
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 3600 or type(value) is not bytes:
            raise ValueError("Cache requires bytes and a TTL between 1 and 3600 seconds")
        with self._lock:
            self._expire()
            self._delete(key)
            if len(value) > self.max_bytes:
                return
            self._entries[key] = (self._clock() + ttl_seconds, value)
            self._bytes += len(value)
            while len(self._entries) > self.max_entries or self._bytes > self.max_bytes:
                oldest = next(iter(self._entries))
                self._delete(oldest)

    def delete(self, key: str) -> None:
        with self._lock:
            self._delete(key)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._bytes = 0

    def configure(self, *, max_entries: int, max_bytes: int) -> None:
        with self._lock:
            self.max_entries, self.max_bytes = max_entries, max_bytes
            self._expire()
            while len(self._entries) > self.max_entries or self._bytes > self.max_bytes:
                self._delete(next(iter(self._entries)))
