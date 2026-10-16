from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from contextvars import ContextVar
from inspect import isawaitable
from time import monotonic
from typing import Any, TYPE_CHECKING

from ..domain.exceptions import BudgetExceeded, ExecutionTimeout
from ..domain.models import CallContext, Usage
from .deadlines import current_deadline, inherited_deadline

if TYPE_CHECKING:
    from .runtime import RunContext, _AdmittedCall

current_call: ContextVar[CallContext | None] = ContextVar("agentguard_call", default=None)


class CallLifecycle:
    """One terminal event and one cleanup, regardless of callback failures."""

    def __init__(self, context: RunContext, admitted: _AdmittedCall, metadata: CallContext) -> None:
        self.context, self.admitted, self.metadata = context, admitted, metadata
        self.deadline = inherited_deadline(context.deadline).child(context.config.timeout.llm_seconds, "llm")
        self.done = False
        self.started = monotonic()
        self.resource: Any = None
        self.output_tokens = 0
        self.provider_usage: Usage | None = None
        self.stream_started = False
        from .retries import current_attempt
        attempt = current_attempt.get()
        self.retry_attempt = attempt if attempt is not None and attempt.context is context and attempt.call is None else None
        if self.retry_attempt is not None:
            self.retry_attempt.call = self
        context._active_calls[metadata.request_id] = self

    def attach(self, resource: Any) -> Any:
        self.resource = resource
        self.context._pending[self.metadata.request_id] = resource
        return resource

    def finish(self, *, error: BaseException | None = None, response: Any = None, stream: bool = False) -> Any:
        if self.done:
            return response
        self.done = True
        self.context._pending.pop(self.metadata.request_id, None)
        self.context._active_calls.pop(self.metadata.request_id, None)
        from .runtime import _extract_usage, _resolve_usage
        usage = None
        try:
            # Interceptors may replace the returned value, but cannot replace
            # the provider's billable usage with their own fabricated value.
            original_usage = _extract_usage(response) if error is None and not stream else None
            if error is None and not stream:
                response = self.context.interceptors.after_response(response)
            if error is None or (stream and self.stream_started) or (
                self.retry_attempt is not None and self.retry_attempt.provider_error is error
            ):
                actual = (self.provider_usage or Usage(input_tokens=self.admitted.estimated_usage.input_tokens,
                          output_tokens=self.output_tokens, source="estimate")) if stream else original_usage or self.admitted.estimated_usage
                if error is not None and not stream:
                    self.context.state.record("budget.failed_attempt_estimated", request_id=self.metadata.request_id,
                                              total_tokens=actual.total_tokens, usage_source="estimate")
                usage = _resolve_usage(self.context, self.admitted.estimated_usage, actual, self.admitted.model)
                cost = self.context.pricing.estimate(self.admitted.model, usage)
                self.admitted.reservation.settle(usage, self.admitted.estimated_cost if cost is None else cost)
        except BaseException as processing_error:
            if error is None:
                error = processing_error
        finally:
            try:
                self.admitted.admission.release()
            finally:
                self.admitted.reservation.release()
        event = "llm.completed" if error is None else "llm.failed"
        fields = dict(request_id=self.metadata.request_id, parent_id=self.metadata.parent_id,
                      duration_seconds=monotonic() - self.started,
                      outcome="completed" if error is None else "timed_out" if isinstance(error, ExecutionTimeout)
                      else "cancelled" if isinstance(error, (asyncio.CancelledError, GeneratorExit)) else "failed")
        if usage is not None:
            fields.update(total_tokens=usage.total_tokens, usage_source=usage.source)
        if isinstance(error, ExecutionTimeout):
            fields["reason_code"] = error.reason_code
            self.context.state.record("llm.timed_out", **fields)
        self.context.state.record(event, **fields)
        if error is None:
            self.context.circuits.record_success(self.admitted.circuit_key)
        elif not isinstance(error, (asyncio.CancelledError, GeneratorExit, ExecutionTimeout)):
            self.context.circuits.record_failure(self.admitted.circuit_key, error)
        try:
            if error is not None:
                try:
                    self.context.interceptors.on_error(error)
                except BaseException:
                    pass
        finally:
            try:
                self.context.interceptors.on_finish(self.metadata)
            except BaseException:
                if error is None:
                    raise
            finally:
                if self.context._automatic and not self.context._active_calls and not self.context._retry_sessions:
                    self.context._complete(error)
        if error is not None:
            raise error
        return response

    def abort(self) -> None:
        if not self.done:
            try:
                self.finish(error=GeneratorExit(), stream=True)
            except GeneratorExit:
                pass

    def chunk(self, chunk: Any) -> Any:
        from .runtime import _extract_usage, _stream_token_count
        self.stream_started = True
        usage = _extract_usage(chunk)
        if usage.source != "unknown":
            self.provider_usage = usage
            self.output_tokens = usage.output_tokens  # Provider usage is cumulative.
        else:
            self.output_tokens += _stream_token_count(chunk)
        limit = self.context.config.token.run_token_budget
        input_tokens = self.provider_usage.input_tokens if self.provider_usage else self.admitted.estimated_usage.input_tokens
        if limit is not None and self.context.state.total_tokens + input_tokens + self.output_tokens > limit:
            self.context.state.record("budget.stream_stopped", action=self.context.config.token.on_exceeded,
                                      actual_tokens=self.context.state.total_tokens + input_tokens + self.output_tokens, limit=limit)
            raise BudgetExceeded("Stream token budget exceeded", reason_code="stream_token_budget")
        if not getattr(self, "_first_token", False):
            self._first_token = True
            self.context.state.record("llm.first_token", request_id=self.metadata.request_id,
                                      duration_seconds=monotonic() - self.started)
        return self.context.interceptors.on_chunk(chunk, self.metadata)


class SyncStream:
    def __init__(self, target: Any, call: CallLifecycle) -> None:
        self.target, self.call = target, call
        self.iterator = None
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if self.closed or self.call.done:
            raise StopIteration
        token = current_call.set(self.call.metadata)
        deadline_token = current_deadline.set(self.call.deadline)
        from .runtime import _current_run
        run_token = _current_run.set(self.call.context)
        try:
            self.call.deadline.check()
            if self.iterator is None:
                self.iterator = iter(self.target)
            chunk = next(self.iterator)
            self.call.deadline.check()
            return self.call.chunk(chunk)
        except StopIteration:
            self._finish()
            raise
        except BaseException as error:
            self._finish(error)
            raise
        finally:
            current_call.reset(token)
            current_deadline.reset(deadline_token)
            _current_run.reset(run_token)

    def _finish(self, error: BaseException | None = None):
        if self.closed:
            return
        self.closed = True
        try:
            close = getattr(self.target, "close", None)
            if close:
                close()
            if self.iterator is not None and self.iterator is not self.target:
                close = getattr(self.iterator, "close", None)
                if close:
                    close()
        except BaseException as close_error:
            if error is None:
                error = close_error
        self.call.finish(error=error, stream=True)

    def close(self):
        try:
            self._finish(GeneratorExit())
        except GeneratorExit:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class AsyncStream:
    def __init__(self, target: Any, call: CallLifecycle) -> None:
        self.target, self.call = target, call
        self.iterator = None
        self.closed = False
        self.task: asyncio.Task | None = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed or self.call.done:
            raise StopAsyncIteration
        self.task = asyncio.current_task()
        token = current_call.set(self.call.metadata)
        deadline_token = current_deadline.set(self.call.deadline)
        from .runtime import _current_run
        run_token = _current_run.set(self.call.context)
        try:
            self.call.deadline.check()
            if self.iterator is None:
                self.iterator = self.target.__aiter__()
            chunk = await self.call.deadline.wait(self.iterator.__anext__())
            self.call.deadline.check()
            return self.call.chunk(chunk)
        except StopAsyncIteration:
            await self._finish()
            raise
        except BaseException as error:
            await self._finish(error)
            raise
        finally:
            self.task = None
            current_call.reset(token)
            current_deadline.reset(deadline_token)
            _current_run.reset(run_token)

    async def _finish(self, error: BaseException | None = None):
        if self.closed:
            return
        self.closed = True
        try:
            for target in (self.target,) if self.iterator is None or self.iterator is self.target else (self.iterator, self.target):
                close = getattr(target, "aclose", None)
                if close:
                    await close()
        except BaseException as close_error:
            if error is None:
                error = close_error
        self.call.finish(error=error, stream=True)

    async def aclose(self):
        task = self.task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        try:
            await self._finish(GeneratorExit())
        except GeneratorExit:
            pass

    def close(self):
        self.call.abort()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.aclose())
        else:
            loop.create_task(self.aclose())

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()


class PendingResponse(Coroutine):
    """A create_task-compatible coroutine that also closes before first await."""

    def __init__(self, target: Any, call: CallLifecycle) -> None:
        self.target, self.call = target, call
        self.task: asyncio.Task | None = None
        self.runner = self._run()
        self.finished = False

    async def _run(self):
        self.task = asyncio.current_task()
        token = current_call.set(self.call.metadata)
        deadline_token = current_deadline.set(self.call.deadline)
        from .runtime import _current_run
        run_token = _current_run.set(self.call.context)
        try:
            self.call.deadline.check()
            try:
                response = await self.call.deadline.wait(self.target)
            except BaseException as error:
                if self.call.retry_attempt is not None:
                    self.call.retry_attempt.provider_error = error
                raise
            try:
                self.call.deadline.check()
            except ExecutionTimeout:
                try:
                    close = getattr(response, "aclose", None)
                    if callable(close):
                        await close()
                    else:
                        close = getattr(response, "close", None)
                        if callable(close):
                            close()
                except BaseException:
                    self.call.context.state.record("llm.cleanup_failed", request_id=self.call.metadata.request_id)
                raise
            return dispatch(response, self.call)
        except BaseException as error:
            if not self.call.done:
                self.call.finish(error=error)
            raise
        finally:
            self.finished = True
            current_call.reset(token)
            current_deadline.reset(deadline_token)
            if hasattr(self.target, "close"):
                try:
                    self.target.close()
                except BaseException:
                    pass
            _current_run.reset(run_token)

    def __await__(self):
        return self

    def __iter__(self):
        return self

    def __next__(self):
        return self.send(None)

    def send(self, value):
        return self.runner.send(value)

    def throw(self, *args):
        return self.runner.throw(*args)

    def close(self):
        if self.finished:
            return
        if self.task is None:
            try:
                self.task = next((task for task in asyncio.all_tasks() if task.get_coro() is self), None)
            except RuntimeError:
                pass
        if self.task is not None and not self.task.done():
            self.task.cancel()
        else:
            self.runner.close()
            close = getattr(self.target, "close", None)
            if close:
                close()
        self.call.abort()
        self.finished = True

    async def aclose(self):
        self.close()
        if self.task is not None and self.task is not asyncio.current_task():
            await asyncio.gather(self.task, return_exceptions=True)


def dispatch(result: Any, call: CallLifecycle) -> Any:
    if isawaitable(result):
        return call.attach(PendingResponse(result, call))
    if call.admitted.kwargs.get("stream") is True:
        if hasattr(result, "__aiter__"):
            return call.attach(AsyncStream(result, call))
        if hasattr(result, "__iter__"):
            return call.attach(SyncStream(result, call))
    return call.finish(response=result)
