from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..domain.exceptions import AgentGuardError


class LoopDetected(AgentGuardError):
    """Agent 重复执行同一工具动作过多次时抛出。"""


@dataclass
class BehaviorFingerprint:
    max_steps: int | None = None
    max_repeated_calls: int = 3
    steps: int = 0
    _calls: dict[str, int] = field(default_factory=dict)

    def check(self, tool_name: str, arguments: Any = None) -> None:
        self.steps += 1
        if self.max_steps is not None and self.steps > self.max_steps:
            raise LoopDetected("Agent 步数已超过上限")
        key = f"{tool_name}:{repr(arguments)}"
        self._calls[key] = self._calls.get(key, 0) + 1
        if self._calls[key] > self.max_repeated_calls:
            raise LoopDetected(f"检测到重复的工具调用：{tool_name}")


class ToolGuard:
    def __init__(self, fingerprint: BehaviorFingerprint | None = None, max_calls: int | None = None) -> None:
        self.fingerprint = fingerprint or BehaviorFingerprint()
        self.max_calls = max_calls
        self.calls = 0

    def wrap(self, tool_name: str, function: Callable[..., Any]) -> Callable[..., Any]:
        def guarded(*args: Any, **kwargs: Any) -> Any:
            if self.max_calls is not None and self.calls >= self.max_calls:
                raise LoopDetected("工具调用次数已超过上限")
            self.fingerprint.check(tool_name, {"args": args, "kwargs": kwargs})
            self.calls += 1
            return function(*args, **kwargs)
        return guarded
