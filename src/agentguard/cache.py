"""内存缓存适配器的向后兼容导入。"""

from .adapters.storage.memory import CacheEntry, MemoryCache, make_cache_key

__all__ = ["CacheEntry", "MemoryCache", "make_cache_key"]
