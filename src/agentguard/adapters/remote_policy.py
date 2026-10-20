"""Authenticated, bounded policy transport and HMAC envelope verification.

Shared signing keys belong in a secret store; never put them in editable policy data.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..domain.exceptions import PolicyUnavailable


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def sign_policy(payload: dict, key: bytes, *, key_id: str = "primary") -> dict:
    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError("Policy signing key needs at least 32 random bytes")
    body = {**payload, "key_id": key_id}
    return {"payload": body, "signature": hmac.new(key, canonical(body), hashlib.sha256).hexdigest()}


class PolicyVerifier:
    def __init__(self, keys: dict[str, bytes]):
        if not keys or any(not isinstance(value, bytes) or len(value) < 32 for value in keys.values()):
            raise ValueError("Trusted policy keys need at least 32 random bytes")
        self.keys = dict(keys)

    def verify(self, envelope: dict) -> dict:
        try:
            if set(envelope) != {"payload", "signature"}:
                raise ValueError()
            payload, signature = envelope["payload"], envelope["signature"]
            if not isinstance(payload, dict) or not isinstance(signature, str) or len(signature) != 64:
                raise ValueError()
            key = self.keys[payload["key_id"]]
            raw = canonical(payload)
            if len(raw) > 1024 * 1024 or not hmac.compare_digest(hmac.new(key, raw, hashlib.sha256).hexdigest(), signature):
                raise ValueError()
            return json.loads(raw)
        except (KeyError, TypeError, ValueError, RecursionError, OverflowError):
            raise PolicyUnavailable("Policy signature or envelope is invalid") from None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class HTTPPolicyProvider:
    def __init__(self, url: str, *, token: str, timeout: float = 2, allow_loopback_http: bool = False):
        parsed = urlsplit(url)
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and not (allow_loopback_http and loopback and parsed.scheme == "http"):
            raise ValueError("Remote policy transport requires HTTPS")
        if not parsed.hostname or parsed.username or parsed.password or parsed.fragment or not token or len(token) > 256:
            raise ValueError("Invalid policy transport settings")
        if type(timeout) not in (int, float) or not 0 < timeout <= 10:
            raise ValueError("Policy timeout must be 0 to 10 seconds")
        self.url, self.token, self.timeout = url, token, timeout
        self._opener = build_opener(_NoRedirect())

    def fetch(self) -> dict:
        try:
            request = Request(self.url, headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"})
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ValueError()
                return json.loads(raw)
        except Exception:
            raise PolicyUnavailable("Remote policy endpoint unavailable") from None
