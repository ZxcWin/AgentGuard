from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path
from typing import Any, Mapping

from ..domain.exceptions import ConfigurationError

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ModelPriceConfig:
    input_per_million: float = 0.0
    output_per_million: float = 0.0
    cached_input_per_million: float | None = None
    reasoning_output_per_million: float | None = None

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "ModelPriceConfig":
        values = _mapping(values, "pricing model")
        _check_keys(values, set(cls.__dataclass_fields__), "pricing model")
        return cls(
            input_per_million=_optional_float(values.get("input_per_million", 0), "pricing.input_per_million") or 0.0,
            output_per_million=_optional_float(values.get("output_per_million", 0), "pricing.output_per_million") or 0.0,
            cached_input_per_million=_optional_float(values.get("cached_input_per_million"), "pricing.cached_input_per_million"),
            reasoning_output_per_million=_optional_float(values.get("reasoning_output_per_million"), "pricing.reasoning_output_per_million"),
        )


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{name} 必须是映射类型")
    return value


def _optional_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigurationError(f"{name} 必须是非负整数或 None")
    return value


def _optional_float(value: Any, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{name} 必须是非负数或 None")
    try:
        result = float(value)
    except OverflowError as error:
        raise ConfigurationError(f"{name} must be a finite nonnegative number") from error
    if result < 0 or not isfinite(result):
        raise ConfigurationError(f"{name} 必须是有限的非负数或 None")
    return result


def _bounded_float(value: Any, name: str, default: float) -> float:
    result = _optional_float(default if value is None else value, name)
    assert result is not None
    if not 0 <= result <= 1:
        raise ConfigurationError(f"{name} 必须介于 0 和 1 之间")
    return result


def _check_keys(values: Mapping[str, Any], allowed: set[str], name: str) -> None:
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ConfigurationError(f"{name} 存在未知字段：{', '.join(unknown)}")


SCOPE_FIELDS = (
    "tenant_id",
    "project_id",
    "environment",
    "agent_id",
    "user_id",
    "model",
    "tool",
)


def _scope_fields(value: Any, name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    if not isinstance(value, (list, tuple)) or not value:
        raise ConfigurationError(f"{name} 必须是非空的 scope 字段列表")
    fields = tuple(value)
    if any(not isinstance(item, str) or item not in SCOPE_FIELDS for item in fields):
        raise ConfigurationError(f"{name} 包含不支持的 scope 字段")
    if len(set(fields)) != len(fields):
        raise ConfigurationError(f"{name} 不能包含重复的 scope 字段")
    return fields


@dataclass(frozen=True)
class TokenConfig:
    max_output_tokens: int | None = None
    max_input_tokens: int | None = None
    run_token_budget: int | None = None
    reservation_buffer: float = 0.1
    retry_budget_ratio: float = 0.2
    on_exceeded: str = "reject"
    missing_usage: str = "estimate"
    usage_mismatch: str = "observe"
    missing_price: str = "observe"

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "TokenConfig":
        values = _mapping(values, "token")
        _check_keys(values, set(cls.__dataclass_fields__), "token")
        config = cls(
            max_output_tokens=_optional_int(values.get("max_output_tokens"), "token.max_output_tokens"),
            max_input_tokens=_optional_int(values.get("max_input_tokens"), "token.max_input_tokens"),
            run_token_budget=_optional_int(values.get("run_token_budget"), "token.run_token_budget"),
            reservation_buffer=_bounded_float(values.get("reservation_buffer"), "token.reservation_buffer", 0.1),
            retry_budget_ratio=_bounded_float(values.get("retry_budget_ratio"), "token.retry_budget_ratio", 0.2),
            on_exceeded=str(values.get("on_exceeded", "reject")),
            missing_usage=str(values.get("missing_usage", "estimate")),
            usage_mismatch=str(values.get("usage_mismatch", "observe")),
            missing_price=str(values.get("missing_price", "observe")),
        )
        if config.on_exceeded not in {"reject", "downgrade", "stop_stream"}:
            raise ConfigurationError("token.on_exceeded 必须是 reject、downgrade 或 stop_stream")
        if config.missing_usage not in {"estimate", "unknown", "reject"}:
            raise ConfigurationError("token.missing_usage 必须是 estimate、unknown 或 reject")
        if config.usage_mismatch not in {"observe", "reject"}:
            raise ConfigurationError("token.usage_mismatch 必须是 observe 或 reject")
        if config.missing_price not in {"observe", "reject"}:
            raise ConfigurationError("token.missing_price 必须是 observe 或 reject")
        return config


@dataclass(frozen=True)
class BudgetConfig:
    max_cost_usd: float | None = None
    run_cost_usd: float | None = None
    cost_warning_threshold: float = 0.8
    key_fields: tuple[str, ...] = SCOPE_FIELDS[:5]

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "BudgetConfig":
        values = _mapping(values, "budget")
        _check_keys(values, set(cls.__dataclass_fields__), "budget")
        return cls(
            max_cost_usd=_optional_float(values.get("max_cost_usd"), "budget.max_cost_usd"),
            run_cost_usd=_optional_float(values.get("run_cost_usd"), "budget.run_cost_usd"),
            cost_warning_threshold=_bounded_float(
                values.get("cost_warning_threshold"), "budget.cost_warning_threshold", 0.8
            ),
            key_fields=_scope_fields(values.get("key_fields"), "budget.key_fields", SCOPE_FIELDS[:5]),
        )


@dataclass(frozen=True)
class RateLimitConfig:
    backend: str = "memory"
    redis_url: str | None = None
    redis_failure_policy: str = "reject"
    redis_prefix: str = "agentguard"
    redis_window_seconds: int = 60
    rpm: int | None = None
    tpm: int | None = None
    max_concurrency: int | None = None
    max_tool_calls: int | None = None
    max_scope_keys: int = 10_000
    wait_timeout_seconds: float = 0.0
    key_fields: tuple[str, ...] = SCOPE_FIELDS[:5]

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "RateLimitConfig":
        values = _mapping(values, "rate_limit")
        _check_keys(values, set(cls.__dataclass_fields__), "rate_limit")
        backend = str(values.get("backend", "memory"))
        redis_url = values.get("redis_url")
        if redis_url is not None and (not isinstance(redis_url, str) or not redis_url.strip()):
            raise ConfigurationError("rate_limit.redis_url 必须是非空字符串或 None")
        redis_failure_policy = str(values.get("redis_failure_policy", "reject"))
        redis_prefix = str(values.get("redis_prefix", "agentguard"))
        redis_window_seconds = _optional_int(values.get("redis_window_seconds", 60), "rate_limit.redis_window_seconds")
        if backend not in {"memory", "redis"}:
            raise ConfigurationError("rate_limit.backend 必须是 memory 或 redis")
        if redis_failure_policy not in {"reject", "memory"}:
            raise ConfigurationError("rate_limit.redis_failure_policy 必须是 reject 或 memory")
        if not redis_prefix.strip() or redis_window_seconds is None or redis_window_seconds <= 0:
            raise ConfigurationError("rate_limit 的 Redis 配置无效")
        if backend == "redis" and not redis_url:
            raise ConfigurationError("backend 为 redis 时必须配置 rate_limit.redis_url")
        max_scope_keys = _optional_int(values.get("max_scope_keys"), "rate_limit.max_scope_keys")
        if max_scope_keys is None:
            max_scope_keys = 10_000
        if max_scope_keys <= 0:
            raise ConfigurationError("rate_limit.max_scope_keys 必须大于 0")
        return cls(
            backend=backend,
            redis_url=redis_url,
            redis_failure_policy=redis_failure_policy,
            redis_prefix=redis_prefix,
            redis_window_seconds=redis_window_seconds,
            rpm=_optional_int(values.get("rpm"), "rate_limit.rpm"),
            tpm=_optional_int(values.get("tpm"), "rate_limit.tpm"),
            max_concurrency=_optional_int(values.get("max_concurrency"), "rate_limit.max_concurrency"),
            max_tool_calls=_optional_int(values.get("max_tool_calls"), "rate_limit.max_tool_calls"),
            max_scope_keys=max_scope_keys,
            wait_timeout_seconds=_bounded_float(
                values.get("wait_timeout_seconds"), "rate_limit.wait_timeout_seconds", 0.0
            ),
            key_fields=_scope_fields(values.get("key_fields"), "rate_limit.key_fields", SCOPE_FIELDS[:5]),
        )


@dataclass(frozen=True)
class PricingConfig:
    version: str = "1"
    models: Mapping[str, ModelPriceConfig] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "PricingConfig":
        values = _mapping(values, "pricing")
        _check_keys(values, {"version", "models"}, "pricing")
        version = values.get("version", "1")
        if not isinstance(version, str) or not version.strip():
            raise ConfigurationError("pricing.version 必须是非空字符串")
        models = _mapping(values.get("models"), "pricing.models")
        return cls(version=version, models={
            str(model): ModelPriceConfig.from_mapping(price)
            for model, price in models.items()
        })


MODEL_CAPABILITIES = {"text", "tools", "json_object", "json_schema", "vision", "audio", "streaming"}
DATA_CLASSIFICATIONS = ("public", "internal", "confidential", "restricted")


def _string_list(value: Any, name: str, allowed: set[str] | None = None) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) or not item.strip() or item != item.strip() for item in value
    ):
        raise ConfigurationError(f"{name} 必须是非空字符串组成的列表")
    if len(set(value)) != len(value) or (allowed is not None and set(value) - allowed):
        raise ConfigurationError(f"{name} 包含重复或不支持的值")
    return tuple(value)


@dataclass(frozen=True)
class ModelCapabilityConfig:
    capabilities: tuple[str, ...] = ("text",)
    regions: tuple[str, ...] = ()
    max_data_classification: str = "public"

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "ModelCapabilityConfig":
        values = _mapping(values, "model profile")
        _check_keys(values, set(cls.__dataclass_fields__), "model profile")
        classification = values.get("max_data_classification", "public")
        if classification not in DATA_CLASSIFICATIONS:
            raise ConfigurationError("max_data_classification 必须是 public/internal/confidential/restricted")
        return cls(
            capabilities=_string_list(values.get("capabilities", ["text"]), "capabilities", MODEL_CAPABILITIES),
            regions=_string_list(values.get("regions", []), "regions"),
            max_data_classification=classification,
        )


@dataclass(frozen=True)
class DegradationConfig:
    fallback_model: str | None = None
    model_profiles: Mapping[str, ModelCapabilityConfig] = field(default_factory=dict)
    required_capabilities: tuple[str, ...] = ()
    allowed_regions: tuple[str, ...] = ()
    data_classification: str = "public"
    max_cost_usd: float | None = None

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "DegradationConfig":
        values = _mapping(values, "degradation")
        _check_keys(values, set(cls.__dataclass_fields__), "degradation")
        fallback_model = values.get("fallback_model")
        if fallback_model is not None and (not isinstance(fallback_model, str) or not fallback_model.strip()):
            raise ConfigurationError("degradation.fallback_model 必须是非空字符串或 None")
        profiles = _mapping(values.get("model_profiles"), "degradation.model_profiles")
        if any(not isinstance(key, str) or not key.strip() or key != key.strip() for key in profiles):
            raise ConfigurationError("model profile names must be nonempty strings without surrounding whitespace")
        classification = values.get("data_classification", "public")
        if classification not in DATA_CLASSIFICATIONS:
            raise ConfigurationError("degradation.data_classification 必须是 public/internal/confidential/restricted")
        return cls(
            fallback_model=fallback_model,
            model_profiles={key: ModelCapabilityConfig.from_mapping(profile) for key, profile in profiles.items()},
            required_capabilities=_string_list(values.get("required_capabilities", []), "degradation.required_capabilities", MODEL_CAPABILITIES),
            allowed_regions=_string_list(values.get("allowed_regions", []), "degradation.allowed_regions"),
            data_classification=classification,
            max_cost_usd=_optional_float(values.get("max_cost_usd"), "degradation.max_cost_usd"),
        )


@dataclass(frozen=True)
class TelemetryConfig:
    verbose: bool = True
    enable_metrics_export: bool = False

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "TelemetryConfig":
        values = _mapping(values, "telemetry")
        _check_keys(values, set(cls.__dataclass_fields__), "telemetry")
        for key in cls.__dataclass_fields__:
            if key in values and not isinstance(values[key], bool):
                raise ConfigurationError(f"telemetry.{key} 必须是布尔值")
        return cls(
            verbose=values.get("verbose", True),
            enable_metrics_export=values.get("enable_metrics_export", False),
        )


@dataclass(frozen=True)
class RetryConfig:
    max_retries: int = 0
    initial_delay_seconds: float = 0.5
    max_delay_seconds: float = 10.0
    max_elapsed_seconds: float = 30.0
    jitter: bool = True

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "RetryConfig":
        values = _mapping(values, "retry")
        _check_keys(values, set(cls.__dataclass_fields__), "retry")
        count = _optional_int(values.get("max_retries", 0), "retry.max_retries")
        if count is None or count > 10:
            raise ConfigurationError("retry.max_retries must be between 0 and 10")
        delays = {key: _optional_float(values.get(key, default), f"retry.{key}")
                  for key, default in (("initial_delay_seconds", .5), ("max_delay_seconds", 10.0), ("max_elapsed_seconds", 30.0))}
        if any(value is None for value in delays.values()) or delays["max_elapsed_seconds"] <= 0:
            raise ConfigurationError("retry delays must be finite numbers and max_elapsed_seconds must be positive")
        if delays["initial_delay_seconds"] > delays["max_delay_seconds"]:
            raise ConfigurationError("retry.initial_delay_seconds cannot exceed max_delay_seconds")
        jitter = values.get("jitter", True)
        if not isinstance(jitter, bool):
            raise ConfigurationError("retry.jitter must be a boolean")
        return cls(max_retries=count, jitter=jitter, **delays)


@dataclass(frozen=True)
class TimeoutConfig:
    run_seconds: float | None = None
    llm_seconds: float | None = None
    tool_seconds: float | None = None

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "TimeoutConfig":
        values = _mapping(values, "timeout")
        _check_keys(values, set(cls.__dataclass_fields__), "timeout")
        return cls(**{key: _optional_float(values.get(key), f"timeout.{key}") for key in cls.__dataclass_fields__})


@dataclass(frozen=True)
class ToolConfig:
    max_calls: int | None = None
    max_steps: int | None = None
    max_repeated_calls: int | None = None
    per_tool_limits: Mapping[str, int] = field(default_factory=dict)
    side_effects: Mapping[str, str] = field(default_factory=dict)
    idempotency_required_for: tuple[str, ...] = ()
    approval_required_for: tuple[str, ...] = ()
    approval_timeout_seconds: float | None = None

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "ToolConfig":
        values = _mapping(values, "tools")
        _check_keys(values, set(cls.__dataclass_fields__), "tools")
        limits = _mapping(values.get("per_tool_limits"), "tools.per_tool_limits")
        effects = _mapping(values.get("side_effects"), "tools.side_effects")
        if any(not isinstance(key, str) or not key.strip() for key in (*limits, *effects)):
            raise ConfigurationError("tools names must be nonempty strings")
        checked_limits = {key: _optional_int(value, f"tools.per_tool_limits.{key}") for key, value in limits.items()}
        if any(value is None for value in checked_limits.values()):
            raise ConfigurationError("per-tool limits must be nonnegative integers")
        if any(not isinstance(value, str) or value not in {"read", "write", "irreversible"} for value in effects.values()):
            raise ConfigurationError("tool side effects must be read, write, or irreversible")
        def tool_names(value: Any, name: str) -> tuple[str, ...]:
            if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) or not item.strip() or item != item.strip() for item in value):
                raise ConfigurationError(f"{name} 必须是工具名字符串列表")
            if len(set(value)) != len(value):
                raise ConfigurationError(f"{name} 不能包含重复工具名")
            return tuple(value)
        return cls(max_calls=_optional_int(values.get("max_calls"), "tools.max_calls"),
                   max_steps=_optional_int(values.get("max_steps"), "tools.max_steps"),
                   max_repeated_calls=_optional_int(values.get("max_repeated_calls"), "tools.max_repeated_calls"),
                   per_tool_limits=checked_limits, side_effects=dict(effects),
                   idempotency_required_for=tool_names(values.get("idempotency_required_for", []), "tools.idempotency_required_for"),
                   approval_required_for=tool_names(values.get("approval_required_for", []), "tools.approval_required_for"),
                   approval_timeout_seconds=_optional_float(values.get("approval_timeout_seconds"), "tools.approval_timeout_seconds"))


@dataclass(frozen=True)
class AgentGuardConfig:
    schema_version: int = SCHEMA_VERSION
    token: TokenConfig = field(default_factory=TokenConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    pricing: PricingConfig = field(default_factory=PricingConfig)
    degradation: DegradationConfig = field(default_factory=DegradationConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    timeout: TimeoutConfig = field(default_factory=TimeoutConfig)
    tools: ToolConfig = field(default_factory=ToolConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "AgentGuardConfig":
        values = _mapping(values, "configuration root")
        _check_keys(values, set(cls.__dataclass_fields__), "configuration")
        schema_version = values.get("schema_version", SCHEMA_VERSION)
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise ConfigurationError("schema_version 必须是整数")
        if schema_version != SCHEMA_VERSION:
            raise ConfigurationError(f"不支持的 schema_version：{schema_version}")
        return cls(
            schema_version=schema_version,
            token=TokenConfig.from_mapping(values.get("token")),
            budget=BudgetConfig.from_mapping(values.get("budget")),
            pricing=PricingConfig.from_mapping(values.get("pricing")),
            degradation=DegradationConfig.from_mapping(values.get("degradation")),
            rate_limit=RateLimitConfig.from_mapping(values.get("rate_limit")),
            telemetry=TelemetryConfig.from_mapping(values.get("telemetry")),
            timeout=TimeoutConfig.from_mapping(values.get("timeout")),
            tools=ToolConfig.from_mapping(values.get("tools")),
            retry=RetryConfig.from_mapping(values.get("retry")),
        )

    @classmethod
    def from_yaml(cls, path: str | Path) -> "AgentGuardConfig":
        from .loader import load_yaml_mapping

        return cls.from_mapping(load_yaml_mapping(path))
