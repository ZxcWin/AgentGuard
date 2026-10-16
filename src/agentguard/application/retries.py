from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from inspect import isawaitable, iscoroutinefunction
from random import uniform
from time import sleep
from typing import Any

from ..policies.retry import retry_after, retry_reason
from .deadlines import current_deadline, inherited_deadline


@dataclass
class Attempt:
    context: Any
    number: int
    tools_before: int
    call: Any = None
    provider_error: BaseException | None = None


current_attempt: ContextVar[Attempt | None] = ContextVar("agentguard_retry_attempt", default=None)


class RetrySession:
    def __init__(self, context, function, args, kwargs, purpose):
        self.context, self.function, self.args, self.kwargs, self.purpose = context, function, args, kwargs, purpose
        self.config = context.config.retry
        self.deadline = inherited_deadline(context.deadline).child(context.config.timeout.llm_seconds, "llm").child(self.config.max_elapsed_seconds, "retry")

    def invoke(self, attempt):
        from .runtime import _invoke_once
        self.context._check()
        self.deadline.check()
        token = current_attempt.set(attempt)
        deadline_token = current_deadline.set(self.deadline)
        try:
            return _invoke_once(self.context, self.function, *self.args,
                                budget_purpose=self.purpose if attempt.number == 1 else "retry", **self.kwargs)
        finally:
            current_attempt.reset(token)
            current_deadline.reset(deadline_token)

    def delay(self, error, attempt):
        call = attempt.call
        fields = dict(attempt=attempt.number, request_id=call.metadata.request_id if call else None)
        reason = retry_reason(error)
        stopped = None
        if attempt.provider_error is not error or reason is None:
            stopped = "not_retryable"
        elif call is not None and call.admitted.kwargs.get("stream") is True:
            stopped = "streaming"
        elif self.context.state.tool_calls != attempt.tools_before:
            stopped = "nested_tool_executed"
        elif attempt.number > self.config.max_retries:
            stopped = "max_retries"
        base = min(self.config.max_delay_seconds, self.config.initial_delay_seconds * 2 ** (attempt.number - 1))
        delay = uniform(0, base) if self.config.jitter else base
        server_delay = retry_after(error, datetime.now(timezone.utc)) if stopped is None else None
        if server_delay is not None:
            delay = max(delay, server_delay)
        remaining = self.deadline.remaining()
        if stopped is None and delay > self.config.max_delay_seconds:
            stopped = "retry_after_limit"
        if stopped is None and remaining is not None and delay >= remaining:
            stopped = "retry_time_budget"
        if stopped is not None:
            self.context.state.record("llm.retry_stopped", **fields, reason_code=stopped)
            return None
        self.context.state.record("llm.retry_scheduled", **fields, reason_code=reason, delay_seconds=delay)
        return delay

    async def async_run(self, first=None, initial_attempt=None):
        self.context._retry_sessions += 1
        try:
            starting_number = initial_attempt.number if initial_attempt is not None else 1
            for number in range(starting_number, self.config.max_retries + 2):
                attempt = initial_attempt if number == starting_number and initial_attempt is not None else Attempt(self.context, number, self.context.state.tool_calls)
                token = current_attempt.set(attempt)
                deadline_token = current_deadline.set(self.deadline)
                try:
                    try:
                        result = first if number == starting_number and initial_attempt is not None else self.invoke(attempt)
                        return await result if isawaitable(result) else result
                    except Exception as error:
                        delay = self.delay(error, attempt)
                        if delay is None:
                            raise
                finally:
                    current_attempt.reset(token)
                    current_deadline.reset(deadline_token)
                await self.deadline.wait(asyncio.sleep(delay))
        finally:
            self.context._retry_sessions -= 1

    def sync_run(self):
        self.context._retry_sessions += 1
        try:
            for number in range(1, self.config.max_retries + 2):
                attempt = Attempt(self.context, number, self.context.state.tool_calls)
                try:
                    result = self.invoke(attempt)
                    if isawaitable(result):
                        # The sync callable has already produced its first
                        # coroutine. Track it through the Run's lazy cleanup.
                        return self.context._defer(lambda: self.async_run(result, attempt), target=result)
                    return result
                except Exception as error:
                    delay = self.delay(error, attempt)
                    if delay is None:
                        raise
                sleep(delay)
                self.deadline.check()
        finally:
            self.context._retry_sessions -= 1


def invoke_with_retry(context, function, args, kwargs, purpose):
    if iscoroutinefunction(function):
        async def execute():
            return await RetrySession(context, function, args, kwargs, purpose).async_run()
        return context._defer(execute)
    return RetrySession(context, function, args, kwargs, purpose).sync_run()
