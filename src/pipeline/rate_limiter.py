"""
Rate Limiter — async token-bucket implementation for all outgoing API calls.

R4 implementation — FundJohn Pipeline Audit 2026-04-13

Reads limits from preferences.json and enforces them on every outgoing
HTTP request in both the Python ingestion layer and any async caller.

Usage
-----
    limiter = get_rate_limiter()

    # Before any API call:
    await limiter.acquire("polygon")
    data = await http_client.get(polygon_url)

    # Or as a context manager:
    async with limiter.limited("fmp"):
        data = await http_client.get(fmp_url)
"""
import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Dict, Optional

logger = logging.getLogger(__name__)

PREFS_PATH = os.path.join(
    os.path.dirname(__file__),
    "../../workspaces/default/.agents/user/preferences.json",
)


class TokenBucket:
    """
    Async token-bucket rate limiter.

    Supports three independent constraint axes:
      - per_minute : replenished at 1/60 tokens per second
      - per_second : replenished at 1 token per second  (SEC EDGAR)
      - per_day    : counted across a rolling 24-hour window
    """

    def __init__(
        self,
        per_minute: Optional[int] = None,
        per_second: Optional[int] = None,
        per_day:    Optional[int] = None,
        source:     str = "unknown",
    ):
        self.source     = source
        self.per_minute = per_minute
        self.per_second = per_second
        self.per_day    = per_day

        # Minute bucket
        self._min_tokens = float(per_minute or 0)
        self._min_last   = time.monotonic()
        self._min_lock   = asyncio.Lock()

        # Second bucket
        self._sec_tokens = float(per_second or 0)
        self._sec_last   = time.monotonic()
        self._sec_lock   = asyncio.Lock()

        # Day counter
        self._day_count  = 0
        self._day_reset  = time.monotonic() + 86400
        self._day_lock   = asyncio.Lock()

        # Stats
        self.total_waits = 0
        self.total_calls = 0

    async def acquire(self) -> None:
        """Block until a request token is available across all limit axes."""
        self.total_calls += 1
        waited = False

        # ── Day cap ──────────────────────────────────────────────────────────
        if self.per_day is not None:
            async with self._day_lock:
                now = time.monotonic()
                if now >= self._day_reset:
                    self._day_count = 0
                    self._day_reset = now + 86400
                if self._day_count >= self.per_day:
                    sleep_for = self._day_reset - now
                    logger.warning(
                        "RateLimiter[%s]: day cap (%d) hit, sleeping %.0fs",
                        self.source, self.per_day, sleep_for
                    )
                    waited = True
                    await asyncio.sleep(sleep_for)
                    self._day_count = 0
                    self._day_reset = time.monotonic() + 86400
                self._day_count += 1

        # ── Per-second bucket ─────────────────────────────────────────────────
        if self.per_second is not None:
            async with self._sec_lock:
                now     = time.monotonic()
                elapsed = now - self._sec_last
                self._sec_tokens = min(
                    float(self.per_second),
                    self._sec_tokens + elapsed * self.per_second
                )
                self._sec_last = now
                if self._sec_tokens < 1.0:
                    sleep_for = (1.0 - self._sec_tokens) / self.per_second
                    waited = True
                    await asyncio.sleep(sleep_for)
                    self._sec_tokens = 0.0
                else:
                    self._sec_tokens -= 1.0

        # ── Per-minute bucket ─────────────────────────────────────────────────
        if self.per_minute is not None:
            async with self._min_lock:
                refill_rate = self.per_minute / 60.0
                now         = time.monotonic()
                elapsed     = now - self._min_last
                self._min_tokens = min(
                    float(self.per_minute),
                    self._min_tokens + elapsed * refill_rate
                )
                self._min_last = now
                if self._min_tokens < 1.0:
                    sleep_for = (1.0 - self._min_tokens) / refill_rate
                    waited = True
                    await asyncio.sleep(sleep_for)
                    self._min_tokens = 0.0
                else:
                    self._min_tokens -= 1.0

        if waited:
            self.total_waits += 1

    def stats(self) -> Dict:
        return {
            "source":       self.source,
            "total_calls":  self.total_calls,
            "total_waits":  self.total_waits,
            "per_minute":   self.per_minute,
            "per_second":   self.per_second,
            "per_day":      self.per_day,
        }


class RateLimitRegistry:
    """
    Loads rate limits from preferences.json and provides per-source limiters.
    All callers should use the module-level get_rate_limiter() singleton.
    """

    def __init__(self, prefs_path: str = PREFS_PATH):
        self._limiters: Dict[str, TokenBucket] = {}
        self._load(prefs_path)

    def _load(self, path: str) -> None:
        try:
            with open(path) as f:
                prefs = json.load(f)
            limits = prefs.get("rate_limits", {})
            for source, cfg in limits.items():
                self._limiters[source] = TokenBucket(
                    per_minute=cfg.get("per_minute"),
                    per_second=cfg.get("per_second"),
                    per_day=cfg.get("per_day"),
                    source=source,
                )
            logger.info("RateLimitRegistry: loaded %d source limiters", len(self._limiters))
        except Exception as exc:
            logger.error("Failed to load rate limits from %s: %s", path, exc)

    async def acquire(self, source: str) -> None:
        """Acquire a token for the given source. Blocks if rate-limited."""
        limiter = self._limiters.get(source)
        if limiter:
            await limiter.acquire()
        else:
            logger.debug("RateLimiter: no limit configured for source '%s'", source)

    @asynccontextmanager
    async def limited(self, source: str):
        """Async context manager — acquire before, yield, no release needed."""
        await self.acquire(source)
        yield

    def get_limiter(self, source: str) -> Optional[TokenBucket]:
        return self._limiters.get(source)

    def all_stats(self) -> Dict[str, Dict]:
        return {src: b.stats() for src, b in self._limiters.items()}


# ── Module-level singleton ────────────────────────────────────────────────────

_registry: Optional[RateLimitRegistry] = None


def get_rate_limiter() -> RateLimitRegistry:
    """Return (or create) the module-level singleton RateLimitRegistry."""
    global _registry
    if _registry is None:
        _registry = RateLimitRegistry()
    return _registry
