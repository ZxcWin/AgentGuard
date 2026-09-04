from __future__ import annotations

import json
import os
from typing import Any, Mapping

from ..domain.exceptions import ConfigurationError

PREFIX = "AGENTGUARD_"


def _parse_value(value: str) -> Any:
    lowered = value.strip().lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none"}:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def environment_mapping(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    source = os.environ if environ is None else environ
    result: dict[str, Any] = {}
    for key, value in source.items():
        if not key.startswith(PREFIX):
            continue
        path = key[len(PREFIX):].lower().split("__")
        if not path or any(not part for part in path):
            raise ConfigurationError(f"invalid environment variable name: {key}")
        current = result
        for part in path[:-1]:
            existing = current.setdefault(part, {})
            if not isinstance(existing, dict):
                raise ConfigurationError(f"conflicting environment variable path: {key}")
            current = existing
        current[path[-1]] = _parse_value(value)
    return result
