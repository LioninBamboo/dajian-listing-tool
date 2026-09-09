"""
Tiny in-process TTL cache for Terapeak/eBay Browse responses.

Why not cachetools? Avoid adding a dependency for ~30 lines of code.
Why not lru_cache? It can't key on dict params and has no TTL.

Thread-safe enough for our usage (Streamlit + ThreadPoolExecutor):
the worst case on a race is one extra upstream call.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Optional


def _default_ttl() -> int:
    try:
        return max(0, int(os.getenv("TERAPEAK_CACHE_TTL_SECONDS", "900")))
    except ValueError:
        return 900


class TTLCache:
    def __init__(self, maxsize: int = 512, ttl: Optional[int] = None):
        self.maxsize = maxsize
        self.ttl = _default_ttl() if ttl is None else ttl
        self._store: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def _make_key(self, *parts: Any) -> str:
        # Stable JSON for dict params; sort keys.
        norm = []
        for p in parts:
            if isinstance(p, dict):
                norm.append(json.dumps(p, sort_keys=True, default=str))
            else:
                norm.append(str(p))
        return "|".join(norm)

    def get(self, key: str) -> Optional[Any]:
        if self.ttl <= 0:
            return None
        with self._lock:
            entry = self._store.get(key)
            if not entry:
                self._misses += 1
                return None
            expires_at, value = entry
            if expires_at < time.time():
                self._store.pop(key, None)
                self._misses += 1
                return None
            self._hits += 1
            return value

    def set(self, key: str, value: Any) -> None:
        if self.ttl <= 0:
            return
        with self._lock:
            if len(self._store) >= self.maxsize:
                # Evict oldest expiry (cheap O(n); n is small).
                oldest = min(self._store.items(), key=lambda kv: kv[1][0])[0]
                self._store.pop(oldest, None)
            self._store[key] = (time.time() + self.ttl, value)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._hits = 0
            self._misses = 0

    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            hit_rate = (self._hits / total) if total else 0.0
            return {
                "size": len(self._store),
                "maxsize": self.maxsize,
                "ttl": self.ttl,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(hit_rate, 3),
            }


# Module-level singleton shared across TerapeakClient instances.
_response_cache = TTLCache()


def get_response_cache() -> TTLCache:
    return _response_cache
