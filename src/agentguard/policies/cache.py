"""Pure cache eligibility and bounded canonical JSON; no arbitrary serializers."""
import json
import math
import re
from typing import Any

from .security import SecurityGuard

MAX_PAYLOAD = 200_000
_PROHIBITED_KEYS = {"authorization", "apikey", "password", "secret", "accesstoken", "token", "session",
                    "refreshtoken", "sessiontoken", "approval", "approvaltoken", "credentials",
                    "tools", "toolcalls", "toolcallid", "toolchoice", "functions", "functioncall"}
_SCANNER = SecurityGuard(detect_pii=True, max_chars=MAX_PAYLOAD)


class Uncacheable(ValueError):
    pass


def encode(value: Any) -> bytes:
    nodes = 0
    chars = 0
    def walk(item, depth=0):
        nonlocal nodes, chars
        nodes += 1
        if nodes > 10000 or depth > 32:
            raise Uncacheable("structure_limit")
        if type(item) is str:
            chars += len(item)
            if chars > MAX_PAYLOAD:
                raise Uncacheable("payload_limit")
        elif item is None or type(item) in {bool, int}:
            if type(item) is int and item.bit_length() > 256:
                raise Uncacheable("structure_limit")
        elif type(item) is float:
            if not math.isfinite(item):
                raise Uncacheable("unsupported_value")
        elif type(item) is list:
            for child in item:
                walk(child, depth + 1)
        elif type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise Uncacheable("unsupported_value")
                walk(key, depth + 1)
                walk(child, depth + 1)
        else:
            raise Uncacheable("unsupported_value")
    walk(value)
    result = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
    if len(result) > MAX_PAYLOAD:
        raise Uncacheable("payload_limit")
    return result


def validate_content(value: Any, *, response=False) -> bytes:
    encoded = encode(value)
    texts = []
    def inspect(item):
        start = len(texts)
        if type(item) is str:
            texts.append(item)
            if not _SCANNER.inspect(item).passed:
                raise Uncacheable("sensitive_content")
        elif type(item) is list:
            for child in item:
                inspect(child)
        elif type(item) is dict:
            for key, child in item.items():
                normalized = re.sub(r"[^a-z0-9]", "", key.lower())
                if normalized in _PROHIBITED_KEYS:
                    raise Uncacheable("prohibited_content")
                if key == "role" and child in ("tool", "function"):
                    raise Uncacheable("tool_content")
                if normalized in {"classification", "dataclassification"} and child in ("confidential", "restricted"):
                    raise Uncacheable("data_classification")
                if response and ((key == "error" and child is not None) or
                                 (key == "finish_reason" and child not in ("stop", "end_turn")) or
                                 (key == "status" and child not in ("completed", "succeeded"))):
                    raise Uncacheable("incomplete_response")
                inspect(key)
                inspect(child)
        if type(item) in {list, dict} and not _SCANNER.inspect("".join(texts[start:])).passed:
            raise Uncacheable("sensitive_content")
    inspect(value)
    if not _SCANNER.inspect("".join(texts)).passed or not _SCANNER.inspect(" ".join(texts)).passed:
        raise Uncacheable("sensitive_content")
    return encoded
