"""Exact-cache orchestration. All backend failures become ordinary misses."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, replace
import hashlib
import hmac
import json
import secrets
import weakref
from threading import RLock
from time import monotonic, sleep, time
import math

from ..adapters.storage.exact_cache import MemoryExactCache
from ..adapters.storage.sqlite_cache import SQLiteExactCache
from ..domain.cache import CacheRequest
from ..ports import ExactCacheStore
from ..policies.cache import Uncacheable, validate_content, encode
from .deadlines import inherited_deadline
from .guards import check_quality, check_security

MISS = object()


class CacheRuntime:
    def __init__(self, store: ExactCacheStore | None = None, *, signing_key: bytes | None = None):
        self.store = store if store is not None else MemoryExactCache()
        if signing_key is not None and (type(signing_key) is not bytes or not 32 <= len(signing_key) <= 4096):
            raise ValueError("Cache signing_key must contain at least 32 bytes")
        if signing_key is not None and not callable(getattr(self.store, "generation", None)):
            raise ValueError("Stable cache keys require a store with shared invalidation generations")
        self.stable = signing_key is not None
        self.secret = signing_key if self.stable else secrets.token_bytes(32)
        self.lock = RLock()
        self.owners = {}
        self.providers = {}

    def epoch(self):
        generation = getattr(self.store, "generation", None)
        return hmac.new(self.secret, str(generation()).encode(), hashlib.sha256).digest() if generation is not None else self.secret

    def now(self):
        return time() if self.stable else monotonic()

    def provider_key(self, function):
        with self.lock:
            key = id(function)
            if key not in self.providers:
                if len(self.providers) >= 1024:
                    raise Uncacheable("provider_capacity")
                def remove(reference):
                    with self.lock:
                        current = self.providers.get(key)
                        if current is not None and current[0] is reference:
                            self.providers.pop(key, None)
                try:
                    reference = weakref.ref(function, remove)
                except TypeError:
                    raise Uncacheable("unsupported_provider") from None
                self.providers[key] = (reference, secrets.token_hex(16))
            return self.providers[key][1]

    def claim(self, key):
        with self.lock:
            if key in self.owners:
                return None
            if len(self.owners) >= 1024:
                raise Uncacheable("inflight_capacity")
            claim = getattr(self.store, "claim", None)
            try:
                token = claim(key) if claim is not None else object()
            except Exception:
                raise Uncacheable("cache_claim_failed") from None
            if token is None:
                return None
            self.owners[key] = token
            return token

    def release(self, key, token):
        with self.lock:
            if self.owners.get(key) is token:
                self.owners.pop(key, None)
                release = getattr(self.store, "release", None)
                if release is not None:
                    try:
                        release(key, token)
                    except Exception:
                        pass  # The bounded lease will expire without business replay.

    def clear(self):
        # Rotate the key as well: in-flight work cannot repopulate accessible
        # entries after an explicit invalidation.
        with self.lock:
            if not self.stable:
                self.secret = secrets.token_bytes(32)
            self.providers.clear()
            self.store.clear()


@dataclass
class CacheTicket:
    runtime: CacheRuntime
    context: object
    prepared: object
    key: str
    epoch: bytes
    token: object = None
    payload: bytes | None = None
    tools_before: int = 0
    calls_before: int = 0

    def event(self, name, reason=None):
        self.context.state.record(f"cache.{name}", request_id=self.prepared.call_context.request_id,
                                  parent_id=self.prepared.call_context.parent_id,
                                  model=self.prepared.model,
                                  **({"reason_code": reason} if reason else {}))

    def release(self):
        self.runtime.release(self.key, self.token)

    def read(self):
        try:
            if self.epoch != self.runtime.epoch():
                return MISS
            payload = self.runtime.store.get(self.key)
            if payload is None:
                return MISS
            if self.epoch != self.runtime.epoch():
                return MISS
            if type(payload) is not bytes or len(payload) > 200000:
                raise Uncacheable("invalid_entry")
            # Stored JSON is a signed envelope so a shared/custom store cannot
            # substitute another response or another tenant's payload.
            envelope = json.loads(payload)
            raw = envelope["response"].encode()
            expires = envelope["expires"]
            issued, clock = envelope["issued"], envelope["clock"]
            signed = encode([self.key, envelope["response"], issued, expires, clock])
            expected = hmac.new(self.epoch, signed, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, envelope["signature"]):
                raise Uncacheable("invalid_entry")
            now = self.runtime.now()
            if (clock != ("unix" if self.runtime.stable else "monotonic") or
                type(issued) not in {int, float} or type(expires) not in {int, float} or
                not math.isfinite(issued) or not math.isfinite(expires) or not 0 < expires - issued <= 3600 or
                now < issued or now >= expires):
                self.runtime.store.delete(self.key)
                return MISS
            response = json.loads(raw)
            validate_content(response, response=True)
            if type(response) not in {str, dict}:
                raise Uncacheable("unsupported_response")
            return response
        except Exception:
            self.event("error", "cache_read_failed")
            try:
                self.runtime.store.delete(self.key)
            except Exception:
                pass
            return MISS

    def capture(self, response):
        try:
            if type(response) not in {str, dict}:
                raise Uncacheable("unsupported_response")
            self.payload = validate_content(response, response=True)
        except Uncacheable as error:
            self.event("bypassed", str(error))

    def publish(self):
        try:
            unsafe = any(e.get("request_id") == self.prepared.call_context.request_id and
                         e["event"] in {"security.finding", "security.rejected", "quality.finding", "quality.rejected"}
                         for e in self.context.state.events)
            if self.epoch != self.runtime.epoch():
                self.event("bypassed", "invalidated")
            elif unsafe or self.context.state.tool_calls != self.tools_before or self.context.state.llm_calls != self.calls_before + 1:
                self.event("bypassed", "guard_or_tool_activity")
            elif self.payload is not None:
                issued = self.runtime.now()
                expires = issued + self.context.config.cache.ttl_seconds
                clock = "unix" if self.runtime.stable else "monotonic"
                signed = encode([self.key, self.payload.decode(), issued, expires, clock])
                signature = hmac.new(self.epoch, signed, hashlib.sha256).hexdigest()
                envelope = encode({"response": self.payload.decode(), "signature": signature, "issued": issued, "expires": expires, "clock": clock})
                self.runtime.store.set(self.key, envelope, ttl_seconds=self.context.config.cache.ttl_seconds)
                self.event("stored")
        except Exception:
            self.event("error", "cache_write_failed")
        finally:
            self.release()


def prepare_cache(context, prepared, options):
    if options is None:
        return None
    if not isinstance(options, CacheRequest):
        raise TypeError("cache_options must be CacheRequest or None")
    try:
        if not context.config.cache.enabled or options.bypass:
            raise Uncacheable("disabled" if not context.config.cache.enabled else "explicit_bypass")
        if any(e.get("request_id") == prepared.call_context.request_id and e["event"] in {"security.finding", "security.redacted"}
               for e in context.state.events):
            raise Uncacheable("security_finding")
        if not options.pure:
            raise Uncacheable("purity_not_declared")
        if options.data_classification not in {"public", "internal"} or context.config.degradation.data_classification not in {"public", "internal"}:
            raise Uncacheable("data_classification")
        if prepared.kwargs.get("stream") is True:
            raise Uncacheable("streaming")
        if prepared.model not in context.config.cache.allowed_models:
            raise Uncacheable("model_not_allowed")
        if context.interceptors.interceptors:
            raise Uncacheable("interceptors")
        # Explicit scope, including user authorization revision, is mandatory.
        scope = [context.state.tenant_id, context.state.project_id, context.state.environment, context.state.user_id]
        if any(type(value) is not str or not value.strip() for value in scope):
            raise Uncacheable("missing_identity")
        validate_content([list(prepared.args), prepared.kwargs])
        runtime = context.cache_runtime
        if isinstance(runtime.store, (MemoryExactCache, SQLiteExactCache)):
            runtime.store.configure(max_entries=context.config.cache.max_entries, max_bytes=context.config.cache.max_bytes)
        material = encode({"scope": scope, "agent": context.state.agent_id, "options": asdict(options),
                           "policy": json.loads(json.dumps(asdict(context.config))), "version": context.config_version,
                           "provider": options.provider_id if runtime.stable and options.provider_id else runtime.provider_key(prepared.function),
                           "args": list(prepared.args), "kwargs": prepared.kwargs})
        epoch = runtime.epoch()
        key = hmac.new(epoch, material, hashlib.sha256).hexdigest()
        return CacheTicket(runtime, context, prepared, key, epoch, tools_before=context.state.tool_calls,
                           calls_before=context.state.llm_calls)
    except Uncacheable as error:
        context.state.record("cache.bypassed", request_id=prepared.call_context.request_id, reason_code=str(error))
        return None
    except Exception:
        context.state.record("cache.error", request_id=prepared.call_context.request_id, reason_code="cache_prepare_failed")
        return None


def deliver_hit(ticket, response):
    context = ticket.context
    metadata = replace(ticket.prepared.call_context, call_type="cache")
    try:
        inherited_deadline(context.deadline).check()
        if context.security_guard is not None:
            response = check_security(context, metadata, response, stage="response")
        if context.quality_guard is not None:
            check_quality(context, metadata, response)
        if any(e.get("request_id") == metadata.request_id and e["event"] in {"security.finding", "quality.finding"}
               for e in context.state.events):
            ticket.runtime.store.delete(ticket.key)
            ticket.event("bypassed", "current_guard_finding")
            return MISS
        inherited_deadline(context.deadline).check()
        if ticket.epoch != ticket.runtime.epoch():
            ticket.event("bypassed", "invalidated")
            return MISS
        ticket.event("hit")
        # Provider usage in the cached response describes its original request;
        # no provider work was incurred by this invocation.
        return response
    except BaseException:
        try:
            ticket.runtime.store.delete(ticket.key)
        except Exception:
            ticket.event("error", "cache_delete_failed")
        raise


def lookup(ticket):
    if ticket is None:
        return MISS
    end = monotonic() + 1.0  # Bounded coalescing; timeout falls back to normal admission.
    while True:
        inherited_deadline(ticket.context.deadline).check()
        response = ticket.read()
        if response is not MISS:
            return deliver_hit(ticket, response)
        try:
            ticket.token = ticket.runtime.claim(ticket.key)
        except Uncacheable:
            ticket.event("bypassed", "inflight_capacity")
            return MISS
        if ticket.token is not None:
            response = ticket.read()
            if response is not MISS:
                ticket.release()
                return deliver_hit(ticket, response)
            ticket.event("miss")
            return MISS
        if monotonic() >= end:
            ticket.event("bypassed", "coalescing_timeout")
            return MISS
        sleep(.01)


async def async_lookup(ticket):
    if ticket is None:
        return MISS
    end = monotonic() + 1.0
    while True:
        inherited_deadline(ticket.context.deadline).check()
        response = ticket.read()
        if response is not MISS:
            return deliver_hit(ticket, response)
        try:
            ticket.token = ticket.runtime.claim(ticket.key)
        except Uncacheable:
            ticket.event("bypassed", "inflight_capacity")
            return MISS
        if ticket.token is not None:
            response = ticket.read()
            if response is not MISS:
                ticket.release()
                return deliver_hit(ticket, response)
            ticket.event("miss")
            return MISS
        if monotonic() >= end:
            ticket.event("bypassed", "coalescing_timeout")
            return MISS
        await asyncio.sleep(.01)
