"""
handoff.py — Redis + filesystem JSON handoff layer for the FundJohn pipeline.

Keys:
  handoff:{date}:memos    — compact memo digest from post_memos.py
  handoff:{date}:research — compact research payload from research_report.py
  handoff:{date}:sized    — sized signals from trade_agent.py

Filesystem fallback: output/handoffs/{date}_{stage}.json
TTL: 86400 seconds (24h)
"""

import os, json
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent.parent
HANDOFF_DIR = ROOT / 'output' / 'handoffs'
REDIS_TTL   = 86_400


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


def write_handoff(run_date: str, stage: str, payload: Any) -> bool:
    """
    Serialize payload to JSON and write to:
      1. Redis key handoff:{run_date}:{stage}  (TTL 24h)
      2. output/handoffs/{run_date}_{stage}.json  (filesystem fallback)

    Returns True if at least filesystem write succeeded.
    """
    data = json.dumps(payload, default=str)
    key  = f'handoff:{run_date}:{stage}'

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
