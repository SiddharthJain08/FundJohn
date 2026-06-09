"""src/execution/intraday_prefetch.py — coordination for the tick-1 price
prefetch and the tick-3 data-ready gate (OPENCLAW_INTRADAY_15MIN_PREFETCH).

State lives in two Redis keys:
  intraday:prefetch:{date}   — JSON sentinel for the prices-only refetch
  intraday:redeploy:inflight — single-in-flight lock (replaces the old cooldown)
"""
import json
import os

PREFETCH_KEY = 'intraday:prefetch:{date}'
INFLIGHT_KEY = 'intraday:redeploy:inflight'
PREFETCH_TTL_S = 6 * 3600        # sentinel lives the trading day
# Sized to cover redeploy_pipeline GATE_TIMEOUT_S (1200s) + a TYPICAL
# orchestrator runtime (~3 min) so the lock doesn't expire mid-redeploy under
# normal conditions. The real guarantee is the explicit release in
# redeploy_pipeline.main()'s `finally`; this TTL is only a crash backstop.
# NOTE: gate + orchestrator are sequential, so a pathologically-hung
# orchestrator (full ORCHESTRATOR_TIMEOUT_S=1800s) gives a worst-case
# lock-held lifetime of 1200+1800=3000s > 1800s — i.e. on a HUNG orchestrator
# the backstop can still expire early and let a second redeploy overlap.
INFLIGHT_TTL_S = 1800            # 30 min crash backstop (see note above)


def prefetch_enabled() -> bool:
    return os.environ.get('OPENCLAW_INTRADAY_15MIN_PREFETCH') == '1'


def _k(date: str) -> str:
    return PREFETCH_KEY.format(date=date)


def read_prefetch(r, date: str) -> dict | None:
    if r is None:
        return None
    raw = r.get(_k(date))
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        return json.loads(raw)
    except Exception:
        return None


def _write(r, date: str, payload: dict) -> None:
    if r is None:
        return
    r.set(_k(date), json.dumps(payload), ex=PREFETCH_TTL_S)


def set_prefetch_running(r, date: str, *, target_state: str, episode: str,
                         started_at: str | None = None) -> None:
    _write(r, date, {
        'status': 'running', 'target_state': target_state,
        'episode': episode, 'started_at': started_at,
    })


def set_prefetch_done(r, date: str, *, n_tickers: int,
                      finished_at: str | None = None) -> None:
    cur = read_prefetch(r, date) or {}
    cur.update({'status': 'done', 'n_tickers': n_tickers,
                'finished_at': finished_at})
    _write(r, date, cur)


def set_prefetch_failed(r, date: str, *, error: str,
                        finished_at: str | None = None) -> None:
    cur = read_prefetch(r, date) or {}
    cur.update({'status': 'failed', 'error': str(error)[:300],
                'finished_at': finished_at})
    _write(r, date, cur)


def should_prefetch(r, date: str, *, episode: str) -> bool:
    """Debounce: prefetch only if there is no sentinel for THIS episode that
    is already running or done. A different episode (new candidate) re-fires."""
    s = read_prefetch(r, date)
    if not s:
        return True
    if s.get('episode') != episode:
        return True
    return s.get('status') not in ('running', 'done')


def acquire_inflight(r) -> bool:
    """True if we took the lock; False if a redeploy is already in flight."""
    if r is None:
        return True   # no redis → can't coordinate; let the spawn proceed
    return bool(r.set(INFLIGHT_KEY, '1', nx=True, ex=INFLIGHT_TTL_S))


def release_inflight(r) -> None:
    if r is not None:
        try:
            r.delete(INFLIGHT_KEY)
        except Exception:
            pass
