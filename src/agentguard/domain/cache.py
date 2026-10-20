"""Explicit cache consent. Application authorization still runs before invoke."""
from dataclasses import dataclass


@dataclass(frozen=True)
class CacheRequest:
    namespace: str
    prompt_version: str
    authorization_context: str
    data_classification: str
    pure: bool = False
    bypass: bool = False
    provider_id: str | None = None

    def __post_init__(self) -> None:
        if self.provider_id is not None and (type(self.provider_id) is not str or not self.provider_id.strip() or len(self.provider_id) > 256):
            raise ValueError("Stable cache provider_id must be a nonempty string up to 256 characters")
        for value in (self.namespace, self.prompt_version, self.authorization_context):
            if type(value) is not str or not value.strip() or len(value) > 256:
                raise ValueError("Cache identity fields must be nonempty strings up to 256 characters")
        if self.data_classification not in {"public", "internal", "confidential", "restricted"}:
            raise ValueError("Invalid cache data classification")
        if type(self.pure) is not bool or type(self.bypass) is not bool:
            raise ValueError("Cache pure/bypass flags must be booleans")


@dataclass(frozen=True)
class ToolCacheRequest(CacheRequest):
    """Explicit pure read consent; version both tool code and underlying data."""

    tool_version: str = ""
    data_version: str = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        for value in (self.tool_version, self.data_version):
            if type(value) is not str or not value.strip() or len(value) > 256:
                raise ValueError("Tool cache requires tool_version and data_version up to 256 characters")
