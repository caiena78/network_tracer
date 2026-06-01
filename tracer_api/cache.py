"""
tracer_api/cache.py
===================
Lightweight TTL-based in-memory caches.

Two distinct caches are provided:
  - NetboxPrefixCache  – caches the result of "which prefixes contain IP X"
                         so repeated traces to the same subnet skip the
                         NetBox API round-trip.
  - TraceResultCache   – caches completed (src_ip, dst_ip) flat-path results
                         so identical back-to-back traces return instantly.

Both are thread-safe and self-expiring (checked on access, background sweeper
in main.py handles periodic cleanup).
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Tuple


class _TTLEntry:
    __slots__ = ("value", "expires_at")

    def __init__(self, value: Any, ttl: float) -> None:
        self.value      = value
        self.expires_at = time.monotonic() + ttl


class _TTLCache:
    """Generic TTL cache. Thread-safe; all operations are O(1) amortised."""

    def __init__(self, ttl_seconds: float, max_size: int = 512) -> None:
        self._ttl      = ttl_seconds
        self._max      = max_size
        self._data: Dict[Any, _TTLEntry] = {}
        self._lock     = threading.Lock()

    # ------------------------------------------------------------------

    def get(self, key: Any) -> Optional[Any]:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            if time.monotonic() > entry.expires_at:
                del self._data[key]
                return None
            return entry.value

    def set(self, key: Any, value: Any) -> None:
        if self._ttl <= 0:
            return
        with self._lock:
            # Evict oldest entry when the cache is full.
            if len(self._data) >= self._max and key not in self._data:
                oldest = min(self._data, key=lambda k: self._data[k].expires_at)
                del self._data[oldest]
            self._data[key] = _TTLEntry(value, self._ttl)

    def invalidate(self, key: Any) -> None:
        with self._lock:
            self._data.pop(key, None)

    def sweep(self) -> int:
        """Remove all expired entries. Returns the number removed."""
        now = time.monotonic()
        expired = []
        with self._lock:
            for k, e in self._data.items():
                if now > e.expires_at:
                    expired.append(k)
            for k in expired:
                del self._data[k]
        return len(expired)

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._data)


# ---------------------------------------------------------------------------
# Domain-specific cache wrappers
# ---------------------------------------------------------------------------

class NetboxPrefixCache:
    """
    Caches the list of NetBox prefixes that contain a given IP address.

    Key:  (netbox_url, contains_ip)
    Value: list[str]  — e.g. ["10.254.28.0/22", "10.0.0.0/8", ...]
    """

    def __init__(self, ttl_seconds: float = 300) -> None:
        self._inner: _TTLCache = _TTLCache(ttl_seconds)

    def get(self, netbox_url: str, src_ip: str) -> Optional[List[str]]:
        return self._inner.get((netbox_url, src_ip))

    def set(self, netbox_url: str, src_ip: str, prefixes: List[str]) -> None:
        self._inner.set((netbox_url, src_ip), prefixes)

    def sweep(self) -> int:
        return self._inner.sweep()

    @property
    def size(self) -> int:
        return self._inner.size


class TraceResultCache:
    """
    Caches completed flat-path results so an identical (src, dst) trace
    returns immediately from memory instead of re-running all SSH commands.

    Key:  (src_ip, dst_ip, netbox_url)
    Value: list[dict]  — the flat paths returned by build_flat_paths()
    """

    def __init__(self, ttl_seconds: float = 600) -> None:
        self._inner: _TTLCache = _TTLCache(ttl_seconds)

    def _key(self, src_ip: str, dst_ip: str, netbox_url: str) -> Tuple:
        return (src_ip, dst_ip, netbox_url)

    def get(
        self,
        src_ip: str,
        dst_ip: str,
        netbox_url: str,
    ) -> Optional[List[dict]]:
        return self._inner.get(self._key(src_ip, dst_ip, netbox_url))

    def set(
        self,
        src_ip: str,
        dst_ip: str,
        netbox_url: str,
        result: List[dict],
    ) -> None:
        self._inner.set(self._key(src_ip, dst_ip, netbox_url), result)

    def invalidate(
        self,
        src_ip: str,
        dst_ip: str,
        netbox_url: str,
    ) -> None:
        """Remove the cached result for a specific (src, dst) pair."""
        self._inner.invalidate(self._key(src_ip, dst_ip, netbox_url))

    def clear_all(self) -> int:
        """Remove every cached result. Returns the number of entries cleared."""
        with self._inner._lock:
            count = len(self._inner._data)
            self._inner._data.clear()
        return count

    def sweep(self) -> int:
        return self._inner.sweep()

    @property
    def size(self) -> int:
        return self._inner.size


# ---------------------------------------------------------------------------
# Module-level singletons — configured lazily from settings
# ---------------------------------------------------------------------------

_netbox_cache:  Optional[NetboxPrefixCache] = None
_result_cache:  Optional[TraceResultCache]  = None
_cache_lock     = threading.Lock()


def get_netbox_cache() -> NetboxPrefixCache:
    global _netbox_cache
    if _netbox_cache is None:
        with _cache_lock:
            if _netbox_cache is None:
                from .config import settings
                _netbox_cache = NetboxPrefixCache(settings.netbox_prefix_cache_ttl)
    return _netbox_cache


def get_result_cache() -> TraceResultCache:
    global _result_cache
    if _result_cache is None:
        with _cache_lock:
            if _result_cache is None:
                from .config import settings
                _result_cache = TraceResultCache(settings.trace_result_cache_ttl)
    return _result_cache


def sweep_all() -> None:
    """Sweep expired entries from all caches.  Called by the background housekeeping task."""
    get_netbox_cache().sweep()
    get_result_cache().sweep()
