from __future__ import annotations

import asyncio
from dataclasses import dataclass
from contextvars import ContextVar
from time import monotonic
from typing import Any, Awaitable

from ..domain.exceptions import ExecutionTimeout


@dataclass(frozen=True)
class Deadline:
    expires_at: float | None = None
    scope: str = "run"

    def child(self, seconds: float | None, scope: str) -> "Deadline":
        candidate = None if seconds is None else monotonic() + seconds
        if self.expires_at is not None and (candidate is None or self.expires_at <= candidate):
            return self
        return Deadline(candidate, scope)

    def remaining(self) -> float | None:
        return None if self.expires_at is None else max(0.0, self.expires_at - monotonic())

    def check(self) -> None:
        if self.expires_at is not None and monotonic() >= self.expires_at:
            raise ExecutionTimeout(self.scope)

    async def wait(self, awaitable: Awaitable[Any]) -> Any:
        remaining = self.remaining()
        if remaining is None:
            return await awaitable
        # asyncio.wait_for cancels and drains cooperative provider coroutines.
        # Synchronous providers are checked before/after their call, never put in
        # background threads where side effects could continue invisibly.
        task = asyncio.ensure_future(awaitable)
        try:
            return await asyncio.wait_for(task, timeout=remaining)
        except asyncio.TimeoutError as error:
            # wait_for also propagates TimeoutError raised by the provider.
            # Only its own cancellation represents our execution deadline.
            if task.done() and not task.cancelled():
                raise
            raise ExecutionTimeout(self.scope) from error


current_deadline: ContextVar[Deadline | None] = ContextVar("agentguard_deadline", default=None)


def inherited_deadline(run_deadline: Deadline) -> Deadline:
    parent = current_deadline.get()
    if parent is not None and parent.expires_at is not None and (
        run_deadline.expires_at is None or parent.expires_at < run_deadline.expires_at
    ):
        return parent
    return run_deadline
