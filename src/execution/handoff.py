"""
handoff.py — Redis + filesystem JSON handoff layer for the FundJohn pipeline.

Keys:
  handoff:{date}:structured — Phase 2 canonical input for TradeJohn, written
                              by trade_handoff_builder.py (features + regime
                              + portfolio + veto history + mastermind rec).
  handoff:{date}:sized      — sized orders written by regime_blended_sizer_live,
                              consumed by alpaca_executor.py and send_report.py.
  handoff:{date}:memos      — LEGACY (removed with post_memos.py in Phase 2).
  handoff:{date}:research   — LEGACY (removed with research_report.py in Phase 2).

Filesystem fallback: output/handoffs/{date}_{stage}.json
TTL: 86400 seconds (24h)

Phase 2B (2026-05-18): `write_handoff` *optionally* routes the Redis
side through ``DataHub`` (src/database/datahub.py) when the default-OFF
env gate ``OPENCLAW_DATAHUB_HANDOFF=1`` is set. With the gate OFF
(default), behavior is byte-identical to the pre-Phase-2B code path —
direct ``r.setex`` to ``handoff:{date}:{stage}`` plus the filesystem
fallback. With the gate ON, DataHub becomes the primary writer and the
legacy ``r.setex`` is the fallback. Either way the key path and TTL
are unchanged, so all existing readers via ``read_handoff`` keep
working. DataHub additionally publishes on the same topic when the
gate is ON, so a future subscriber can react to new handoffs without
polling.

This default-OFF discipline matches the precedent set by Phase 2D
(``OPENCLAW_UNIFIED_QUOTES=1``) and the live sizer
(``OPENCLAW_REGIME_BLENDED_LIVE=1``).
"""

import os, json
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent.parent
HANDOFF_DIR = ROOT / 'output' / 'handoffs'
REDIS_TTL   = 86_400

# Default-OFF gate for the Phase 2B DataHub migration. When unset (the
# default), `write_handoff` uses the legacy direct-setex path. When set
# to `1`, DataHub is the primary writer with legacy as fallback.
DATAHUB_HANDOFF_GATE_ENV = 'OPENCLAW_DATAHUB_HANDOFF'


def _redis_client():
    try:
        import redis
        r = redis.Redis.from_url(
            os.environ.get('REDIS_URL', 'redis://localhost:6379'),
            decode_responses=True,
            socket_timeout=2,
        )
        r.ping()
        return r
    except Exception:
        return None


def _datahub():
    """Lazily build a DataHub bound to the same Redis the rest of the
    module uses. Returns ``None`` if DataHub or Redis is unreachable so
    the filesystem fallback path is still exercised.

    Imported inside the function to keep ``handoff.py`` importable from
    code that hasn't installed redis (legacy callers).
    """
    try:
        from database.datahub import DataHub, DataHubError
    except Exception:
        return None
    try:
        return DataHub(producer_id='handoff')
    except DataHubError:
        return None
    except Exception:
        return None


def write_handoff(run_date: str, stage: str, payload: Any) -> bool:
    """
    Serialize payload to JSON and write to:
      1. Redis key handoff:{run_date}:{stage}  (TTL 24h), via DataHub
      2. output/handoffs/{run_date}_{stage}.json  (filesystem fallback)

    Returns True if at least filesystem write succeeded.
    """
    data = json.dumps(payload, default=str)
    key  = f'handoff:{run_date}:{stage}'

    published = False
    # Default-OFF: only consult DataHub when the operator opts in.
    if os.environ.get(DATAHUB_HANDOFF_GATE_ENV) == '1':
        hub = _datahub()
        if hub is not None:
            try:
                published = hub.publish(key, payload, ttl=REDIS_TTL)
            except Exception:
                published = False

    if not published:
        # Legacy path — unchanged. Runs both when the gate is off and
        # as a fallback when DataHub publish returned False or raised.
        r = _redis_client()
        if r:
            try:
                r.setex(key, REDIS_TTL, data)
            except Exception:
                pass

    HANDOFF_DIR.mkdir(parents=True, exist_ok=True)
    fpath = HANDOFF_DIR / f'{run_date}_{stage}.json'
    fpath.write_text(data, encoding='utf-8')
    return True


def read_handoff(run_date: str, stage: str) -> Optional[Any]:
    """Read handoff payload. Tries Redis first, falls back to filesystem."""
    key = f'handoff:{run_date}:{stage}'

    r = _redis_client()
    if r:
        try:
            raw = r.get(key)
            if raw:
                return json.loads(raw)
        except Exception:
            pass

    return read_handoff_file(run_date, stage)


def read_handoff_file(run_date: str, stage: str) -> Optional[Any]:
    """Read handoff payload from filesystem only."""
    fpath = HANDOFF_DIR / f'{run_date}_{stage}.json'
    if fpath.exists():
        return json.loads(fpath.read_text(encoding='utf-8'))
    return None
