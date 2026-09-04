from __future__ import annotations

from dataclasses import asdict, dataclass
from threading import RLock
from typing import Any, Mapping

from ..domain.exceptions import ConfigurationError
from .loader import load_config
from .merge import deep_merge
from .schema import AgentGuardConfig


@dataclass(frozen=True)
class ConfigSnapshot:
    """An immutable configuration view captured by one Run."""

    version: int
    config: AgentGuardConfig


class ConfigManager:
    """Atomically validates and publishes configuration snapshots."""

    def __init__(
        self,
        config: AgentGuardConfig | Mapping[str, Any] | None = None,
        *,
        config_path: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        if config is not None and config_path is not None:
            raise ConfigurationError("pass config or config_path, not both")
        if isinstance(config, AgentGuardConfig):
            initial = config
        else:
            initial = load_config(config, path=config_path, environ=environ)
        self._lock = RLock()
        self._snapshot = ConfigSnapshot(version=1, config=initial)
        self._environ = environ
        self._config_path = config_path

    @property
    def config(self) -> AgentGuardConfig:
        return self.snapshot().config

    def snapshot(self) -> ConfigSnapshot:
        with self._lock:
            return self._snapshot

    def replace(self, config: AgentGuardConfig | Mapping[str, Any]) -> ConfigSnapshot:
        """Validate and atomically publish a new configuration."""
        validated = config if isinstance(config, AgentGuardConfig) else AgentGuardConfig.from_mapping(config)
        with self._lock:
            self._snapshot = ConfigSnapshot(self._snapshot.version + 1, validated)
            return self._snapshot

    def update(self, values: Mapping[str, Any]) -> ConfigSnapshot:
        """Deep-merge and atomically validate a partial configuration update."""
        if not isinstance(values, Mapping):
            raise ConfigurationError("configuration update must be a mapping")
        current = asdict(self.config)
        return self.replace(deep_merge(current, values))

    def reload(self) -> ConfigSnapshot:
        """Load and publish the configured source, or raise without changing state."""
        if self._config_path is None:
            raise ConfigurationError("reload requires a config_path")
        loaded = load_config(path=self._config_path, environ=self._environ)
        return self.replace(loaded)
