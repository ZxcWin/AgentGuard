"""Bounded extraction of model text, without repr/str of provider objects."""
from __future__ import annotations

from typing import Any


class ContentLimit(ValueError):
    pass


def text_content(value: Any, max_chars: int, *, include_keys: bool = False) -> str:
    parts: list[str] = []
    remaining, nodes = max_chars, 20_000
    seen: set[int] = set()

    def visit(item: Any, depth: int = 0) -> None:
        nonlocal remaining, nodes
        nodes -= 1
        if nodes < 0 or depth > 64:
            raise ContentLimit("content traversal limit exceeded")
        if isinstance(item, str):
            remaining -= len(item)
            if remaining < 0:
                raise ContentLimit("content character limit exceeded")
            parts.append(item)
            return
        if isinstance(item, (int, float, bool, bytes, bytearray, type(None))):
            return
        identity = id(item)
        if identity in seen:
            return
        seen.add(identity)
        try:
            if isinstance(item, dict):
                for key, child in item.items():
                    if include_keys:
                        visit("\n", depth + 1)
                        visit(key, depth + 1)
                        visit("\n", depth + 1)
                    visit(child, depth + 1)
                    if include_keys:
                        visit("\n", depth + 1)
            elif isinstance(item, (list, tuple, set, frozenset)):
                for child in item:
                    visit(child, depth + 1)
            else:
                # Standard SDK response/message/delta fields only. Never dump
                # arbitrary object attributes (clients may contain credentials).
                for key in ("choices", "message", "delta", "content", "text", "output",
                            "tool_calls", "function", "arguments", "refusal"):
                    child = getattr(item, key, None)
                    if child is not None:
                        visit(child, depth + 1)
        finally:
            seen.remove(identity)

    visit(value)
    return "".join(parts)
