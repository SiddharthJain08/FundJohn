"""tests/test_datahub.py

Tests for `src.database.datahub.DataHub` — the Phase 2B pub/sub facade.

These tests use a real local Redis at ``localhost:6379`` because the
host runs Redis 7 with live state for production services (see CLAUDE.md
infrastructure section). To avoid colliding with that live state, every
test key/topic is prefixed with `test_datahub:` and the per-test fixture
deletes only those prefixed keys in teardown. ``FLUSHDB`` is NEVER used.

Run:
    pytest tests/test_datahub.py -v
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import redis  # noqa: E402

from database.datahub import (  # noqa: E402
    DataHub,
    DataHubError,
    MAX_PAYLOAD_BYTES,
)
from database.datahub_topics import (  # noqa: E402
    T_DATAHUB_OVERSIZED,
    T_DATAHUB_RATE_LIMITED,
    validate_topic,
)


TEST_PREFIX = "test_datahub:"


def _redis_alive() -> bool:
    try:
        redis.Redis(socket_timeout=1).ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _redis_alive(),
    reason="local Redis not reachable on localhost:6379",
)


@pytest.fixture()
def hub():
    """Fresh ``DataHub`` bound to a dedicated test prefix."""
    r = redis.Redis(decode_responses=True)
    h = DataHub(client=r, key_prefix=TEST_PREFIX, producer_id="pytest")
    yield h
    # Teardown: delete only the keys we created. NEVER FLUSHDB.
    for pattern in (
        f"{TEST_PREFIX}*",
        f"datahub:dedup:{TEST_PREFIX}*",
        "datahub:rl:pytest:*",
    ):
        for k in r.scan_iter(match=pattern, count=500):
            r.delete(k)


# ── B2.3a topic-key validation ───────────────────────────────────────────


def test_validate_topic_rejects_bad_topics(hub):
    """Bad topics raise; good topics accepted by both facade and validator."""
    # Empty / wrong type
    with pytest.raises(ValueError):
        validate_topic("")
    with pytest.raises(ValueError):
        validate_topic("UPPER:case:bad")
    # Reserved prefix
    with pytest.raises(ValueError):
        validate_topic("_steering:something")
    with pytest.raises(ValueError):
        validate_topic("rate_limit:polygon")
    # Too many segments
    with pytest.raises(ValueError):
        validate_topic("a:b:c:d:e:f")
    # Wildcards forbidden when publishing
    with pytest.raises(ValueError):
        validate_topic("agent:*:tradejohn", allow_wildcards=False)
    # Good
    validate_topic("agent:status:tradejohn")
    validate_topic("pipeline:event:cycle-start")
    validate_topic("agent:*:tradejohn", allow_wildcards=True)
    # Facade refuses to publish to reserved topic
    with pytest.raises(ValueError):
        hub.publish("_steering:foo", {"x": 1})


# ── B2.3b TTL expiry ─────────────────────────────────────────────────────


def test_publish_respects_ttl(hub):
    """When ``ttl`` is set, the topic-state key expires after that many seconds."""
    topic = "agent:status:test-ttl-bot"
    ok = hub.publish(topic, {"status": "alive"}, ttl=2)
    assert ok is True
    # Last-value key exists with TTL > 0 and <= 2
    raw_ttl = hub.client.ttl(hub._state_key(topic))
    assert 0 < raw_ttl <= 2, f"expected TTL in (0, 2], got {raw_ttl}"
    # Wait for expiry; key should be gone.
    time.sleep(2.2)
    assert hub.client.get(hub._state_key(topic)) is None


# ── B2.3c dedup window ───────────────────────────────────────────────────


def test_dedup_window_suppresses_duplicates(hub):
    """Identical payload inside the dedup window is dropped + observability event fires."""
    topic = "agent:status:test-dedup-bot"
    payload = {"status": "alive", "v": 1}
    first = hub.publish(topic, payload, dedup_window=5)
    second = hub.publish(topic, payload, dedup_window=5)
    third = hub.publish(topic, {"status": "alive", "v": 2}, dedup_window=5)
    assert first is True
    assert second is False, "duplicate payload should be suppressed"
    assert third is True, "different payload should pass through"
    # Observability counter
    assert hub.stats["deduped"] >= 1


# ── B2.4 rate-limit window ───────────────────────────────────────────────


def test_rate_limit_fail_soft(hub):
    """Exceeding the per-producer window returns False + emits rate-limited topic."""
    topic = "agent:status:test-rl-bot"
    # Limit: 3 publishes per 60s for this producer
    hub.set_rate_limit(max_publishes=3, window_seconds=60)
    captured = []

    def cb(channel, msg):
        captured.append((channel, msg))

    # Subscribe to the observability topic in a thread (psubscribe).
    listener = hub.subscribe(T_DATAHUB_RATE_LIMITED, cb)
    try:
        results = [hub.publish(topic, {"i": i}) for i in range(5)]
        # First 3 succeed, last 2 fail-soft
        assert results[:3] == [True, True, True]
        assert results[3:] == [False, False]
        # Give the listener a moment to receive
        for _ in range(20):
            if captured:
                break
            time.sleep(0.05)
        assert len(captured) >= 1, "rate-limited observability event not received"
        assert hub.stats["rate_limited"] >= 2
    finally:
        listener.stop()


# ── B2.3d wildcard subscribe ─────────────────────────────────────────────


def test_wildcard_subscribe_receives_matching_topics(hub):
    """psubscribe-style pattern delivers all matching topic publishes."""
    seen = []

    def cb(channel, msg):
        seen.append((channel, json.loads(msg) if isinstance(msg, str) else msg))

    listener = hub.subscribe("agent:status:*", cb)
    try:
        # Pubsub setup takes a tick; settle.
        time.sleep(0.1)
        hub.publish("agent:status:alpha", {"x": 1})
        hub.publish("agent:status:beta", {"x": 2})
        hub.publish("pipeline:event:cycle-start", {"x": 3})  # not matching
        # Wait for delivery
        deadline = time.time() + 2.0
        while time.time() < deadline and len(seen) < 2:
            time.sleep(0.05)
        channels = sorted(c for c, _ in seen)
        assert channels == [
            f"{TEST_PREFIX}agent:status:alpha",
            f"{TEST_PREFIX}agent:status:beta",
        ], f"unexpected channels: {channels}"
    finally:
        listener.stop()


# ── B2.3e payload size cap ───────────────────────────────────────────────


def test_payload_size_cap_fails_soft(hub):
    """Oversized payload returns False, emits T_DATAHUB_OVERSIZED, never publishes."""
    topic = "agent:status:test-big-bot"
    big = {"blob": "x" * (MAX_PAYLOAD_BYTES + 10)}

    seen = []

    def cb(channel, msg):
        seen.append((channel, msg))

    listener = hub.subscribe(T_DATAHUB_OVERSIZED, cb)
    try:
        result = hub.publish(topic, big)
        assert result is False
        # The state key should NOT have been written
        assert hub.client.get(hub._state_key(topic)) is None
        # Observability fired
        for _ in range(20):
            if seen:
                break
            time.sleep(0.05)
        assert len(seen) >= 1, "oversized observability event not received"
        assert hub.stats["oversized"] >= 1
    finally:
        listener.stop()


# ── Backwards-compatibility smoke ────────────────────────────────────────


def test_legacy_redis_set_still_works(hub):
    """Sanity: existing `redis.set` callers continue working alongside DataHub.

    This guards spec hard-constraint #2: existing redis.publish/set callers
    must keep working. We poke the same client DataHub uses with a raw
    `set` to a non-conforming key.
    """
    raw_client = hub.client
    raw_client.set(f"{TEST_PREFIX}legacy:scattered:key", "ok", ex=10)
    assert raw_client.get(f"{TEST_PREFIX}legacy:scattered:key") == "ok"


def test_raises_when_redis_unreachable(monkeypatch):
    """DataHub init raises when client is None and no env override works."""
    with pytest.raises(DataHubError):
        DataHub(client=None, url="redis://127.0.0.1:1/", producer_id="pytest")
