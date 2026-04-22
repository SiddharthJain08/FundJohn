"""
handoff.py — Redis + filesystem JSON handoff layer for the FundJohn pipeline.

Keys:
  handoff:{date}:structured — Phase 2 canonical input for TradeJohn, written
                              by trade_handoff_builder.py (features + regime
                              + portfolio + veto history + mastermind rec).
  handoff:{date}:sized      — sized orders written by trade_agent_llm.py,
                              consumed by alpaca_executor.py and send_report.py.
  handoff:{date}:memos      — LEGACY (removed with post_memos.py in Phase 2).
  handoff:{date}:research   — LEGACY (removed with research_report.py in Phase 2).

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
