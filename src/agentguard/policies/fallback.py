"""Pure rules for switching model names on the existing provider endpoint."""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from ..config.schema import DATA_CLASSIFICATIONS, DegradationConfig


def request_capabilities(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> set[str]:
    required = {"text"}
    if any(kwargs.get(key) for key in ("tools", "functions", "tool_choice", "function_call")):
        required.add("tools")
    if kwargs.get("stream") is True:
        required.add("streaming")
    formats = [kwargs.get("response_format")]
    if isinstance(kwargs.get("text"), Mapping):
        formats.append(kwargs["text"].get("format"))
    for value in formats:
        if value is None:
            continue
        kind = value.get("type") if isinstance(value, Mapping) else None
        if isinstance(kind, str) and kind in {"json_schema", "json_object"}:
            required.add(kind)
        elif kind != "text":
            required.add("unsupported_format")
    modalities = kwargs.get("modalities")
    if modalities is not None:
        if not isinstance(modalities, (list, tuple)):
            required.add("unsupported_modality")
        else:
            for modality in modalities:
                required.add(modality if modality in ("text", "audio") else "unsupported_modality")
    # Inspect only content structure. Neither prompts nor media are logged.
    pending = [kwargs.get("messages", args[0] if args else None), kwargs.get("input")]
    seen: set[int] = set()
    while pending:
        value = pending.pop()
        if not isinstance(value, (Mapping, list, tuple)) or id(value) in seen:
            continue
        seen.add(id(value))
        if isinstance(value, Mapping):
            kind = value.get("type")
            if isinstance(kind, str):
                if kind in {"image_url", "input_image", "image"}:
                    required.add("vision")
                elif kind in {"input_audio", "audio", "audio_url"}:
                    required.add("audio")
                elif kind in {"tool_use", "tool_result", "function", "function_call", "function_call_output"}:
                    required.add("tools")
                elif kind not in {"text", "input_text", "output_text", "message"}:
                    required.add("unsupported_content")
            if value.get("role") in ("tool", "function") or value.get("tool_calls") or value.get("function_call"):
                required.add("tools")
            pending.extend(value.get(key) for key in ("content", "tool_calls"))
        else:
            pending.extend(value)
    return required


def fallback_rejection(config: DegradationConfig, args: tuple[Any, ...], kwargs: Mapping[str, Any],
                       estimated_cost: Decimal | None) -> str | None:
    profile = config.model_profiles.get(config.fallback_model)
    if profile is None:
        return "fallback_profile_missing"
    if (request_capabilities(args, kwargs) | set(config.required_capabilities)) - set(profile.capabilities):
        return "fallback_capability_mismatch"
    # All possible routing regions must be allowed; an unknown route is denied.
    if config.allowed_regions and (not profile.regions or set(profile.regions) - set(config.allowed_regions)):
        return "fallback_region_mismatch"
    if DATA_CLASSIFICATIONS.index(config.data_classification) > DATA_CLASSIFICATIONS.index(profile.max_data_classification):
        return "fallback_data_classification"
    if estimated_cost is None:
        return "fallback_price_missing"
    if config.max_cost_usd is not None and estimated_cost > Decimal(str(config.max_cost_usd)):
        return "fallback_cost_limit"
    return None
