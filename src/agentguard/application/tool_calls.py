from __future__ import annotations

import asyncio
import hashlib
from inspect import isawaitable, iscoroutinefunction, signature
from time import monotonic
from typing import Any

from ..domain.exceptions import ExecutionTimeout, RateLimitExceeded
from ..domain.models import CallContext
from ..policies.tools import LoopDetected, ToolApprovalDenied
from .lifecycle import current_call
from .deadlines import current_deadline, inherited_deadline
from .behavior import tool_stack


def result_summary(value: Any) -> dict[str, Any]:
    """Structural metadata only, never string/repr or user-defined methods."""
    kind = type(value)
    summary = {"result_type": kind.__name__ if kind in {str, bytes, int, float, bool, list, tuple, dict, set, type(None)} else "object"}
    if kind in {str, bytes, list, tuple, dict, set}:
        summary["result_size"] = len(value)
    return summary


def invoke_tool(context: Any, name: str, function: Any, args: tuple, kwargs: dict,
                *, idempotency_key: str | None = None, approval: Any = None, cache_options: Any = None) -> Any:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("tool_name must be a nonempty string")
    # Async tool admission and side effects happen when awaited, never at creation.
    if iscoroutinefunction(function):
        async def execute():
            context._check()
            deadline = inherited_deadline(context.deadline).child(context.config.timeout.tool_seconds, "tool")
            key = "|".join(context.scope_key(tool=name))
            wait_end = monotonic() + context.config.rate_limit.wait_timeout_seconds
            while True:
                deadline.check()
                try:
                    lease = context.admission.acquire(key=key, tool=True, wait=0)
                    break
                except RateLimitExceeded:
                    if monotonic() >= wait_end:
                        context.state.record("rate_limit.rejected", tool=name, reason_code="tool_admission_timeout")
                        raise
                    remaining = deadline.remaining()
                    await asyncio.sleep(min(.01, max(0, wait_end - monotonic()), remaining if remaining is not None else .01))
            try:
                return await _invoke_async(context, name, function, args, kwargs, lease=lease, deadline=deadline,
                                           idempotency_key=idempotency_key, approval=approval, cache_options=cache_options)
            finally:
                lease.release()
        return context._defer(execute)
    return _invoke(context, name, function, args, kwargs, idempotency_key=idempotency_key, approval=approval,
                   cache_options=cache_options)


def _key_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _approval_call(approval: Any, name: str, key_hash: str | None) -> Any:
    try:
        parameters = signature(approval)
    except (TypeError, ValueError):
        return approval(name, key_hash)
    try:
        parameters.bind(name, key_hash)
    except TypeError:
        parameters.bind()
        return approval()
    return approval(name, key_hash)


def _reject(context: Any, name: str, reason: str, error: BaseException, *, key_hash: str | None = None) -> Any:
    if context.audit is not None:
        context.audit.decision(action="tool.reject", outcome="rejected", tool=name, reason_code=reason,
                               run_id=context.state.run_id, idempotency_key_hash=key_hash)
    context.state.record("tool.rejected", tool=name, reason_code=reason, idempotency_key_hash=key_hash,
                         execution_may_have_completed=False)
    raise error


def _remember_key(context: Any, name: str, key_hash: str | None) -> None:
    if key_hash is not None:
        with context._tool_lock:
            if key_hash in context._idempotency_keys:
                _reject(context, name, "duplicate_idempotency_key",
                        LoopDetected("检测到重复幂等键，已在副作用前阻断"), key_hash=key_hash)
            context._idempotency_keys[key_hash] = name


def _approval_failed(context: Any, name: str, key_hash: str | None, error: BaseException) -> None:
    outcome = ("timed_out" if isinstance(error, ExecutionTimeout) else
               "cancelled" if isinstance(error, (asyncio.CancelledError, GeneratorExit)) else
               "denied" if isinstance(error, ToolApprovalDenied) else "failed")
    context.state.record("tool.approval_resolved", tool=name, idempotency_key_hash=key_hash,
                         approval_outcome=outcome, approval_required=True)
    if context.audit is not None:
        context.audit.decision(action="tool.approval", outcome=outcome, tool=name, idempotency_key_hash=key_hash)
    _reject(context, name, f"approval_{outcome}", error, key_hash=key_hash)


def _preflight(context: Any, name: str, effect: str, *, idempotency_key: str | None, approval: Any,
               deadline: Any, async_mode: bool) -> dict[str, Any]:
    required_key = name in context.config.tools.idempotency_required_for
    if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 256):
        _reject(context, name, "invalid_idempotency_key", LoopDetected("幂等键必须是 1 到 256 个字符的字符串"))
    if required_key and idempotency_key is None:
        _reject(context, name, "missing_idempotency_key", LoopDetected("该工具执行前必须提供幂等键"))
    key_hash = _key_hash(idempotency_key) if idempotency_key is not None else None
    with context._tool_lock:
        if key_hash is not None and key_hash in context._idempotency_keys:
            _reject(context, name, "duplicate_idempotency_key",
                    LoopDetected("检测到重复幂等键，已在副作用前阻断"), key_hash=key_hash)
    approval_required = name in context.config.tools.approval_required_for
    if approval_required:
        if context.audit is not None:
            context.audit.decision(action="tool.approval", outcome="requested", tool=name, idempotency_key_hash=key_hash)
        context.state.record("tool.approval_requested", tool=name, idempotency_key_hash=key_hash, approval_required=True)
        try:
            if approval is None:
                raise ToolApprovalDenied("该工具执行前需要人工审批")
            approval_deadline = deadline.child(context.config.tools.approval_timeout_seconds, "approval")
            approval_deadline.check()
            decision = _approval_call(approval, name, key_hash)
            if isawaitable(decision):
                if not async_mode:
                    if hasattr(decision, "close"):
                        decision.close()
                    elif hasattr(decision, "cancel"):
                        decision.cancel()
                    raise ToolApprovalDenied("同步工具不能使用异步审批回调")
                return {"key_hash": key_hash, "approval_required": True,
                        "approval": approval_deadline.wait(decision)}
            approval_deadline.check()
            if decision is not True:
                raise ToolApprovalDenied("人工审批拒绝了工具执行")
        except BaseException as error:
            _approval_failed(context, name, key_hash, error)
        context.state.record("tool.approval_resolved", tool=name, idempotency_key_hash=key_hash,
                             approval_outcome="approved", approval_required=True)
        if context.audit is not None:
            context.audit.decision(action="tool.approval", outcome="approved", tool=name, idempotency_key_hash=key_hash)
    _remember_key(context, name, key_hash)
    return {"key_hash": key_hash, "approval_required": approval_required, "approval": None}


async def _invoke_async(context: Any, name: str, function: Any, args: tuple, kwargs: dict, *, lease: Any,
                        deadline: Any, idempotency_key: str | None, approval: Any, cache_options: Any) -> Any:
    effect = context.config.tools.side_effects.get(name, "write")
    preflight = _preflight(context, name, effect, idempotency_key=idempotency_key, approval=approval,
                           deadline=deadline, async_mode=True)
    if preflight["approval"] is not None:
        try:
            decision = await preflight["approval"]
            deadline.check()
            if decision is not True:
                raise ToolApprovalDenied("人工审批拒绝了工具执行")
        except BaseException as error:
            _approval_failed(context, name, preflight["key_hash"], error)
        context.state.record("tool.approval_resolved", tool=name, idempotency_key_hash=preflight["key_hash"],
                             approval_outcome="approved", approval_required=True)
        if context.audit is not None:
            context.audit.decision(action="tool.approval", outcome="approved", tool=name, idempotency_key_hash=preflight["key_hash"])
        _remember_key(context, name, preflight["key_hash"])
    result = _invoke(context, name, function, args, kwargs, lease=lease, deadline=deadline,
                     preflight=preflight, cache_options=cache_options)
    return await result if isawaitable(result) else result


def _invoke(context: Any, name: str, function: Any, args: tuple, kwargs: dict, *, lease: Any = None,
            deadline: Any = None, idempotency_key: str | None = None, approval: Any = None,
            preflight: dict[str, Any] | None = None, cache_options: Any = None) -> Any:
    context._check()
    deadline = deadline or inherited_deadline(context.deadline).child(context.config.timeout.tool_seconds, "tool")
    deadline.check()
    effect = context.config.tools.side_effects.get(name, "write")
    preflight = preflight or _preflight(context, name, effect, idempotency_key=idempotency_key, approval=approval,
                                       deadline=deadline, async_mode=False)
    deadline.check()
    try:
        context.tool_guard.admit(name, function, args, kwargs)
    except LoopDetected:
        context.state.record("tool.rejected", tool=name, reason_code="tool_limit")
        raise
    key = "|".join(context.scope_key(tool=name))
    try:
        admission = lease or context.admission.acquire(key=key, tool=True, wait=context._wait_budget(deadline))
    except RateLimitExceeded:
        deadline.check()
        context.state.record("rate_limit.rejected", reason_code="tool_admission_timeout", tool=name)
        raise
    circuit_key = context.circuit_key(tool=name)
    try:
        context.circuits.before_call(circuit_key)
    except BaseException:
        admission.release()
        context.state.record("circuit.rejected", reason_code="circuit_open", tool=name)
        raise
    try:
        context.behavior.acquire_tool(context.config.tools)
    except LoopDetected:
        admission.release()
        context.circuits.abandon_probe(circuit_key)
        context.state.record("tool.rejected", tool=name, reason_code="tool_behavior_limit")
        raise
    parent = current_call.get()
    metadata = CallContext(call_type="tool", run_id=context.state.run_id, trace_id=context.state.trace_id,
                           parent_id=parent.request_id if parent and parent.run_id == context.state.run_id else None)
    started = monotonic()
    fields = {"tool": name, "request_id": metadata.request_id, "parent_id": metadata.parent_id,
              "run_id": metadata.run_id, "trace_id": metadata.trace_id, "side_effect": effect,
              "idempotency_key_hash": preflight["key_hash"],
              "approval_required": preflight["approval_required"],
              "approval_outcome": "approved" if preflight["approval_required"] else None}
    audit_operation = None
    try:
        if context.audit is not None:
            audit_operation = context.audit.begin(action="tool.invoke", config_version=context.config_version, **fields)
    except BaseException:
        admission.release()
        context.circuits.abandon_probe(circuit_key)
        context.behavior.release_tool()
        raise
    context.state.tool_calls += 1
    context.state.record("tool.started", **fields)
    from .tool_caching import prepare_tool_cache
    cache_ticket = prepare_tool_cache(context, metadata, name, function, args, kwargs, cache_options,
                                      effect=effect, preflight=preflight)
    done = False

    def finish(error: BaseException | None = None, result: Any = None):
        nonlocal done
        if done:
            return
        done = True
        context._pending.pop(metadata.request_id, None)
        admission.release()
        context.behavior.release_tool()
        terminal = dict(fields, duration_seconds=monotonic() - started,
                        outcome="completed" if error is None else "timed_out" if isinstance(error, ExecutionTimeout)
                        else "cancelled" if isinstance(error, (asyncio.CancelledError, GeneratorExit)) else "failed")
        if error is None:
            context.behavior.output(result)
            if cache_ticket is not None and cache_ticket.hit:
                context.circuits.abandon_probe(circuit_key)
            else:
                context.circuits.record_success(circuit_key)
            context.state.record("tool.completed", **terminal, **result_summary(result))
        else:
            if not isinstance(error, (asyncio.CancelledError, ExecutionTimeout, GeneratorExit)):
                context.circuits.record_failure(circuit_key, error)
            context.state.record("tool.failed", **terminal)
            if isinstance(error, (asyncio.CancelledError, ExecutionTimeout, GeneratorExit)) or not context.circuits.is_provider_failure(error):
                context.circuits.abandon_probe(circuit_key)
            if isinstance(error, ExecutionTimeout):
                context.state.record("tool.timed_out", **terminal, reason_code=error.reason_code,
                                      execution_may_have_completed=effect != "read")
        context.state.record("tool.finished", **fields, duration_seconds=monotonic() - started)
        if audit_operation is not None:
            context.audit.end(audit_operation, outcome=terminal["outcome"],
                              execution_may_have_completed=effect != "read", **fields)
        if error is None and cache_ticket is not None:
            cache_ticket.publish(result)

    token = current_call.set(metadata)
    stack = tool_stack.get() + (name,)
    stack_token = tool_stack.set(stack)
    deadline_token = current_deadline.set(deadline)
    try:
        deadline.check()
        if context.policy_valid is not None:
            context.policy_valid()
        if cache_ticket is not None:
            from .caching import MISS
            cached = cache_ticket.read()
            if cached is not MISS:
                fields["kind"] = "cache"
                deadline.check()
                if context.policy_valid is not None:
                    context.policy_valid()
                finish(result=cached)
                return cached
        deadline.check()
        result = function(*args, **kwargs)
        if not isawaitable(result):
            deadline.check()
            finish(result=result)
            return result
    except BaseException as error:
        finish(error)
        raise
    finally:
        current_call.reset(token)
        tool_stack.reset(stack_token)
        current_deadline.reset(deadline_token)

    async def consume():
        from .runtime import _current_run
        run_token = _current_run.set(context)
        token = current_call.set(metadata)
        stack_token = tool_stack.set(stack)
        deadline_token = current_deadline.set(deadline)
        try:
            deadline.check()
            response = await deadline.wait(result)
            deadline.check()
            finish(result=response)
            return response
        except BaseException as error:
            finish(error)
            raise
        finally:
            current_call.reset(token)
            tool_stack.reset(stack_token)
            current_deadline.reset(deadline_token)
            _current_run.reset(run_token)
            if hasattr(result, "close"):
                result.close()

    # Reuse the coroutine protocol adapter for deterministic pre-await cleanup.
    resource = ToolPending(consume(), result, finish)
    context._pending[metadata.request_id] = resource
    return resource


from collections.abc import Coroutine


class ToolPending(Coroutine):
    def __init__(self, runner, target, finish):
        self.runner, self.target, self.finish = runner, target, finish
        self.task = None
        self.done = False

    def __await__(self): return self
    def __iter__(self): return self
    def __next__(self): return self.send(None)

    def send(self, value):
        self.task = asyncio.current_task()
        try:
            return self.runner.send(value)
        except BaseException:
            self.done = True
            raise

    def throw(self, *args):
        try:
            return self.runner.throw(*args)
        except BaseException:
            self.done = True
            raise

    def close(self):
        if self.done:
            return
        self.done = True
        if self.task is None:
            try:
                self.task = next((task for task in asyncio.all_tasks() if task.get_coro() is self), None)
            except RuntimeError:
                pass
        if self.task is not None and not self.task.done():
            self.task.cancel()
        else:
            self.runner.close()
            if hasattr(self.target, "close"):
                self.target.close()
        self.finish(GeneratorExit())

    async def aclose(self):
        self.close()
        if self.task is not None and self.task is not asyncio.current_task():
            await asyncio.gather(self.task, return_exceptions=True)
