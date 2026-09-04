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
        max_scope_keys = _optional_int(values.get("max_scope_keys"), "rate_limit.max_scope_keys")
        if max_scope_keys is None:
            max_scope_keys = 10_000
        if max_scope_keys <= 0:
            raise ConfigurationError("rate_limit.max_scope_keys must be greater than zero")
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
        raise ConfigurationError(f"{name} must be a mapping")
    return value


def _optional_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigurationError(f"{name} must be a non-negative integer or None")
    return value


def _optional_float(value: Any, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{name} must be a non-negative number or None")
    result = float(value)
    if result < 0 or not isfinite(result):
        raise ConfigurationError(f"{name} must be a finite non-negative number or None")
    return result


def _bounded_float(value: Any, name: str, default: float) -> float:
    result = _optional_float(default if value is None else value, name)
    assert result is not None
    if not 0 <= result <= 1:
        raise ConfigurationError(f"{name} must be between 0 and 1")
    return result


def _check_keys(values: Mapping[str, Any], allowed: set[str], name: str) -> None:
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ConfigurationError(f"unknown {name} field(s): {', '.join(unknown)}")


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
        raise ConfigurationError(f"{name} must be a non-empty list of scope fields")
    fields = tuple(value)
    if any(not isinstance(item, str) or item not in SCOPE_FIELDS for item in fields):
        raise ConfigurationError(f"{name} contains an unsupported scope field")
    if len(set(fields)) != len(fields):
        raise ConfigurationError(f"{name} must not contain duplicate scope fields")
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
            raise ConfigurationError("token.on_exceeded must be reject, downgrade, or stop_stream")
        if config.missing_usage not in {"estimate", "unknown", "reject"}:
            raise ConfigurationError("token.missing_usage must be estimate, unknown, or reject")
        if config.usage_mismatch not in {"observe", "reject"}:
            raise ConfigurationError("token.usage_mismatch must be observe or reject")
        if config.missing_price not in {"observe", "reject"}:
            raise ConfigurationError("token.missing_price must be observe or reject")
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
        max_scope_keys = _optional_int(values.get("max_scope_keys"), "rate_limit.max_scope_keys")
        if max_scope_keys is None:
            max_scope_keys = 10_000
        if max_scope_keys <= 0:
            raise ConfigurationError("rate_limit.max_scope_keys must be greater than zero")
        return cls(
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
            raise ConfigurationError("pricing.version must be a non-empty string")
        models = _mapping(values.get("models"), "pricing.models")
        return cls(version=version, models={
            str(model): ModelPriceConfig.from_mapping(price)
            for model, price in models.items()
        })


@dataclass(frozen=True)
class DegradationConfig:
    fallback_model: str | None = None

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "DegradationConfig":
        values = _mapping(values, "degradation")
        _check_keys(values, set(cls.__dataclass_fields__), "degradation")
        fallback_model = values.get("fallback_model")
        if fallback_model is not None and (not isinstance(fallback_model, str) or not fallback_model.strip()):
            raise ConfigurationError("degradation.fallback_model must be a non-empty string or None")
        return cls(fallback_model=fallback_model)


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
                raise ConfigurationError(f"telemetry.{key} must be a boolean")
        return cls(
            verbose=values.get("verbose", True),
            enable_metrics_export=values.get("enable_metrics_export", False),
        )


@dataclass(frozen=True)
class AgentGuardConfig:
    schema_version: int = SCHEMA_VERSION
    token: TokenConfig = field(default_factory=TokenConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    pricing: PricingConfig = field(default_factory=PricingConfig)
    degradation: DegradationConfig = field(default_factory=DegradationConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "AgentGuardConfig":
        values = _mapping(values, "configuration root")
        _check_keys(values, {"schema_version", "token", "budget", "pricing", "degradation", "rate_limit", "telemetry"}, "configuration")
        schema_version = values.get("schema_version", SCHEMA_VERSION)
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise ConfigurationError("schema_version must be an integer")
        if schema_version != SCHEMA_VERSION:
            raise ConfigurationError(f"unsupported schema_version: {schema_version}")
        return cls(
            schema_version=schema_version,
            token=TokenConfig.from_mapping(values.get("token")),
            budget=BudgetConfig.from_mapping(values.get("budget")),
            pricing=PricingConfig.from_mapping(values.get("pricing")),
            degradation=DegradationConfig.from_mapping(values.get("degradation")),
            rate_limit=RateLimitConfig.from_mapping(values.get("rate_limit")),
            telemetry=TelemetryConfig.from_mapping(values.get("telemetry")),
        )

    @classmethod
    def from_yaml(cls, path: str | Path) -> "AgentGuardConfig":
        from .loader import load_yaml_mapping

        return cls.from_mapping(load_yaml_mapping(path))
