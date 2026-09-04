from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from ..domain.exceptions import ConfigurationError
from .env import environment_mapping
from .merge import deep_merge
from .schema import (
    AgentGuardConfig,
    BudgetConfig,
    ModelPriceConfig,
    PricingConfig,
    RateLimitConfig,
    TelemetryConfig,
    TokenConfig,
)


def load_yaml_mapping(path: str | Path) -> Mapping[str, Any]:
    try:
        import yaml
    except ImportError as error:
        raise ConfigurationError("YAML support requires installing agentguard[yaml]") from error
    file_path = Path(path)
    try:
        values = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    except OSError as error:
        raise ConfigurationError(f"unable to read configuration: {file_path}") from error
    if not isinstance(values, Mapping):
        raise ConfigurationError("configuration root must be a mapping")
    return values


def load_config(
    values: Mapping[str, Any] | None = None,
    *,
    path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    use_environment: bool = True,
) -> AgentGuardConfig:
    if values is not None and path is not None:
        raise ConfigurationError("pass values or path, not both")
    source = dict(load_yaml_mapping(path) if path is not None else (values or {}))
    if use_environment:
        source = deep_merge(source, environment_mapping(environ))
    return AgentGuardConfig.from_mapping(source)


__all__ = [
    "AgentGuardConfig",
    "BudgetConfig",
    "ModelPriceConfig",
    "PricingConfig",
    "RateLimitConfig",
    "TelemetryConfig",
    "TokenConfig",
    "environment_mapping",
    "load_config",
    "load_yaml_mapping",
]
