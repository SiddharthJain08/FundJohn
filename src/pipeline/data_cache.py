"""
Shared Data Cache — Redis-backed fetch-lock with TTL.

R1 implementation — FundJohn Pipeline Audit 2026-04-13

Both the Python ingestion layer (src/ingestion/pipeline.py) and the JS
pipeline layer (src/pipeline/collector.js) must check this cache BEFORE
making any external API call, eliminating duplicate fetches across layers.

Key format : datacache:{source}:{symbol}:{datatype}:{date_str}
Lock format : lock:datacache:{source}:{symbol}:{datatype}:{date_str}
TTL policy  : mirrors preferences.json staleness_windows
"""
import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, Dict, Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

# TTLs in seconds — must mirror staleness_windows in preferences.json
DEFAULT_TTL: Dict[str, int] = {
    "prices":       24 * 3600,    # prices_hours: 24
    "fundamentals": 30 * 86400,   # statements_days: 30
    "research":      7 * 86400,   # research_days: 7
    "filings":       7 * 86400,   # on_new_filing — 7d conservative
    "news":          1 * 3600,    # 1h (conservative)
    "options":       1 * 3600,    # 1h
    "compute":      24 * 3600,    # compute_hours: 24
}

LOCK_TTL            = 30    # seconds — lock expires if holder crashes
LOCK_RETRY_INTERVAL = 0.25  # seconds between lock-check polls
LOCK_TIMEOUT        = 15    # seconds max wait for a lock


class DataCache:
    """
    Redis-backed shared cache for external API data.

    Usage
    -----
    cache = DataCache()
    await cache.connect()

    data = await cache.get_or_fetch(
        source   = "polygon",
        symbol   = "AAPL",
        datatype = "prices",
        date_str = "2026-04-13",
        fetcher  = lambda: polygon_client.get_ohlcv("AAPL", "2026-04-13"),
    )
    """

    def __init__(self, redis_url: str = "redis://localhost:6379/0"):
        self._redis_url = redis_url
        self._client: Optional[aioredis.Redis] = None

    async def connect(self) -> None:
        self._client = await aioredis.from_url(
            self._redis_url, encoding="utf-8", decode_responses=True
        )
        logger.info("DataCache: connected to Redis at %s", self._redis_url)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    # ── Key helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _data_key(source: str, symbol: str, datatype: str, date_str: str) -> str:
        return f"datacache:{source}:{symbol.upper()}:{datatype}:{date_str}"

    @staticmethod
    def _lock_key(data_key: str) -> str:
        return f"lock:{data_key}"

    # ── Core read / write ────────────────────────────────────────────────────

    async def get(self, source: str, symbol: str,
                  datatype: str, date_str: str) -> Optional[Any]:
        """Return cached data or None on cache miss."""
        key = self._data_key(source, symbol, datatype, date_str)
        raw = await self._client.get(key)
        if raw is None:
            logger.debug("Cache MISS: %s", key)
            return None
        logger.debug("Cache HIT: %s", key)
        return json.loads(raw)

    async def set(self, source: str, symbol: str, datatype: str,
                  date_str: str, data: Any, ttl: Optional[int] = None) -> None:
        """Write data to cache with TTL."""
        key = self._data_key(source, symbol, datatype, date_str)
        effective_ttl = ttl if ttl is not None else DEFAULT_TTL.get(datatype, 3600)
        await self._client.setex(key, effective_ttl, json.dumps(data, default=str))
        logger.debug("Cache SET: %s (ttl=%ds)", key, effective_ttl)

    # ── Fetch-lock pattern ───────────────────────────────────────────────────

    async def get_or_fetch(
        self,
        source:   str,
        symbol:   str,
        datatype: str,
        date_str: str,
        fetcher:  Callable[[], Awaitable[Any]],
        ttl:      Optional[int] = None,
    ) -> Optional[Any]:
        """
        Return fresh cached data if available; otherwise acquire a fetch-lock,
        call fetcher(), cache the result, and return it.

        Concurrent callers for the same key wait for the first fetcher to finish
        rather than all hammering the API simultaneously.
        """
        # Fast path: cache hit
        cached = await self.get(source, symbol, datatype, date_str)
        if cached is not None:
            return cached

        data_key = self._data_key(source, symbol, datatype, date_str)
        lock_key = self._lock_key(data_key)

        # Try to acquire distributed fetch-lock
        lock_acquired = await self._client.set(lock_key, "1", nx=True, ex=LOCK_TTL)

        if lock_acquired:
            try:
                logger.debug("DataCache: fetching %s", data_key)
                data = await fetcher()
                if data is not None:
                    await self.set(source, symbol, datatype, date_str, data, ttl)
                return data
            finally:
                await self._client.delete(lock_key)

        # Another process holds the lock — poll for cache population
        deadline = time.monotonic() + LOCK_TIMEOUT
        while time.monotonic() < deadline:
            await asyncio.sleep(LOCK_RETRY_INTERVAL)
            cached = await self.get(source, symbol, datatype, date_str)
            if cached is not None:
                return cached
            if not await self._client.exists(lock_key):
                break   # lock released without populating — fetch directly

        logger.warning("DataCache: lock wait timeout for %s; fetching directly", data_key)
        return await fetcher()

    # ── Invalidation ─────────────────────────────────────────────────────────

    async def invalidate(self, source: str, symbol: str,
                         datatype: str, date_str: str) -> bool:
        key = self._data_key(source, symbol, datatype, date_str)
        return bool(await self._client.delete(key))

    async def invalidate_symbol(self, symbol: str) -> int:
        """Remove all cache entries for a symbol across all sources/datatypes."""
        pattern = f"datacache:*:{symbol.upper()}:*"
        keys = await self._client.keys(pattern)
        return await self._client.delete(*keys) if keys else 0

    async def invalidate_stale(self) -> int:
        """Redis TTL handles expiry automatically — this is a no-op placeholder."""
        logger.debug("DataCache: stale invalidation handled by Redis TTL")
        return 0

    # ── Health / observability ───────────────────────────────────────────────

    async def health_check(self) -> Dict[str, Any]:
        keys = await self._client.keys("datacache:*")
        by_datatype: Dict[str, int] = {}
        for k in keys:
            parts = k.split(":")
            dt = parts[3] if len(parts) > 3 else "unknown"
            by_datatype[dt] = by_datatype.get(dt, 0) + 1
        return {
            "total_cache_keys": len(keys),
            "by_datatype": by_datatype,
        }


# ── Module-level singleton ────────────────────────────────────────────────────

_cache: Optional[DataCache] = None


async def get_cache(redis_url: str = "redis://localhost:6379/0") -> DataCache:
    """Return (or create) the module-level singleton DataCache."""
    global _cache
    if _cache is None:
        _cache = DataCache(redis_url)
        await _cache.connect()
    return _cache
