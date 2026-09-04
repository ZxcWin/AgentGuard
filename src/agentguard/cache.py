"""Backward-compatible imports for the in-memory cache adapter."""

from .adapters.storage.memory import CacheEntry, MemoryCache, make_cache_key

__all__ = ["CacheEntry", "MemoryCache", "make_cache_key"]
