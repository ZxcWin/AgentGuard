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
    """一次 Run 捕获的不可变配置视图。"""

    version: int
    config: AgentGuardConfig


class ConfigManager:
    """以原子方式校验并发布配置快照。"""

    def __init__(
        self,
        config: AgentGuardConfig | Mapping[str, Any] | None = None,
        *,
        config_path: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        if config is not None and config_path is not None:
            raise ConfigurationError("config 和 config_path 不能同时传入")
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
        """校验并以原子方式发布新配置。"""
        validated = config if isinstance(config, AgentGuardConfig) else AgentGuardConfig.from_mapping(config)
        with self._lock:
            self._snapshot = ConfigSnapshot(self._snapshot.version + 1, validated)
            return self._snapshot

    def update(self, values: Mapping[str, Any]) -> ConfigSnapshot:
        """递归合并并以原子方式校验部分配置更新。"""
        if not isinstance(values, Mapping):
            raise ConfigurationError("配置更新必须是映射类型")
        current = asdict(self.config)
        return self.replace(deep_merge(current, values))

    def reload(self) -> ConfigSnapshot:
        """加载并发布配置源；失败时抛出异常且不改变当前状态。"""
        if self._config_path is None:
            raise ConfigurationError("reload 需要配置 config_path")
        loaded = load_config(path=self._config_path, environ=self._environ)
        return self.replace(loaded)
