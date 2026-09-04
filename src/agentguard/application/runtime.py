from __future__ import annotations

"""Runtime call pipeline and lifecycle orchestration.

This module owns execution sequencing. Public callers should import the
stable facade from ``agentguard.api`` instead.
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
from ..domain.exceptions import BudgetExceeded, RateLimitExceeded
from ..domain.models import RunState, Usage


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
        self.circuits = CircuitRegistry()
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

    def invoke_tool(self, tool_name: str, function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        if not tool_name or not isinstance(tool_name, str):
            raise ValueError("tool_name must be a non-empty string")
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
            raise BudgetExceeded("no price is configured for the model", reason_code="price_missing")
    if actual.source == "unknown":
        policy = context.config.token.missing_usage
        context.state.record("budget.usage_missing", action=policy)
        if policy == "reject":
            raise BudgetExceeded("provider usage is missing or invalid", reason_code="usage_missing")
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
            raise BudgetExceeded("provider usage exceeds the reservation estimate", reason_code="usage_mismatch")
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


def _finish_sync_stream(
    context: RunContext,
    result: Any,
    reservation: Any,
    estimated_usage: Usage,
    model: str | None,
    admission: Any,
    circuits: CircuitRegistry,
    circuit_key: str,
) -> Any:
    output_tokens = 0
    provider_usage: Usage | None = None
    try:
        for chunk in result:
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
                raise BudgetExceeded("stream token budget exceeded", reason_code="stream_token_budget")
            yield chunk
    except BaseException as error:
        circuits.record_failure(circuit_key, error)
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
        except BaseException:
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
) -> Any:
    output_tokens = 0
    provider_usage: Usage | None = None
    try:
        async for chunk in result:
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
                raise BudgetExceeded("stream token budget exceeded", reason_code="stream_token_budget")
            yield chunk
    except BaseException as error:
        circuits.record_failure(circuit_key, error)
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
        except BaseException:
            admission.release()
            reservation.release()
            context.state.record("llm.failed")
            raise


def _invoke_guarded(
    context: RunContext,
    function: Callable[..., T],
    *args: Any,
    budget_purpose: str = "primary",
    **kwargs: Any,
) -> T:
    kwargs = _prepare_request(context, function, kwargs)
    input_tokens, output_tokens = _request_estimates(context, args, kwargs)
    model = kwargs.get("model")
    original_model = model
    estimated_usage = Usage(input_tokens=input_tokens, output_tokens=output_tokens, source="estimate")
    estimated_cost = context.pricing.estimate(model, estimated_usage) or 0.0
    if kwargs.get("stream") is True and context.config.token.run_token_budget is not None:
        remaining_budget = (
            context.config.token.run_token_budget
            - context.state.total_tokens
            - context.budget._reserved_tokens
        )
        max_reserved_request = int(remaining_budget / (1 + context.config.token.reservation_buffer))
        remaining_output = max(0, max_reserved_request - input_tokens)
        if output_tokens > remaining_output:
            output_tokens = remaining_output
            estimated_usage = Usage(input_tokens=input_tokens, output_tokens=output_tokens, source="estimate")
            for parameter in ("max_output_tokens", "max_tokens"):
                if parameter in kwargs:
                    kwargs = dict(kwargs)
                    kwargs[parameter] = output_tokens
                    break
            context.state.record(
                "budget.stream_reservation_capped",
                estimated_output_tokens=output_tokens,
                limit=context.config.token.run_token_budget,
            )
            estimated_cost = context.pricing.estimate(model, estimated_usage) or 0.0
    try:
        admission = context.admission.acquire(
            input_tokens + output_tokens,
            key="|".join(context.scope_key(model=model)),
            wait=context.config.rate_limit.wait_timeout_seconds,
        )
    except RateLimitExceeded:
        context.state.record(
            "rate_limit.rejected",
            reason_code="admission_timeout",
            waited_seconds=context.config.rate_limit.wait_timeout_seconds,
        )
        raise
    circuit_key = "|".join(context.scope_key(model=model))
    try:
        context.circuits.before_call(circuit_key)
    except BaseException as error:
        context.state.record("circuit.rejected", reason_code="circuit_open")
        admission.release()
        raise
    try:
        reservation = context.budget.reserve(input_tokens, output_tokens, estimated_cost, purpose=budget_purpose)
    except BudgetExceeded as error:
        admission.release()
        fallback_model = context.config.degradation.fallback_model
        can_downgrade = (
            context.config.token.on_exceeded == "downgrade"
            and fallback_model
            and model != fallback_model
            and error.reason_code in {"run_token_budget", "run_cost_budget", "request_cost_budget"}
        )
        if can_downgrade:
            kwargs = dict(kwargs)
            kwargs["model"] = fallback_model
            model = fallback_model
            input_tokens, output_tokens = _request_estimates(context, args, kwargs)
            estimated_usage = Usage(input_tokens=input_tokens, output_tokens=output_tokens, source="estimate")
            estimated_cost = context.pricing.estimate(model, estimated_usage) or 0.0
            try:
                reservation = context.budget.reserve(
                    input_tokens, output_tokens, estimated_cost, purpose="fallback"
                )
            except BudgetExceeded:
                context.state.record("budget.rejected", reason_code=error.reason_code, action="downgrade", retryable=error.retryable)
                raise error
            context.state.record(
                "budget.downgraded",
                from_model=original_model,
                to_model=fallback_model,
                reason_code=error.reason_code,
            )
        else:
            context.state.record(
                "budget.rejected",
                reason_code=error.reason_code,
                action=context.config.token.on_exceeded,
                retryable=error.retryable,
            )
            raise
    context.state.llm_calls += 1
    context.state.record("llm.requested", estimated_tokens=input_tokens + output_tokens)
    try:
        result = function(*args, **kwargs)
    except BaseException as error:
        context.circuits.record_failure(circuit_key, error)
        admission.release()
        reservation.release()
        context.state.record("llm.failed")
        raise
    if kwargs.get("stream") is True and hasattr(result, "__aiter__"):
        return _finish_async_stream(context, result, reservation, estimated_usage, model, admission, context.circuits, circuit_key)  # type: ignore[return-value]
    if kwargs.get("stream") is True and hasattr(result, "__iter__"):
        return _finish_sync_stream(context, result, reservation, estimated_usage, model, admission, context.circuits, circuit_key)  # type: ignore[return-value]
    if isawaitable(result):
        async def finish() -> Any:
            try:
                response = await result
                usage = _resolve_usage(context, estimated_usage, _extract_usage(response), model)
                actual_cost = context.pricing.estimate(model, usage) or estimated_cost
                reservation.settle(usage, actual_cost)
                context.circuits.record_success(circuit_key)
                admission.release()
                context.state.record("llm.completed", total_tokens=usage.total_tokens)
                return response
            except BaseException as error:
                context.circuits.record_failure(circuit_key, error)
                context.circuits.record_failure(circuit_key, error if 'error' in locals() else RuntimeError("call failed"))
                admission.release()
                reservation.release()
                context.state.record("llm.failed")
                raise
        return finish()  # type: ignore[return-value]
    try:
        usage = _resolve_usage(context, estimated_usage, _extract_usage(result), model)
        actual_cost = context.pricing.estimate(model, usage) or estimated_cost
        reservation.settle(usage, actual_cost)
        context.circuits.record_success(circuit_key)
        admission.release()
        context.state.record("llm.completed", total_tokens=usage.total_tokens)
        return result
    except BaseException as error:
        context.circuits.record_failure(circuit_key, error)
        admission.release()
        reservation.release()
        context.state.record("llm.failed")
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


class AgentGuard:
    def __init__(
        self,
        config: AgentGuardConfig | None = None,
        config_path: str | None = None,
        config_manager: ConfigManager | None = None,
    ) -> None:
        if config_manager is not None and (config is not None or config_path is not None):
            raise ValueError("pass config_manager or config/config_path, not both")
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
