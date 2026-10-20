"""Ordinary telemetry sinks. These are not a durable audit outbox."""
from __future__ import annotations

from collections import deque
import json
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Mapping

from ..domain.events import operational_event


class MemoryEventSink:
    def __init__(self, max_events: int = 1000) -> None:
        if type(max_events) is not int or not 1 <= max_events <= 100000:
            raise ValueError("max_events must be between 1 and 100000")
        self._events: deque = deque(maxlen=max_events)
        self._lock = RLock()

    def emit(self, event: Mapping[str, Any]) -> None:
        with self._lock:
            self._events.append(operational_event(event))

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(event) for event in self._events]


class CallbackEventSink:
    def __init__(self, callback: Callable[[dict[str, Any]], None]) -> None:
        self.callback = callback

    def emit(self, event: Mapping[str, Any]) -> None:
        self.callback(operational_event(event))


class CallbackBatchEventSink:
    """One bounded callback per available dispatcher batch; no automatic retry."""
    def __init__(self, callback):
        if not callable(callback):
            raise ValueError("Batch callback must be callable")
        self.callback = callback

    def emit(self, event):
        self.emit_batch([event])

    def emit_batch(self, events):
        if not isinstance(events, (list, tuple)) or not 1 <= len(events) <= 1000:
            raise ValueError("Batch must contain between 1 and 1000 events")
        self.callback([operational_event(event) for event in events])


class JSONLEventSink:
    """Append one JSON line per event, with bounded rotating local files.

    Flushes Python/OS buffers at each write; no fsync or crash-durability promise.
    One writer process per path. Use a buffered dispatcher for runtime delivery.
    """
    def __init__(self, path: str | Path, *, max_bytes: int = 10 * 1024 * 1024, backups: int = 2) -> None:
        if type(max_bytes) is not int or not 65536 <= max_bytes <= 1024**3:
            raise ValueError("max_bytes must be between 64 KiB and 1 GiB")
        if type(backups) is not int or not 1 <= backups <= 10:
            raise ValueError("backups must be between 1 and 10")
        self.path, self.max_bytes, self.backups = Path(path), max_bytes, backups
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def emit(self, event: Mapping[str, Any]) -> None:
        line = json.dumps(operational_event(event), ensure_ascii=True, allow_nan=False) + "\n"
        with self._lock:
            if self.path.exists() and self.path.stat().st_size + len(line.encode()) > self.max_bytes:
                for index in range(self.backups, 0, -1):
                    source = self.path if index == 1 else Path(f"{self.path}.{index - 1}")
                    if source.exists():
                        source.replace(Path(f"{self.path}.{index}"))
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line)
