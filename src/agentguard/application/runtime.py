from __future__ import annotations

"""运行时调用管线和生命周期编排。

本模块负责执行顺序。公共调用方应从 ``agentguard.api`` 导入稳定 facade。
"""

from contextlib import AbstractContextManager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from inspect import isawaitable, signature
from typing import Any, Callable, TypeVar

from ..policies.budget import BudgetManager, estimate_tokens
from ..policies.cost import PricingTable
from ..config.loader import AgentGuardConfig
from ..config.manager import ConfigManager
from ..policies.rate_limit import AdmissionController, CircuitRegistry
from ..adapters.storage.redis import RedisAdmissionController
from .interceptors import BaseInterceptor, InterceptorChain
from ..domain.exceptions import BudgetExceeded, RateLimitExceeded
from ..domain.models import CallContext, RequestSnapshot, RunState, Usage


_current_run: ContextVar["RunContext | None"] = ContextVar("agentguard_run", default=None)
T = TypeVar("T")


def current_run() -> "RunContext | None":
    return _current_run.get()


@dataclass
class RunContext(AbstractContextManager["RunContext"]):
    config: AgentGuardConfig
    state: RunState
    config_version: int = 1

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

    def __enter__(self) -> "RunContext":
        self._token = _current_run.set(self)
        self.state.record("run.started", config_version=self.config_version)
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.state.status = "failed" if exc_type else "completed"
        self.state.record("run.failed" if exc_type else "run.completed")
        if self._token is not None:
            _current_run.reset(self._token)

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

    def invoke_tool(self, tool_name: str, function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        if not tool_name or not isinstance(tool_name, str):
            raise ValueError("tool_name 必须是非空字符串")
        key = "|".join(self.scope_key(tool=tool_name))
        try:
            admission = self.admission.acquire(
                key=key,
                tool=True,
                wait=self.config.rate_limit.wait_timeout_seconds,
            )
        except RateLimitExceeded:
            self.state.record("rate_limit.rejected", reason_code="tool_admission_timeout", tool=tool_name)
            raise
        try:
            self.circuits.before_call(key)
        except BaseException:
            admission.release()
            self.state.record("circuit.rejected", reason_code="circuit_open", tool=tool_name)
            raise
        self.state.tool_calls += 1
        self.state.record("tool.started", tool=tool_name)
        try:
            result = function(*args, **kwargs)
        except BaseException as error:
            self.circuits.record_failure(key, error)
            admission.release()
            self.state.record("tool.failed", tool=tool_name)
            raise
        if isawaitable(result):
            async def finish() -> Any:
                try:
                    response = await result
                    self.circuits.record_success(key)
                    self.state.record("tool.completed", tool=tool_name)
                    return response
                except BaseException as error:
                    self.circuits.record_failure(key, error)
                    self.state.record("tool.failed", tool=tool_name)
                    raise
                finally:
                    admission.release()
            return finish()  # type: ignore[return-value]
        self.circuits.record_success(key)
        admission.release()
        self.state.record("tool.completed", tool=tool_name)
        return result

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
    return CallContext(
        call_type=call_type,
        run_id=context.state.run_id,
        trace_id=context.state.trace_id,
        model=model,
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
            wait=context.config.rate_limit.wait_timeout_seconds,
        )
    except RateLimitExceeded:
        context.state.record(
            "rate_limit.rejected",
            reason_code="admission_timeout",
            waited_seconds=context.config.rate_limit.wait_timeout_seconds,
        )
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
        estimated_cost = context.pricing.estimate(model, estimated_usage) or 0.0
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
            from_model=prepared.model,
            to_model=fallback_model,
            reason_code=error.reason_code,
        )
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


def _finish_sync_stream(
    context: RunContext,
    result: Any,
    reservation: Any,
    estimated_usage: Usage,
    model: str | None,
    admission: Any,
    circuits: CircuitRegistry,
    circuit_key: str,
    call_context: CallContext,
) -> Any:
    output_tokens = 0
    provider_usage: Usage | None = None
    try:
        for chunk in result:
            chunk = context.interceptors.on_chunk(chunk, call_context)
            chunk_usage = _extract_usage(chunk)
            if chunk_usage.source != "unknown":
                provider_usage = chunk_usage
            output_tokens += _stream_token_count(chunk)
            token_limit = context.config.token.run_token_budget
            if token_limit is not None and context.state.total_tokens + estimated_usage.input_tokens + output_tokens > token_limit:
                context.state.record(
                    "budget.stream_stopped",
                    action=context.config.token.on_exceeded,
                    actual_tokens=context.state.total_tokens + estimated_usage.input_tokens + output_tokens,
                    limit=token_limit,
                )
                raise BudgetExceeded("流式 Token 预算已超限", reason_code="stream_token_budget")
            yield chunk
    except BaseException as error:
        circuits.record_failure(circuit_key, error)
        context.interceptors.on_error(error)
        raise
    finally:
        usage = provider_usage or Usage(
            input_tokens=estimated_usage.input_tokens,
            output_tokens=output_tokens,
            source="estimate",
        )
        try:
            usage = _resolve_usage(context, estimated_usage, usage, model)
            actual_cost = context.pricing.estimate(model, usage) or 0.0
            reservation.settle(usage, actual_cost)
            circuits.record_success(circuit_key)
            admission.release()
            context.state.record("llm.completed", total_tokens=usage.total_tokens)
            context.interceptors.on_finish(call_context)
        except BaseException:
            context.interceptors.on_finish(call_context)
            admission.release()
            reservation.release()
            context.state.record("llm.failed")
            raise


async def _finish_async_stream(
    context: RunContext,
    result: Any,
    reservation: Any,
    estimated_usage: Usage,
    model: str | None,
    admission: Any,
    circuits: CircuitRegistry,
    circuit_key: str,
    call_context: CallContext,
) -> Any:
    output_tokens = 0
    provider_usage: Usage | None = None
    try:
        async for chunk in result:
            chunk = context.interceptors.on_chunk(chunk, call_context)
            chunk_usage = _extract_usage(chunk)
            if chunk_usage.source != "unknown":
                provider_usage = chunk_usage
            output_tokens += _stream_token_count(chunk)
            token_limit = context.config.token.run_token_budget
            if token_limit is not None and context.state.total_tokens + estimated_usage.input_tokens + output_tokens > token_limit:
                context.state.record(
                    "budget.stream_stopped",
                    action=context.config.token.on_exceeded,
                    actual_tokens=context.state.total_tokens + estimated_usage.input_tokens + output_tokens,
                    limit=token_limit,
                )
                raise BudgetExceeded("流式 Token 预算已超限", reason_code="stream_token_budget")
            yield chunk
    except BaseException as error:
        circuits.record_failure(circuit_key, error)
        context.interceptors.on_error(error)
        raise
    finally:
        usage = provider_usage or Usage(
            input_tokens=estimated_usage.input_tokens,
            output_tokens=output_tokens,
            source="estimate",
        )
        try:
            usage = _resolve_usage(context, estimated_usage, usage, model)
            actual_cost = context.pricing.estimate(model, usage) or 0.0
            reservation.settle(usage, actual_cost)
            circuits.record_success(circuit_key)
            admission.release()
            context.state.record("llm.completed", total_tokens=usage.total_tokens)
            context.interceptors.on_finish(call_context)
        except BaseException:
            context.interceptors.on_finish(call_context)
            admission.release()
            reservation.release()
            context.state.record("llm.failed")
            raise


def _fail_call(
    context: RunContext,
    error: BaseException,
    reservation: Any,
    admission: Any,
    circuits: CircuitRegistry,
    circuit_key: str,
    call_context: CallContext,
) -> None:
    """统一执行调用失败时的熔断、拦截器和资源清理。"""
    try:
        circuits.record_failure(circuit_key, error)
        context.interceptors.on_error(error)
    finally:
        try:
            context.interceptors.on_finish(call_context)
        finally:
            admission.release()
            reservation.release()
            context.state.record("llm.failed")


def _settle_response(
    context: RunContext,
    response: Any,
    reservation: Any,
    estimated_usage: Usage,
    estimated_cost: float,
    model: str | None,
    admission: Any,
    circuits: CircuitRegistry,
    circuit_key: str,
    call_context: CallContext,
) -> Any:
    """统一处理非流式响应、实际 usage、结算和成功事件。"""
    response = context.interceptors.after_response(response)
    usage = _resolve_usage(context, estimated_usage, _extract_usage(response), model)
    actual_cost = context.pricing.estimate(model, usage) or estimated_cost
    reservation.settle(usage, actual_cost)
    circuits.record_success(circuit_key)
    admission.release()
    context.state.record("llm.completed", total_tokens=usage.total_tokens)
    context.interceptors.on_finish(call_context)
    return response


def _start_call(
    context: RunContext,
    function: Callable[..., Any],
    admitted: _AdmittedCall,
    call_context: CallContext,
) -> Any:
    """启动一次供应商调用，并记录调用开始阶段事件。"""
    context.interceptors.on_start(call_context)
    context.state.llm_calls += 1
    context.state.record(
        "llm.requested",
        estimated_tokens=admitted.estimated_usage.total_tokens,
        request_id=call_context.request_id,
        trace_id=call_context.trace_id,
        run_id=call_context.run_id,
        parent_id=call_context.parent_id,
        attempt=call_context.attempt,
    )
    try:
        return function(*admitted.args, **admitted.kwargs)
    except BaseException as error:
        _fail_call(
            context,
            error,
            admitted.reservation,
            admitted.admission,
            context.circuits,
            admitted.circuit_key,
            call_context,
        )
        raise


def _dispatch_response(
    context: RunContext,
    result: Any,
    admitted: _AdmittedCall,
    call_context: CallContext,
) -> Any:
    """按返回对象类型分派到同步、异步或流式消费阶段。"""
    if admitted.kwargs.get("stream") is True and hasattr(result, "__aiter__"):
        return _finish_async_stream(
            context,
            result,
            admitted.reservation,
            admitted.estimated_usage,
            admitted.model,
            admitted.admission,
            context.circuits,
            admitted.circuit_key,
            call_context,
        )
    if admitted.kwargs.get("stream") is True and hasattr(result, "__iter__"):
        return _finish_sync_stream(
            context,
            result,
            admitted.reservation,
            admitted.estimated_usage,
            admitted.model,
            admitted.admission,
            context.circuits,
            admitted.circuit_key,
            call_context,
        )
    if isawaitable(result):
        async def finish() -> Any:
            try:
                response = await result
                return _settle_response(
                    context,
                    response,
                    admitted.reservation,
                    admitted.estimated_usage,
                    admitted.estimated_cost,
                    admitted.model,
                    admitted.admission,
                    context.circuits,
                    admitted.circuit_key,
                    call_context,
                )
            except BaseException as error:
                _fail_call(
                    context,
                    error,
                    admitted.reservation,
                    admitted.admission,
                    context.circuits,
                    admitted.circuit_key,
                    call_context,
                )
                raise

        return finish()
    try:
        return _settle_response(
            context,
            result,
            admitted.reservation,
            admitted.estimated_usage,
            admitted.estimated_cost,
            admitted.model,
            admitted.admission,
            context.circuits,
            admitted.circuit_key,
            call_context,
        )
    except BaseException as error:
        _fail_call(
            context,
            error,
            admitted.reservation,
            admitted.admission,
            context.circuits,
            admitted.circuit_key,
            call_context,
        )
        raise


def _invoke_guarded(
    context: RunContext,
    function: Callable[..., T],
    *args: Any,
    budget_purpose: str = "primary",
    **kwargs: Any,
) -> T:
    prepared = _prepare_call(context, function, args, kwargs)
    call_context = prepared.call_context
    admitted = _admit_call(context, prepared, budget_purpose=budget_purpose)
    result = _start_call(context, function, admitted, call_context)
    return _dispatch_response(context, result, admitted, call_context)  # type: ignore[return-value]


class GuardedProxy:
    def __init__(self, target: Any, guard: "AgentGuard", path: str = "") -> None:
        self._target = target
        self._guard = guard
        self._path = path

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._target, name)
        path = f"{self._path}.{name}" if self._path else name
        if callable(value) and name in {"create", "invoke", "run"}:
            @wraps(value)
            def guarded(*args: Any, **kwargs: Any) -> Any:
                context = current_run()
                if context is None:
                    with self._guard.run(name=path) as new_context:
                        return _invoke_guarded(new_context, value, *args, **kwargs)
                return _invoke_guarded(context, value, *args, **kwargs)
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
    ) -> None:
        if config_manager is not None and (config is not None or config_path is not None):
            raise ValueError("config_manager 与 config/config_path 不能同时传入")
        self._config_manager = config_manager or ConfigManager(config, config_path=config_path)

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
        )

    def wrap(self, client: Any) -> GuardedProxy:
        if isinstance(client, GuardedProxy) and client._guard is self:
            return client
        return GuardedProxy(client, self)

    def guard(self, function: Callable[..., T] | None = None, *, name: str | None = None) -> Any:
        def decorator(target: Callable[..., T]) -> Callable[..., T]:
            @wraps(target)
            def wrapped(*args: Any, **kwargs: Any) -> T:
                context = current_run()
                if context is None:
                    with self.run(name=name or target.__name__) as new_context:
                        return new_context.invoke(target, *args, **kwargs)
                return context.invoke(target, *args, **kwargs)
            return wrapped
        return decorator(function) if function is not None else decorator


def guard(function: Callable[..., T] | None = None, **kwargs: Any) -> Any:
    return AgentGuard().guard(function, **kwargs)
