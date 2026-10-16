from __future__ import annotations

from dataclasses import asdict, dataclass
from threading import RLock
from typing import Any, Mapping

from ..domain.exceptions import ConfigurationError, PolicyVersionConflict
from ..ports import PolicyStore
from .loader import load_config
from .merge import deep_merge
from .schema import AgentGuardConfig


@dataclass(frozen=True)
class ConfigSnapshot:
    """一次 Run 捕获的不可变配置视图。"""

    version: int
    config: AgentGuardConfig


ConfigVersionConflict = PolicyVersionConflict


class ConfigManager:
    """以原子方式校验并发布配置快照。"""

    def __init__(
        self,
        config: AgentGuardConfig | Mapping[str, Any] | None = None,
        *,
        config_path: str | None = None,
        environ: Mapping[str, str] | None = None,
        policy_store: PolicyStore | None = None,
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
        self._policy_store = policy_store
        if policy_store is not None:
            saved = policy_store.load()
            if saved is None:
                try:
                    policy_store.save(1, asdict(initial), expected_version=0)
                except PolicyVersionConflict:
                    saved = policy_store.load()
            if saved is not None:
                self._snapshot = ConfigSnapshot(saved[0], AgentGuardConfig.from_mapping(saved[1]))

    @property
    def config(self) -> AgentGuardConfig:
        return self.snapshot().config

    def snapshot(self) -> ConfigSnapshot:
        with self._lock:
            if self._policy_store is not None:
                saved = self._policy_store.load()
                if saved is not None and saved[0] != self._snapshot.version:
                    self._snapshot = ConfigSnapshot(saved[0], AgentGuardConfig.from_mapping(saved[1]))
            return self._snapshot

    def replace(self, config: AgentGuardConfig | Mapping[str, Any], *, expected_version: int | None = None) -> ConfigSnapshot:
        """校验并以原子方式发布新配置。"""
        validated = config if isinstance(config, AgentGuardConfig) else AgentGuardConfig.from_mapping(config)
        with self._lock:
            current = self.snapshot()
            if expected_version is not None and expected_version != current.version:
                raise ConfigVersionConflict("Policy version has changed; reload before saving")
            updated = ConfigSnapshot(current.version + 1, validated)
            if self._policy_store is not None:
                self._policy_store.save(updated.version, asdict(validated), expected_version=current.version)
            self._snapshot = updated
            return self._snapshot

    def update(self, values: Mapping[str, Any], *, expected_version: int | None = None) -> ConfigSnapshot:
        """递归合并并以原子方式校验部分配置更新。"""
        if not isinstance(values, Mapping):
            raise ConfigurationError("配置更新必须是映射类型")
        with self._lock:
            self.snapshot()
            if expected_version is not None and expected_version != self._snapshot.version:
                raise ConfigVersionConflict("Policy version has changed; reload before saving")
            current = asdict(self._snapshot.config)
            validated = AgentGuardConfig.from_mapping(deep_merge(current, values))
            updated = ConfigSnapshot(self._snapshot.version + 1, validated)
            if self._policy_store is not None:
                self._policy_store.save(updated.version, asdict(validated), expected_version=self._snapshot.version)
            self._snapshot = updated
            return updated

    def reload(self) -> ConfigSnapshot:
        """加载并发布配置源；失败时抛出异常且不改变当前状态。"""
        if self._config_path is None:
            raise ConfigurationError("reload 需要配置 config_path")
        loaded = load_config(path=self._config_path, environ=self._environ)
        return self.replace(loaded)
