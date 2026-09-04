from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..domain.exceptions import AgentGuardError


class LoopDetected(AgentGuardError):
    """Raised when an Agent repeats the same tool action too often."""


@dataclass
class BehaviorFingerprint:
    max_steps: int | None = None
    max_repeated_calls: int = 3
    steps: int = 0
    _calls: dict[str, int] = field(default_factory=dict)

    def check(self, tool_name: str, arguments: Any = None) -> None:
        self.steps += 1
        if self.max_steps is not None and self.steps > self.max_steps:
            raise LoopDetected("maximum Agent steps exceeded")
        key = f"{tool_name}:{repr(arguments)}"
        self._calls[key] = self._calls.get(key, 0) + 1
        if self._calls[key] > self.max_repeated_calls:
            raise LoopDetected(f"repeated tool call detected: {tool_name}")


class ToolGuard:
    def __init__(self, fingerprint: BehaviorFingerprint | None = None, max_calls: int | None = None) -> None:
        self.fingerprint = fingerprint or BehaviorFingerprint()
        self.max_calls = max_calls
        self.calls = 0

    def wrap(self, tool_name: str, function: Callable[..., Any]) -> Callable[..., Any]:
        def guarded(*args: Any, **kwargs: Any) -> Any:
            if self.max_calls is not None and self.calls >= self.max_calls:
                raise LoopDetected("maximum tool calls exceeded")
            self.fingerprint.check(tool_name, {"args": args, "kwargs": kwargs})
            self.calls += 1
            return function(*args, **kwargs)
        return guarded
