"""src/database/datahub.py — DataHub Phase 2B pub/sub facade.

Single entry point for Redis topic-based pub/sub + last-value state
writes, with TTL discipline, payload dedup window, per-producer
soft-fail rate-limit, and payload-size cap.

Ships ALONGSIDE the existing scattered ``redis.publish`` / ``redis.set``
callers across the codebase (see
``docs/superpowers/specs/datahub-inventory-2026-05-18.md``). Per
Phase 2B's hard constraints, callers are migrated incrementally in
follow-up commits; none are deleted in this phase.

Usage:

    from database.datahub import DataHub
    from database.datahub_topics import T_AGENT_STATUS

    hub = DataHub.default(producer_id="tradejohn")
    hub.publish(
        T_AGENT_STATUS.format(agent_id="tradejohn"),
        {"state": "running"},
        ttl=300,
    )

    # Subscribe (background thread)
    def on_msg(channel: str, payload: str) -> None:
        print(channel, payload)

    listener = hub.subscribe("agent:status:*", on_msg)
    # ... eventually:
    listener.stop()

Hard constraint: DataHub MUST NOT touch keys with reserved prefixes
(``_steering:``, ``rate_limit:``, ``ratelimit:``, ``regime_blended:``).
The validator in ``datahub_topics.validate_topic`` enforces this.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import redis

from .datahub_topics import (
    T_DATAHUB_DEDUPED,
    T_DATAHUB_OVERSIZED,
    T_DATAHUB_RATE_LIMITED,
    validate_topic,
)

logger = logging.getLogger(__name__)

# 64KB cap. Redis itself can hold much larger values; this cap is a
# guardrail against accidental log-blob publishes that would blow up
# subscriber memory and the dashboard SSE buffer.
MAX_PAYLOAD_BYTES = 64 * 1024

# Default dedup window when ``dedup_window`` is omitted = OFF (None).
# Callers opt in explicitly; we do not silently swallow distinct
# publishes.

# Rate limit window default = OFF. Callers opt in via ``set_rate_limit``.

DEFAULT_REDIS_URL = "redis://localhost:6379"

# Topic at which the facade emits its own observability events.
_OBSERVABILITY_TOPICS = (
    T_DATAHUB_DEDUPED,
    T_DATAHUB_OVERSIZED,
    T_DATAHUB_RATE_LIMITED,
)


class DataHubError(RuntimeError):
    """Raised on connection failure or programmer error."""


@dataclass
class _RateLimit:
    max_publishes: int
    window_seconds: int


@dataclass
class _Stats:
    published: int = 0
    deduped: int = 0
    rate_limited: int = 0
    oversized: int = 0
    invalid_topic: int = 0

    def as_dict(self) -> dict:
        return {
            "published": self.published,
            "deduped": self.deduped,
            "rate_limited": self.rate_limited,
            "oversized": self.oversized,
            "invalid_topic": self.invalid_topic,
        }

    def __getitem__(self, key: str) -> int:
        # Allow `hub.stats["published"]` ergonomics for dashboards/tests.
        return self.as_dict()[key]


class _Subscriber:
    """Background-thread wrapper for a Redis psubscribe pattern.

    Returned by ``DataHub.subscribe``. Call ``.stop()`` to close cleanly.
    """

    def __init__(
        self,
        client: redis.Redis,
        pattern: str,
        callback: Callable[[str, str], None],
        key_prefix: str,
    ) -> None:
        self._client = client
        self._pattern = pattern
        self._callback = callback
        self._key_prefix = key_prefix
        self._stop = threading.Event()
        self._pubsub = client.pubsub(ignore_subscribe_messages=True)
        self._pubsub.psubscribe(f"{key_prefix}{pattern}")
        self._thread = threading.Thread(
            target=self._run,
            name=f"datahub-sub-{pattern}",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                msg = self._pubsub.get_message(timeout=0.2)
                if not msg:
                    continue
                if msg.get("type") not in ("pmessage", "message"):
                    continue
                channel = msg.get("channel")
                data = msg.get("data")
                if isinstance(channel, bytes):
                    channel = channel.decode("utf-8")
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
                try:
                    self._callback(channel, data)
                except Exception:
                    logger.exception(
                        "datahub subscriber callback raised on %s", channel
                    )
        finally:
            try:
                self._pubsub.close()
            except Exception:
                pass

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)


class DataHub:
    """Pub/sub facade with topic schema, TTL, dedup, and rate-limit.

    Parameters
    ----------
    client:
        Pre-built ``redis.Redis`` instance. If ``None``, a client is
        constructed from ``url`` (default ``redis://localhost:6379``)
        and a ``PING`` is issued. Failure raises ``DataHubError``.
    key_prefix:
        Prepended to every key/channel the facade writes/publishes.
        Tests use ``test_datahub:`` to isolate from live state.
    producer_id:
        Identifier of the publishing process; used for rate-limit
        accounting + included in observability events.
    """

    def __init__(
        self,
        *,
        client: Optional[redis.Redis] = None,
        url: Optional[str] = None,
        key_prefix: str = "",
        producer_id: str = "unknown",
    ) -> None:
        if client is None:
            url = url or os.environ.get("REDIS_URL", DEFAULT_REDIS_URL)
            try:
                client = redis.Redis.from_url(
                    url, decode_responses=True, socket_timeout=2
                )
                client.ping()
            except Exception as exc:
                raise DataHubError(
                    f"DataHub: cannot reach Redis at {url}: {exc}"
                ) from exc
        self.client = client
        self.key_prefix = key_prefix
        self.producer_id = producer_id
        self.stats = _Stats()
        self._rate_limit: Optional[_RateLimit] = None

    # ── Construction helpers ─────────────────────────────────────────

    @classmethod
    def default(cls, *, producer_id: str = "unknown") -> "DataHub":
        """Build a DataHub bound to the env-configured Redis (or localhost)."""
        return cls(producer_id=producer_id)

    # ── Key namespacing ──────────────────────────────────────────────

    def _state_key(self, topic: str) -> str:
        """Last-value key for a topic (separate from pub/sub channel)."""
        return f"{self.key_prefix}{topic}"

    def _channel(self, topic: str) -> str:
        return f"{self.key_prefix}{topic}"

    def _dedup_key(self, topic: str, payload_hash: str) -> str:
        return f"datahub:dedup:{self.key_prefix}{topic}:{payload_hash}"

    def _rate_key(self, window_id: int) -> str:
        return f"datahub:rl:{self.producer_id}:{window_id}"

    # ── Configuration ────────────────────────────────────────────────

    def set_rate_limit(
        self, *, max_publishes: int, window_seconds: int
    ) -> None:
        """Configure the per-producer soft-fail rate limit (B2.4).

        ``max_publishes`` is a fixed-window cap; once exceeded inside
        ``window_seconds`` further ``publish()`` calls return ``False``
        and emit ``T_DATAHUB_RATE_LIMITED`` for observability. Set
        ``max_publishes=0`` or ``None`` to disable.
        """
        if not max_publishes:
            self._rate_limit = None
            return
        if max_publishes < 1 or window_seconds < 1:
            raise ValueError("max_publishes and window_seconds must be >= 1")
        self._rate_limit = _RateLimit(
            max_publishes=int(max_publishes),
            window_seconds=int(window_seconds),
        )

    # ── publish ──────────────────────────────────────────────────────

    def publish(
        self,
        topic: str,
        payload: Any,
        *,
        ttl: Optional[int] = None,
        dedup_window: Optional[int] = None,
    ) -> bool:
        """Publish a payload to ``topic``.

        Returns
        -------
        bool
            ``True`` on success, ``False`` if the publish was suppressed
            by dedup, rate-limit, or payload-size cap (fail-soft).

        Raises
        ------
        ValueError
            On an invalid topic (bad characters / reserved prefix).
        """
        # 1. Validate topic (raises on bad input — programmer error, not soft-fail).
        validate_topic(topic, allow_wildcards=False)

        # 2. Serialize payload as JSON.
        try:
            data = json.dumps(payload, default=str, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"DataHub: payload not JSON-serializable: {exc}") from exc

        size = len(data.encode("utf-8"))
        if size > MAX_PAYLOAD_BYTES:
            self.stats.oversized += 1
            self._emit_observability(
                T_DATAHUB_OVERSIZED,
                {
                    "topic": topic,
                    "size_bytes": size,
                    "cap_bytes": MAX_PAYLOAD_BYTES,
                    "producer": self.producer_id,
                },
            )
            return False

        # 3. Rate-limit check (B2.4 — per-producer fixed-window soft-fail).
        if self._rate_limit is not None:
            window_id = int(time.time() // self._rate_limit.window_seconds)
            key = self._rate_key(window_id)
            count = self.client.incr(key)
            if count == 1:
                # First hit in this window — set TTL so the counter expires
                # cleanly. window+1 to absorb clock drift on the boundary.
                self.client.expire(key, self._rate_limit.window_seconds + 1)
            if count > self._rate_limit.max_publishes:
                self.stats.rate_limited += 1
                # Roll the counter back so a tight loop of denied requests
                # doesn't inflate it past the limit indefinitely. The
                # limiter is a circuit-breaker, not a billing meter.
                try:
                    self.client.decr(key)
                except Exception:
                    pass
                self._emit_observability(
                    T_DATAHUB_RATE_LIMITED,
                    {
                        "topic": topic,
                        "producer": self.producer_id,
                        "limit": self._rate_limit.max_publishes,
                        "window_seconds": self._rate_limit.window_seconds,
                    },
                )
                return False

        # 4. Dedup check.
        if dedup_window is not None and dedup_window > 0:
            payload_hash = hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]
            dedup_key = self._dedup_key(topic, payload_hash)
            # NX EX: claim the key only if it doesn't exist; expires after window.
            claimed = self.client.set(
                dedup_key, "1", nx=True, ex=int(dedup_window)
            )
            if not claimed:
                self.stats.deduped += 1
                self._emit_observability(
                    T_DATAHUB_DEDUPED,
                    {
                        "topic": topic,
                        "producer": self.producer_id,
                        "window_seconds": int(dedup_window),
                    },
                )
                return False

        # 5. Write last-value + publish.
        state_key = self._state_key(topic)
        channel = self._channel(topic)
        if ttl is not None and ttl > 0:
            self.client.setex(state_key, int(ttl), data)
        else:
            # No TTL — write as a persistent last-value. Most callers
            # SHOULD set a TTL; we permit None for pub-only consumers
            # who don't want the last-value side effect.
            self.client.set(state_key, data)
        self.client.publish(channel, data)
        self.stats.published += 1
        return True

    # ── subscribe ────────────────────────────────────────────────────

    def subscribe(
        self,
        topic_pattern: str,
        callback: Callable[[str, str], None],
    ) -> _Subscriber:
        """Subscribe to a topic pattern (supports `*` wildcards).

        ``callback`` is invoked from a background daemon thread with
        ``(channel: str, payload: str)``. Returns a handle whose
        ``.stop()`` method shuts the subscriber down cleanly.
        """
        # Validate but allow wildcards.
        validate_topic(topic_pattern, allow_wildcards=True)
        return _Subscriber(
            client=self.client,
            pattern=topic_pattern,
            callback=callback,
            key_prefix=self.key_prefix,
        )

    # ── Observability ────────────────────────────────────────────────

    def _emit_observability(self, topic: str, payload: dict) -> None:
        """Emit a DataHub-internal event. Does NOT recurse through publish()."""
        try:
            channel = self._channel(topic)
            self.client.publish(channel, json.dumps(payload, default=str))
        except Exception:
            logger.warning("datahub: failed to emit observability %s", topic)


__all__ = [
    "DataHub",
    "DataHubError",
    "MAX_PAYLOAD_BYTES",
]
