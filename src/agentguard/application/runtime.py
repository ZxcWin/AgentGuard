from __future__ import annotations

"""运行时调用管线和生命周期编排。

本模块负责执行顺序。公共调用方应从 ``agentguard.api`` 导入稳定 facade。
"""

from contextlib import AbstractContextManager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from functools import wraps
from inspect import isawaitable, signature
from time import monotonic
from threading import RLock
from typing import Any, Callable, TypeVar

from ..policies.budget import BudgetManager, estimate_tokens
from ..policies.cost import PricingTable
from ..policies.fallback import fallback_rejection
from ..config.loader import AgentGuardConfig
from ..config.manager import ConfigManager
from ..policies.rate_limit import AdmissionController, CircuitRegistry
from ..adapters.storage.redis import RedisAdmissionController
from .interceptors import BaseInterceptor, InterceptorChain
from ..domain.exceptions import BudgetExceeded, RateLimitExceeded
from ..domain.models import CallContext, RequestSnapshot, RunState, Usage
from ..ports import RunStore
import logging
import asyncio
from uuid import uuid4
from inspect import iscoroutinefunction
from .deadlines import Deadline, current_deadline, inherited_deadline
from .lifecycle import CallLifecycle, current_call, dispatch
from ..domain.exceptions import AgentGuardError, ExecutionTimeout


_current_run: ContextVar["RunContext | None"] = ContextVar("agentguard_run", default=None)
T = TypeVar("T")


def current_run() -> "RunContext | None":
    return _current_run.get()


@dataclass
class RunContext(AbstractContextManager["RunContext"]):
    config: AgentGuardConfig
    state: RunState
    config_version: int = 1
    run_store: RunStore | None = None

    def __post_init__(self) -> None:
        self.pricing = PricingTable.from_config(self.config.pricing)
        self.budget = BudgetManager(self.config, self.state, self.pricing)
        self.admission = AdmissionController(self.config.rate_limit)
        if self.config.rate_limit.backend == "redis":
            try:
                import importlib

                redis_module = importlib.import_module("redis")
            except ImportError as error:
                raise RuntimeError("Redis 后端需要安装 Redis 客户端") from error
            client = redis_module.Redis.from_url(self.config.rate_limit.redis_url)
            self.admission = RedisAdmissionController(client, self.config.rate_limit)
        self.circuits = CircuitRegistry()
        self.interceptors = InterceptorChain()
        self._token = None
        self._pending: dict[str, Any] = {}
        self._active_calls: dict[str, Any] = {}
        self._closed = False
        self._closing = False
        self._entered = False
        self._automatic = False
        self._retry_sessions = 0
        self._tool_lock = RLock()
        self._idempotency_keys: dict[str, str] = {}
        self.deadline = Deadline()
        from ..policies.tools import BehaviorFingerprint, ToolGuard
        tools = self.config.tools
        self.tool_guard = ToolGuard(BehaviorFingerprint(max_steps=tools.max_steps, max_repeated_calls=tools.max_repeated_calls),
                                    max_calls=tools.max_calls, per_tool_limits=dict(tools.per_tool_limits))

    def __enter__(self) -> "RunContext":
        if self._entered or self._closed:
            raise RuntimeError("Run contexts cannot be reused")
        self._entered = True
        parent = current_run()
        self.deadline = inherited_deadline(parent.deadline if parent else Deadline()).child(self.config.timeout.run_seconds, "run")
        self._token = _current_run.set(self)
        self.state.record("run.started", config_version=self.config_version)
        self._publish()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self._closing = True
        try:
            for resource in list(self._pending.values()):
                try:
                    resource.close()
                except BaseException:
                    self.state.record("run.cleanup_failed")
            completed_error = self._complete(exc)
            if exc is None and isinstance(completed_error, ExecutionTimeout):
                raise completed_error
        finally:
            self._detach()

    async def __aenter__(self) -> "RunContext":
        return self.__enter__()

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self._closing = True
        try:
            for resource in list(self._pending.values()):
                try:
                    if hasattr(resource, "aclose"):
                        await resource.aclose()
                    else:
                        resource.close()
                except BaseException:
                    self.state.record("run.cleanup_failed")
            completed_error = self._complete(exc)
            if exc is None and isinstance(completed_error, ExecutionTimeout):
                raise completed_error
        finally:
            self._detach()

    def _complete(self, error: BaseException | None = None) -> BaseException | None:
        if self._closed:
            return
        self._closed = True
        if error is None:
            try:
                self.deadline.check()
            except ExecutionTimeout as timeout:
                error = timeout
        self.state.status = ("cancelled" if isinstance(error, (asyncio.CancelledError, GeneratorExit)) else
                             "timed_out" if isinstance(error, ExecutionTimeout) else "failed" if error else "completed")
        self.state.record(f"run.{self.state.status}")
        self._publish()
        return error

    def _detach(self) -> None:
        if self._token is not None:
            _current_run.reset(self._token)
            self._token = None

    def _check(self) -> None:
        if self._closed or self._closing:
            raise RuntimeError("Cannot invoke a closed Run")
        self.deadline.check()

    def _defer(self, factory: Callable[..., Any], *, target: Any = None) -> Any:
        from .tool_calls import ToolPending
        key = f"pending_{uuid4().hex}"
        def finish(error: BaseException | None = None):
            self._pending.pop(key, None)
            if self._automatic and not self._closed and not self._pending and not self._retry_sessions:
                self._complete(error)
        async def execute():
            try:
                self._check()
                result = await factory()
                finish()
                return result
            except BaseException as error:
                finish(error)
                raise
        resource = ToolPending(execute(), target, finish)
        self._pending[key] = resource
        return resource

    def _publish(self) -> None:
        if self.run_store is not None:
            try:
                self.run_store.save(self.state, self.config_version)
            except Exception:
                logging.getLogger(__name__).warning("AgentGuard run storage failed")

    def invoke(
        self,
        function: Callable[..., T],
        *args: Any,
        budget_purpose: str = "primary",
        **kwargs: Any,
    ) -> T:
        return _invoke_guarded(self, function, *args, budget_purpose=budget_purpose, **kwargs)

    def add_interceptor(self, interceptor: BaseInterceptor) -> None:
        """向当前 Run 添加一个拦截器。"""
        self.interceptors.add(interceptor)

    def _wait_budget(self, deadline: Deadline) -> float:
        remaining = deadline.remaining()
        configured = self.config.rate_limit.wait_timeout_seconds
        return configured if remaining is None else min(configured, remaining)

    def invoke_tool(self, tool_name: str, function: Callable[..., T], *args: Any,
                    idempotency_key: str | None = None, approval: Callable[..., Any] | None = None,
                    **kwargs: Any) -> T:
        from .tool_calls import invoke_tool
        return invoke_tool(self, tool_name, function, args, kwargs,
                           idempotency_key=idempotency_key, approval=approval)

    def scope_key(self, *, model: str | None = None, tool: str | None = None) -> tuple[str, ...]:
        values = {
            "tenant_id": self.state.tenant_id,
            "project_id": self.state.project_id,
            "environment": self.state.environment,
            "agent_id": self.state.agent_id,
            "user_id": self.state.user_id,
            "model": model,
            "tool": tool,
        }
        fields = tuple(dict.fromkeys(self.config.rate_limit.key_fields + self.config.budget.key_fields))
        return tuple(f"{field}={values[field] or '*'}" for field in fields)


def _read_value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _extract_usage(response: Any) -> Usage:
    usage = _read_value(response, "usage", {}) or {}
    has_usage = any(
        _read_value(usage, key) is not None
        for key in ("prompt_tokens", "input_tokens", "completion_tokens", "output_tokens")
    )
    try:
        return Usage(
            input_tokens=int(_read_value(usage, "prompt_tokens", _read_value(usage, "input_tokens", 0)) or 0),
            output_tokens=int(_read_value(usage, "completion_tokens", _read_value(usage, "output_tokens", 0)) or 0),
            cached_tokens=int(_read_value(usage, "cached_tokens", 0) or 0),
            reasoning_tokens=int(_read_value(usage, "reasoning_tokens", 0) or 0),
            source="provider" if has_usage else "unknown",
        )
    except (TypeError, ValueError, OverflowError):
        return Usage(source="unknown")


def _resolve_usage(context: RunContext, estimated: Usage, actual: Usage, model: str | None) -> Usage:
    if context.pricing.price_for(model) is None:
        policy = context.config.token.missing_price
        context.state.record("budget.price_missing", model=model, action=policy)
        if policy == "reject":
            raise BudgetExceeded("未配置该模型的价格", reason_code="price_missing")
    if actual.source == "unknown":
        policy = context.config.token.missing_usage
        context.state.record("budget.usage_missing", action=policy)
        if policy == "reject":
            raise BudgetExceeded("供应商 usage 缺失或无效", reason_code="usage_missing")
        if policy == "estimate":
            return estimated
        return actual
    if actual.total_tokens > estimated.total_tokens:
        context.state.record(
            "budget.usage_mismatch",
            estimated_tokens=estimated.total_tokens,
            actual_tokens=actual.total_tokens,
            action=context.config.token.usage_mismatch,
        )
        if context.config.token.usage_mismatch == "reject":
            raise BudgetExceeded("供应商 usage 超过预留估算值", reason_code="usage_mismatch")
    return actual


def _request_estimates(
    context: RunContext, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> tuple[int, int]:
    messages = kwargs.get("messages", args[0] if args else None)
    input_tokens = estimate_tokens(messages)
    output_tokens = kwargs.get("max_output_tokens", kwargs.get("max_tokens"))
    if output_tokens is None:
        output_tokens = context.config.token.max_output_tokens or 256
    if not isinstance(output_tokens, int) or output_tokens < 0:
        output_tokens = 256
    return input_tokens, output_tokens


def _prepare_request(context: RunContext, function: Callable[..., T], kwargs: dict[str, Any]) -> dict[str, Any]:
    configured_limit = context.config.token.max_output_tokens
    if configured_limit is None:
        return kwargs
    prepared = dict(kwargs)
    for parameter in ("max_output_tokens", "max_tokens"):
        if parameter in prepared:
            value = prepared[parameter]
            if isinstance(value, int) and not isinstance(value, bool) and value > configured_limit:
                prepared[parameter] = configured_limit
            return prepared
    try:
        parameters = signature(function).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "max_output_tokens" in parameters or any(
        parameter.kind == parameter.VAR_KEYWORD for parameter in parameters.values()
    ):
        prepared["max_output_tokens"] = configured_limit
    elif "max_tokens" in parameters:
        prepared["max_tokens"] = configured_limit
    return prepared


def _stream_token_count(chunk: Any) -> int:
    usage = _extract_usage(chunk)
    if usage.source != "unknown":
        return usage.output_tokens
    return estimate_tokens(chunk)


def _call_metadata(context: RunContext, model: str | None, call_type: str = "llm") -> CallContext:
    parent = current_call.get()
    return CallContext(
        call_type=call_type,
        run_id=context.state.run_id,
        trace_id=context.state.trace_id,
        model=model,
        parent_id=parent.request_id if parent is not None and parent.run_id == context.state.run_id else None,
    )


@dataclass
class _PreparedCall:
    """请求准备阶段的标准化结果。"""

    function: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    call_context: CallContext
    model: str | None
    original_model: str | None
    estimated_usage: Usage
    estimated_cost: float


@dataclass
class _AdmittedCall:
    """准入和预算预留阶段的结果。"""

    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    model: str | None
    estimated_usage: Usage
    estimated_cost: float
    admission: Any
    reservation: Any
    circuit_key: str


def _apply_interceptors(
    context: RunContext,
    call_context: CallContext,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    request = {
        "call": call_context,
        "args": args,
        "kwargs": dict(kwargs),
        "snapshot": RequestSnapshot(
            model=kwargs.get("model"),
            messages=kwargs.get("messages", args[0] if args else None),
            parameters={key: value for key, value in kwargs.items() if key not in {"messages", "model"}},
            stream=kwargs.get("stream") is True,
        ),
    }
    updated = context.interceptors.before_request(request)
    if updated is None:
        raise BudgetExceeded("请求被拦截器阻止", reason_code="interceptor_blocked")
    updated_args = updated.get("args", args)
    updated_kwargs = updated.get("kwargs", kwargs)
    if not isinstance(updated_args, tuple) or not isinstance(updated_kwargs, dict):
        raise TypeError("拦截器必须返回 tuple 类型的 args 和 dict 类型的 kwargs")
    return updated_args, updated_kwargs


def _prepare_call(
    context: RunContext,
    function: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> _PreparedCall:
    """完成请求标准化、拦截器处理和预算预估。"""
    prepared_kwargs = _prepare_request(context, function, kwargs)
    call_context = _call_metadata(context, prepared_kwargs.get("model"))
    prepared_args, prepared_kwargs = _apply_interceptors(
        context, call_context, args, prepared_kwargs
    )
    prepared_kwargs = _prepare_request(context, function, prepared_kwargs)
    input_tokens, output_tokens = _request_estimates(context, prepared_args, prepared_kwargs)
    model = prepared_kwargs.get("model")
    estimated_usage = Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        source="estimate",
    )
    estimated_cost = context.pricing.estimate(model, estimated_usage) or 0.0
    if prepared_kwargs.get("stream") is True and context.config.token.run_token_budget is not None:
        remaining_budget = (
            context.config.token.run_token_budget
            - context.state.total_tokens
            - context.budget._reserved_tokens
        )
        max_reserved_request = int(remaining_budget / (1 + context.config.token.reservation_buffer))
        remaining_output = max(0, max_reserved_request - input_tokens)
        if output_tokens > remaining_output:
            output_tokens = remaining_output
            estimated_usage = Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                source="estimate",
            )
            for parameter in ("max_output_tokens", "max_tokens"):
                if parameter in prepared_kwargs:
                    prepared_kwargs = dict(prepared_kwargs)
                    prepared_kwargs[parameter] = output_tokens
                    break
            context.state.record(
                "budget.stream_reservation_capped",
                estimated_output_tokens=output_tokens,
                limit=context.config.token.run_token_budget,
            )
            estimated_cost = context.pricing.estimate(model, estimated_usage) or 0.0
    return _PreparedCall(
        function=function,
        args=prepared_args,
        kwargs=prepared_kwargs,
        call_context=call_context,
        model=model,
        original_model=model,
        estimated_usage=estimated_usage,
        estimated_cost=estimated_cost,
    )


def _admit_call(
    context: RunContext,
    prepared: _PreparedCall,
    *,
    budget_purpose: str,
    wait: float | None = None,
    record_rejection: bool = True,
) -> _AdmittedCall:
    """执行 admission、熔断检查和预算预留。"""
    args = prepared.args
    kwargs = prepared.kwargs
    model = prepared.model
    estimated_usage = prepared.estimated_usage
    estimated_cost = prepared.estimated_cost
    key = "|".join(context.scope_key(model=model))
    try:
        admission = context.admission.acquire(
            estimated_usage.total_tokens,
            key=key,
            wait=context._wait_budget(inherited_deadline(context.deadline)) if wait is None else wait,
        )
    except RateLimitExceeded:
        inherited_deadline(context.deadline).check()
        if record_rejection:
            context.state.record("rate_limit.rejected", reason_code="admission_timeout",
                                 waited_seconds=context.config.rate_limit.wait_timeout_seconds)
        raise
    try:
        inherited_deadline(context.deadline).check()
    except BaseException:
        admission.release()
        raise
    circuit_key = key
    try:
        context.circuits.before_call(circuit_key)
    except BaseException:
        context.state.record("circuit.rejected", reason_code="circuit_open")
        admission.release()
        raise
    try:
        reservation = context.budget.reserve(
            estimated_usage.input_tokens,
            estimated_usage.output_tokens,
            estimated_cost,
            purpose=budget_purpose,
        )
    except BudgetExceeded as error:
        admission.release()
        fallback_model = context.config.degradation.fallback_model
        can_downgrade = (
            context.config.token.on_exceeded == "downgrade"
            and fallback_model
            and model != fallback_model
            and error.reason_code in {"run_token_budget", "run_cost_budget", "request_cost_budget"}
        )
        if not can_downgrade:
            context.state.record(
                "budget.rejected",
                reason_code=error.reason_code,
                action=context.config.token.on_exceeded,
                retryable=error.retryable,
            )
            raise
        kwargs = dict(kwargs)
        kwargs["model"] = fallback_model
        model = fallback_model
        input_tokens, output_tokens = _request_estimates(context, args, kwargs)
        estimated_usage = Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            source="estimate",
        )
        fallback_cost = context.pricing.estimate(model, estimated_usage)
        rejection = fallback_rejection(context.config.degradation, args, kwargs, fallback_cost)
        if rejection is not None:
            context.state.record(
                "budget.fallback_rejected", request_id=prepared.call_context.request_id,
                from_model=prepared.model, to_model=fallback_model,
                reason_code=rejection, trigger_reason=error.reason_code,
            )
            raise error
        estimated_cost = fallback_cost
        try:
            reservation = context.budget.reserve(
                input_tokens,
                output_tokens,
                estimated_cost,
                purpose="fallback",
            )
        except BudgetExceeded:
            context.state.record(
                "budget.rejected",
                reason_code=error.reason_code,
                action="downgrade",
                retryable=error.retryable,
            )
            raise error
        context.state.record(
            "budget.downgraded",
            request_id=prepared.call_context.request_id,
            from_model=prepared.model,
            to_model=fallback_model,
            reason_code=error.reason_code,
        )
        # The original lease was released before selecting a fallback. Admit the
        # fallback under its own model key; never call it with a released lease.
        circuit_key = "|".join(context.scope_key(model=model))
        try:
            admission = context.admission.acquire(estimated_usage.total_tokens, key=circuit_key,
                                                 wait=context._wait_budget(inherited_deadline(context.deadline)) if wait is None else wait)
            context.circuits.before_call(circuit_key)
        except BaseException:
            admission.release()
            reservation.release()
            raise
    return _AdmittedCall(
        args=args,
        kwargs=kwargs,
        model=model,
        estimated_usage=estimated_usage,
        estimated_cost=estimated_cost,
        admission=admission,
        reservation=reservation,
        circuit_key=circuit_key,
    )


def _invoke_guarded(
    context: RunContext,
    function: Callable[..., T],
    *args: Any,
    budget_purpose: str = "primary",
    **kwargs: Any,
) -> T:
    if context.config.retry.max_retries and kwargs.get("stream") is not True:
        from .retries import invoke_with_retry
        return invoke_with_retry(context, function, args, kwargs, budget_purpose)
    return _invoke_once(context, function, *args, budget_purpose=budget_purpose, **kwargs)


def _invoke_once(
    context: RunContext,
    function: Callable[..., T],
    *args: Any,
    budget_purpose: str = "primary",
    **kwargs: Any,
) -> T:
    if iscoroutinefunction(function):
        async def execute():
            context._check()
            request_deadline = inherited_deadline(context.deadline).child(context.config.timeout.llm_seconds, "llm")
            prepared = _prepare_call(context, function, args, kwargs)
            wait_end = monotonic() + context.config.rate_limit.wait_timeout_seconds
            while True:
                request_deadline.check()
                try:
                    admitted = _admit_call(context, prepared, budget_purpose=budget_purpose, wait=0, record_rejection=False)
                    break
                except RateLimitExceeded:
                    if monotonic() >= wait_end:
                        context.state.record("rate_limit.rejected", reason_code="admission_timeout")
                        raise
                    remaining = request_deadline.remaining()
                    await asyncio.sleep(min(.01, max(0, wait_end - monotonic()), remaining if remaining is not None else .01))
            token = current_deadline.set(request_deadline)
            try:
                result = _execute_admitted(context, function, admitted, prepared.call_context)
                return await result if isawaitable(result) else result
            finally:
                current_deadline.reset(token)
        return context._defer(execute)
    context._check()
    deadline = inherited_deadline(context.deadline).child(context.config.timeout.llm_seconds, "llm")
    token = current_deadline.set(deadline)
    try:
        prepared = _prepare_call(context, function, args, kwargs)
        deadline.check()
        admitted = _admit_call(context, prepared, budget_purpose=budget_purpose)
        return _execute_admitted(context, function, admitted, prepared.call_context)
    finally:
        current_deadline.reset(token)


def _execute_admitted(context: RunContext, function: Callable[..., T], admitted: _AdmittedCall, metadata: CallContext) -> T:
    from .retries import current_attempt
    attempt = current_attempt.get()
    if attempt is not None and attempt.context is context and attempt.call is None:
        metadata = replace(metadata, attempt=attempt.number)
    call = CallLifecycle(context, admitted, replace(metadata, model=admitted.model))
    token = current_call.set(call.metadata)
    deadline_token = current_deadline.set(call.deadline)
    try:
        call.deadline.check()
        context.interceptors.on_start(call.metadata)
        context.state.llm_calls += 1
        if call.retry_attempt is not None and call.metadata.attempt > 1:
            context.state.retry_count += 1
        context.state.record("llm.requested", estimated_tokens=admitted.estimated_usage.total_tokens,
                             request_id=call.metadata.request_id, trace_id=call.metadata.trace_id,
                             run_id=call.metadata.run_id, parent_id=call.metadata.parent_id,
                             attempt=call.metadata.attempt, model=admitted.model)
        try:
            result = function(*admitted.args, **admitted.kwargs)
        except BaseException as error:
            if call.retry_attempt is not None:
                call.retry_attempt.provider_error = error
            raise
        if not isawaitable(result):
            try:
                call.deadline.check()
            except ExecutionTimeout:
                # A synchronous provider may return a live HTTP stream after
                # its deadline. Close it before discarding the late response.
                close = getattr(result, "close", None)
                if callable(close):
                    try:
                        close()
                    except BaseException:
                        context.state.record("llm.cleanup_failed", request_id=call.metadata.request_id)
                raise
        return dispatch(result, call)
    except BaseException as error:
        if not call.done:
            call.finish(error=error)
        raise
    finally:
        current_call.reset(token)
        current_deadline.reset(deadline_token)


def _invoke_owned(guard: "AgentGuard", name: str, function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    context = current_run()
    if context is not None:
        return context.invoke(function, *args, **kwargs)
    context = guard.run(name=name)
    context.__enter__()
    try:
        result = context.invoke(function, *args, **kwargs)
        if context._pending:
            context._automatic = True
            context._detach()
        else:
            context.__exit__(None, None, None)
        return result
    except BaseException as error:
        context.__exit__(type(error), error, error.__traceback__)
        raise


class GuardedProxy:
    def __init__(self, target: Any, guard: "AgentGuard", path: str = "") -> None:
        self._target = target
        self._guard = guard
        self._path = path

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._target, name)
        path = f"{self._path}.{name}" if self._path else name
        if callable(value) and name in {"create", "invoke", "run"}:
            if iscoroutinefunction(value):
                @wraps(value)
                async def guarded_async(*args: Any, **kwargs: Any) -> Any:
                    return await _invoke_owned(self._guard, path, value, *args, **kwargs)
                return guarded_async
            @wraps(value)
            def guarded(*args: Any, **kwargs: Any) -> Any:
                return _invoke_owned(self._guard, path, value, *args, **kwargs)
            return guarded
        if callable(value) and not isinstance(value, type):
            return value
        if hasattr(value, "__dict__") or not isinstance(value, (str, int, float, bool, bytes, type(None))):
            return GuardedProxy(value, self._guard, path)
        return value

    def __repr__(self) -> str:
        return repr(self._target)

    def __str__(self) -> str:
        return str(self._target)

    def __enter__(self) -> Any:
        enter = getattr(self._target, "__enter__", None)
        if enter is None:
            raise TypeError("被包装对象不支持同步上下文管理器")
        value = enter()
        return self if value is self._target else value

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> Any:
        exit_method = getattr(self._target, "__exit__", None)
        if exit_method is None:
            raise TypeError("被包装对象不支持同步上下文管理器")
        return exit_method(exc_type, exc, traceback)

    async def __aenter__(self) -> Any:
        enter = getattr(self._target, "__aenter__", None)
        if enter is None:
            raise TypeError("被包装对象不支持异步上下文管理器")
        value = await enter()
        return self if value is self._target else value

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> Any:
        exit_method = getattr(self._target, "__aexit__", None)
        if exit_method is None:
            raise TypeError("被包装对象不支持异步上下文管理器")
        return await exit_method(exc_type, exc, traceback)

    def __iter__(self):
        iterator = getattr(self._target, "__iter__", None)
        if iterator is None:
            raise TypeError("被包装对象不可迭代")
        return iterator()

    def __aiter__(self):
        iterator = getattr(self._target, "__aiter__", None)
        if iterator is None:
            raise TypeError("被包装对象不支持异步迭代")
        return iterator()

    async def __anext__(self):
        iterator = getattr(self._target, "__anext__", None)
        if iterator is None:
            raise TypeError("被包装对象不支持异步迭代")
        return await iterator()


class AgentGuard:
    def __init__(
        self,
        config: AgentGuardConfig | None = None,
        config_path: str | None = None,
        config_manager: ConfigManager | None = None,
        run_store: RunStore | None = None,
    ) -> None:
        if config_manager is not None and (config is not None or config_path is not None):
            raise ValueError("config_manager 与 config/config_path 不能同时传入")
        self._config_manager = config_manager or ConfigManager(config, config_path=config_path)
        self._run_store = run_store

    @property
    def config(self) -> AgentGuardConfig:
        return self._config_manager.config

    def snapshot(self) -> tuple[AgentGuardConfig, int]:
        snapshot = self._config_manager.snapshot()
        return snapshot.config, snapshot.version

    def reload(self) -> None:
        self._config_manager.reload()

    def update_config(self, values: dict[str, Any]) -> None:
        self._config_manager.update(values)

    def run(
        self,
        name: str = "agent_run",
        user_id: str | None = None,
        *,
        tenant_id: str | None = None,
        project_id: str | None = None,
        environment: str | None = None,
        agent_id: str | None = None,
    ) -> RunContext:
        snapshot = self._config_manager.snapshot()
        return RunContext(
            snapshot.config,
            RunState(
                name=name,
                user_id=user_id,
                tenant_id=tenant_id,
                project_id=project_id,
                environment=environment,
                agent_id=agent_id,
            ),
            snapshot.version,
            self._run_store,
        )

    def wrap(self, client: Any) -> GuardedProxy:
        if isinstance(client, GuardedProxy) and client._guard is self:
            return client
        return GuardedProxy(client, self)

    def guard(self, function: Callable[..., T] | None = None, *, name: str | None = None) -> Any:
        def decorator(target: Callable[..., T]) -> Callable[..., T]:
            if iscoroutinefunction(target):
                @wraps(target)
                async def wrapped_async(*args: Any, **kwargs: Any) -> Any:
                    return await _invoke_owned(self, name or target.__name__, target, *args, **kwargs)
                return wrapped_async
            @wraps(target)
            def wrapped(*args: Any, **kwargs: Any) -> T:
                return _invoke_owned(self, name or target.__name__, target, *args, **kwargs)
            return wrapped
        return decorator(function) if function is not None else decorator


def guard(function: Callable[..., T] | None = None, **kwargs: Any) -> Any:
    return AgentGuard().guard(function, **kwargs)
