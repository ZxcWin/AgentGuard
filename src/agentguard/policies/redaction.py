"""Bounded structure-preserving redaction for plain text and JSON values."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re

from .content import ContentLimit

SECRET_FIELDS = frozenset({"apikey", "secret", "token", "password", "authorization", "accesstoken", "refreshtoken"})
_MARKER = re.compile(r"\[REDACTED:(?:secret|pii)\.[a-z_]+:[A-P]{24}\]")


def invalid_constant(value):
    raise ValueError("non-finite JSON")


def secret_field(key):
    return type(key) is str and key.lower().replace("_", "").replace("-", "") in SECRET_FIELDS


def structured_secret(value):
    """No arbitrary attributes or repr; the main scanner already bounds content."""
    remaining = 20_000
    active = set()
    def visit(item, depth=0):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 64:
            raise ContentLimit()
        if type(item) is str and item.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(item, parse_constant=invalid_constant)
            except (ValueError, RecursionError):
                return False
            return visit(parsed, depth + 1) if type(parsed) in {dict, list} else False
        if type(item) not in {dict, list, tuple} or id(item) in active:
            return False
        active.add(id(item))
        try:
            if type(item) is dict:
                for key, child in item.items():
                    if secret_field(key) and type(child) is str and child and not is_marker(child):
                        return True
                    if visit(child, depth + 1):
                        return True
            else:
                return any(visit(child, depth + 1) for child in item)
            return False
        finally:
            active.remove(id(item))
    return visit(value)


def is_marker(value):
    # Existing replacement markers are ordinary untrusted text. Recognizing
    # them only prevents repeatedly replacing a whole credential field.
    return _MARKER.fullmatch(value) is not None


def redact(value, *, patterns, key, max_chars, detect_secrets):
    remaining, nodes = max_chars, 20_000
    active = set()
    count = 0

    def marker(code, text):
        nonlocal count
        count += 1
        digest = hmac.new(key, (code + "\0" + text).encode(), hashlib.sha256).hexdigest()[:24]
        # Letters avoid accidentally creating a phone-number finding in a hash.
        opaque = digest.translate(str.maketrans("0123456789abcdef", "ABCDEFGHIJKLMNOP"))
        return f"[REDACTED:{code}:{opaque}]"

    def text(item, depth):
        nonlocal remaining
        remaining -= len(item)
        if remaining < 0:
            raise ContentLimit()
        # Do not damage quotes/escapes inside serialized structured parameters.
        if item.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(item, parse_constant=invalid_constant)
            except (ValueError, RecursionError):
                parsed = None
            if type(parsed) in {dict, list}:
                before = count
                converted = visit(parsed, depth + 1)
                return json.dumps(converted, ensure_ascii=False, separators=(",", ":")) if count != before else item
        spans = []
        for code, pattern in patterns:
            spans.extend((match.start(), match.end(), code) for match in pattern.finditer(item))
        # The first rule wins overlapping ranges; merge ranges so a shorter
        # overlapping match cannot leave part of a credential visible.
        merged = []
        for start, end, code in sorted(spans):
            if merged and start < merged[-1][1]:
                old_start, old_end, old_code = merged[-1]
                merged[-1] = (old_start, max(old_end, end), old_code)
            else:
                merged.append((start, end, code))
        pieces, end = [], 0
        for start, stop, code in merged:
            pieces.extend((item[end:start], marker(code, item[start:stop])))
            end = stop
        pieces.append(item[end:])
        return "".join(pieces)

    def visit(item, depth=0):
        nonlocal nodes, remaining
        nodes -= 1
        if nodes < 0 or depth > 64:
            raise ContentLimit()
        if type(item) is str:
            return text(item, depth)
        if item is None or type(item) in {int, bool} or (type(item) is float and math.isfinite(item)):
            return item
        if type(item) not in {dict, list, tuple} or id(item) in active:
            raise ContentLimit()
        active.add(id(item))
        try:
            if type(item) is dict:
                result = {}
                for name, child in item.items():
                    if type(name) is not str or any(pattern.search(name) for _, pattern in patterns):
                        # Renaming keys can change authorization or tool semantics.
                        raise ContentLimit()
                    remaining -= len(name)
                    if remaining < 0:
                        raise ContentLimit()
                    if detect_secrets and secret_field(name) and type(child) is str and child and not is_marker(child):
                        remaining -= len(child)
                        if remaining < 0:
                            raise ContentLimit()
                        result[name] = marker("secret.field", child)
                    else:
                        result[name] = visit(child, depth + 1)
                return result
            converted = [visit(child, depth + 1) for child in item]
            return tuple(converted) if type(item) is tuple else converted
        finally:
            active.remove(id(item))

    return visit(value), count
