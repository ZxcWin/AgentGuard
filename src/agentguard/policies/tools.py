from __future__ import annotations

from dataclasses import dataclass, field
from functools import wraps
import inspect
from hashlib import sha256
from threading import RLock
from typing import Any, Callable

from ..domain.exceptions import AgentGuardError


class LoopDetected(AgentGuardError):
    """Agent 重复执行同一工具动作过多次时抛出。"""


class ToolApprovalDenied(AgentGuardError):
    """工具审批缺失、拒绝或超时。"""


@dataclass
class BehaviorFingerprint:
    max_steps: int | None = None
    max_repeated_calls: int | None = 3
    steps: int = 0
    _calls: dict[str, int] = field(default_factory=dict)
    _lock: Any = field(default_factory=RLock, repr=False, compare=False)

    def __post_init__(self) -> None:
        _validate_limit(self.max_steps, "max_steps")
        _validate_limit(self.max_repeated_calls, "max_repeated_calls")

    def check(self, tool_name: str, arguments: Any = None) -> None:
        key = sha256(f"{tool_name}:{_stable_repr(arguments)}".encode()).hexdigest() if self.max_repeated_calls is not None else ""
        with self._lock:
            if self.max_steps is not None and self.steps >= self.max_steps:
                raise LoopDetected("Agent 步数已超过上限")
            if self.max_repeated_calls is not None and self._calls.get(key, 0) >= self.max_repeated_calls:
                raise LoopDetected(f"检测到重复的工具调用：{tool_name}")
            self.steps += 1
            if self.max_repeated_calls is not None:
                self._calls[key] = self._calls.get(key, 0) + 1


class ToolGuard:
    def __init__(self, fingerprint: BehaviorFingerprint | None = None, max_calls: int | None = None,
                 per_tool_limits: dict[str, int] | None = None) -> None:
        _validate_limit(max_calls, "max_calls")
        self.fingerprint = fingerprint or BehaviorFingerprint()
        self.max_calls = max_calls
        self.calls = 0
        self._lock = RLock()
        self.per_tool_limits = dict(per_tool_limits or {})
        for value in self.per_tool_limits.values():
            _validate_limit(value, "per_tool_limits", optional=False)
        self.tool_calls: dict[str, int] = {}

    def admit(self, tool_name: str, function: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        try:
            parameters = inspect.signature(function)
        except (TypeError, ValueError):
            arguments = {"args": args, "kwargs": kwargs}
        else:
            bound = parameters.bind(*args, **kwargs)
            bound.apply_defaults()
            arguments = bound.arguments
        self._admit(tool_name, arguments)

    def wrap(self, tool_name: str, function: Callable[..., Any]) -> Callable[..., Any]:
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise ValueError("tool_name 必须是非空字符串")
        try:
            parameters = inspect.signature(function)
        except (TypeError, ValueError):
            parameters = None

        def admit(args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
            if parameters is None:
                self._admit(tool_name, {"args": args, "kwargs": kwargs})
                return
            bound = parameters.bind(*args, **kwargs)
            bound.apply_defaults()
            self._admit(tool_name, bound.arguments)

        if inspect.iscoroutinefunction(function):
            @wraps(function)
            async def guarded_async(*args: Any, **kwargs: Any) -> Any:
                admit(args, kwargs)
                return await function(*args, **kwargs)
            return guarded_async

        @wraps(function)
        def guarded(*args: Any, **kwargs: Any) -> Any:
            admit(args, kwargs)
            return function(*args, **kwargs)
        return guarded

    def _admit(self, tool_name: str, arguments: Any) -> None:
        with self._lock:
            if self.max_calls is not None and self.calls >= self.max_calls:
                raise LoopDetected("工具调用次数已超过上限")
            if tool_name in self.per_tool_limits and self.tool_calls.get(tool_name, 0) >= self.per_tool_limits[tool_name]:
                raise LoopDetected("Per-tool call limit exceeded")
            self.fingerprint.check(tool_name, arguments)
            self.calls += 1
            self.tool_calls[tool_name] = self.tool_calls.get(tool_name, 0) + 1


def _validate_limit(value: Any, name: str, *, optional: bool = True) -> None:
    if value is None and optional:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} 必须是非负整数")


def _stable_repr(value: Any, active: set[int] | None = None) -> str:
    active = set() if active is None else active
    if type(value) in (str, int, float, bool, bytes, type(None)):
        return f"{type(value).__name__}:{value!r}"
    if type(value) not in (dict, list, tuple, set, frozenset):
        raise ValueError("工具参数包含不支持的类型")
    identity = id(value)
    if identity in active:
        raise ValueError("工具参数不能包含循环引用")
    active.add(identity)
    try:
        if isinstance(value, dict):
            items = sorted(
                (_stable_repr(key, active), _stable_repr(item, active))
                for key, item in value.items()
            )
        else:
            items = [_stable_repr(item, active) for item in value]
            if isinstance(value, (set, frozenset)):
                items.sort()
        return f"{type(value).__name__}:{items!r}"
    finally:
        active.remove(identity)
