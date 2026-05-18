"""tests/test_handoff_datahub_migration.py

Smoke test for the B2.5 demonstrator migration: `write_handoff` now
routes through `DataHub.publish` while preserving the existing key
schema (`handoff:{date}:{stage}`) and TTL (86400s) that all existing
readers (read_handoff, alpaca_executor, send_report) depend on.

Run:
    pytest tests/test_handoff_datahub_migration.py -v
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import redis  # noqa: E402

from execution.handoff import (  # noqa: E402
    DATAHUB_HANDOFF_GATE_ENV,
    REDIS_TTL,
    read_handoff,
    write_handoff,
)


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


# Use a date string outside any plausible live run window so we never
# stomp the real `handoff:<today>:structured` key the daily cycle uses.
_TEST_DATE = "1999-12-31"
_TEST_STAGE = "datahub-migration-smoke"
_TEST_KEY = f"handoff:{_TEST_DATE}:{_TEST_STAGE}"


@pytest.fixture()
def _cleanup_handoff():
    r = redis.Redis(decode_responses=True)
    r.delete(_TEST_KEY)
    fpath = ROOT / "output" / "handoffs" / f"{_TEST_DATE}_{_TEST_STAGE}.json"
    fpath.unlink(missing_ok=True)
    yield
    r.delete(_TEST_KEY)
    fpath.unlink(missing_ok=True)


@pytest.fixture()
def _gate_on(monkeypatch):
    """Flip the DataHub gate ON for tests that exercise the migrated path."""
    monkeypatch.setenv(DATAHUB_HANDOFF_GATE_ENV, "1")


def test_write_handoff_via_datahub_writes_canonical_key(_cleanup_handoff, _gate_on):
    """write_handoff places JSON at handoff:{date}:{stage} with TTL 24h."""
    payload = {"orders": [], "marker": "datahub-smoke"}
    assert write_handoff(_TEST_DATE, _TEST_STAGE, payload) is True

    r = redis.Redis(decode_responses=True)
    raw = r.get(_TEST_KEY)
    assert raw is not None, "DataHub did not write the canonical key"
    assert json.loads(raw) == payload
    ttl = r.ttl(_TEST_KEY)
    assert 0 < ttl <= REDIS_TTL, f"expected TTL in (0, {REDIS_TTL}], got {ttl}"


def test_read_handoff_round_trips_after_datahub_write(_cleanup_handoff, _gate_on):
    """read_handoff (raw GET path) sees what write_handoff (DataHub) wrote."""
    payload = {"answer": 42, "regime": "LOW_VOL"}
    assert write_handoff(_TEST_DATE, _TEST_STAGE, payload) is True
    got = read_handoff(_TEST_DATE, _TEST_STAGE)
    assert got == payload


def test_write_handoff_also_publishes(_cleanup_handoff, _gate_on):
    """DataHub fan-out: a subscriber on handoff:* sees the migration publish."""
    seen = []

    def cb(channel, msg):
        seen.append((channel, msg))

    # Subscribe via raw psubscribe so we don't depend on DataHub.subscribe
    # being correct; the migration only depends on DataHub.publish.
    r = redis.Redis(decode_responses=True)
    ps = r.pubsub(ignore_subscribe_messages=True)
    ps.psubscribe(f"{_TEST_KEY}")
    # Drain subscribe ack before publishing.
    time.sleep(0.1)
    try:
        write_handoff(_TEST_DATE, _TEST_STAGE, {"hello": "world"})
        # Pull messages for up to 1s.
        deadline = time.time() + 1.0
        while time.time() < deadline:
            msg = ps.get_message(timeout=0.1)
            if msg and msg.get("type") in ("pmessage", "message"):
                seen.append((msg.get("channel"), msg.get("data")))
                break
        assert len(seen) >= 1, "expected DataHub to publish on the handoff topic"
    finally:
        ps.close()


def test_gate_off_does_not_publish(_cleanup_handoff, monkeypatch):
    """With the gate OFF (default), DataHub.publish is NEVER called.

    Verified two ways: (1) `_datahub()` is monkey-patched to a sentinel
    that records calls; (2) the legacy `r.setex` path still wrote the
    canonical key (so backwards compat holds).
    """
    monkeypatch.delenv(DATAHUB_HANDOFF_GATE_ENV, raising=False)
    from execution import handoff as ho

    calls = []

    def _spy():
        calls.append(("_datahub", time.time()))
        return None  # simulate hub unavailable

    monkeypatch.setattr(ho, "_datahub", _spy)
    payload = {"gate": "off", "expect": "legacy-path"}
    assert write_handoff(_TEST_DATE, _TEST_STAGE, payload) is True
    assert calls == [], (
        f"_datahub() must not be invoked when gate is OFF; got {calls}"
    )
    # Legacy path still wrote the canonical key.
    r = redis.Redis(decode_responses=True)
    raw = r.get(_TEST_KEY)
    assert raw is not None and json.loads(raw) == payload
