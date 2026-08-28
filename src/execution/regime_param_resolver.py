"""DB-backed reader for per-(strategy, regime) eligibility + tunable params.

Caches (strategy_id, regime_state) → row with a 30s TTL plus explicit
invalidate(). All writers (dashboard, CLI, future Mastermind) MUST call
invalidate() after committing a write so the same-process consumer sees
the new value within milliseconds; cross-process freshness is bounded by
the TTL.

Backward-compat semantics:
- Row missing (strategy not yet migrated, or never seeded) → is_eligible
  returns True; size_scalar returns the Phase 1 regime-only static default.
- size_scalar/stop_pct/target_pct/max_hold_days are NULL by default — the
  *_override getters return None and callers fall back to existing logic.

Spec: docs/archive/superpowers/specs/2026-05-12-regime-blended-sizer-phase-2a-design.md
"""
from __future__ import annotations

import math
import os
import threading
import time
from typing import Optional

# Phase 1 regime-only static scalars (canonical source: trade_handoff_builder.py
# line 156). Duplicated here for backward-compat fallback only. The dict in
# trade_handoff_builder gets replaced by a call to size_scalar() in Task 5.
PHASE1_REGIME_SCALARS: dict[str, float] = {
    'LOW_VOL':       1.0,
    'TRANSITIONING': 0.55,
    'HIGH_VOL':      0.35,
    'CRISIS':        0.15,
}

CACHE_TTL_SECONDS = 30.0
_COLUMNS = ('strategy_id', 'regime_state', 'eligible',
            'size_scalar', 'stop_pct', 'target_pct', 'max_hold_days')


class _Cache:
    def __init__(self):
        self._d: dict[tuple, tuple] = {}   # (sid, regime) -> (row_or_None, expires_at)
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            entry = self._d.get(key)
            if entry is None:
                return _MISS
            row, expires_at = entry
            if time.monotonic() >= expires_at:
                self._d.pop(key, None)
                return _MISS
            return row

    def set(self, key, row):
        with self._lock:
            self._d[key] = (row, time.monotonic() + CACHE_TTL_SECONDS)

    def invalidate(self, key):
        with self._lock:
            self._d.pop(key, None)

    def clear(self):
        with self._lock:
            self._d.clear()


_MISS = object()
_cache = _Cache()


def _db_uri() -> str:
    return (os.environ.get('DATABASE_URL')
            or os.environ.get('POSTGRES_URI')
            or 'postgresql://openclaw:password@localhost:5432/openclaw')


def _connect():
    import psycopg2
    return psycopg2.connect(_db_uri())


def _fetch_row(strategy_id: str, regime_state: str) -> Optional[dict]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT strategy_id, regime_state, eligible,
                       size_scalar, stop_pct, target_pct, max_hold_days
                  FROM strategy_regime_params
                 WHERE strategy_id = %s AND regime_state = %s
            """, (strategy_id, regime_state))
            row = cur.fetchone()
    if row is None:
        return None
    return dict(zip(_COLUMNS, row))


def get_row(strategy_id: str, regime_state: str) -> Optional[dict]:
    """Cached read of a single row. None when no row exists."""
    key = (strategy_id, regime_state)
    cached = _cache.get(key)
    if cached is _MISS:
        row = _fetch_row(strategy_id, regime_state)
        _cache.set(key, row)
        return row
    return cached


def invalidate(strategy_id: str, regime_state: str) -> None:
    _cache.invalidate((strategy_id, regime_state))


# ── Public read API ────────────────────────────────────────────────────────

def is_eligible(strategy_id: str, regime_state: str) -> bool:
    row = get_row(strategy_id, regime_state)
    if row is None:
        return True   # backward-compat: not migrated → eligible everywhere
    return bool(row['eligible'])


def size_scalar(strategy_id: str, regime_state: str) -> float:
    row = get_row(strategy_id, regime_state)
    if row is None or row['size_scalar'] is None:
        return PHASE1_REGIME_SCALARS.get(regime_state,
                                         PHASE1_REGIME_SCALARS['TRANSITIONING'])
    return float(row['size_scalar'])


def stop_pct_override(strategy_id: str, regime_state: str) -> Optional[float]:
    row = get_row(strategy_id, regime_state)
    if row is None or row['stop_pct'] is None:
        return None
    return float(row['stop_pct'])


def target_pct_override(strategy_id: str, regime_state: str) -> Optional[float]:
    row = get_row(strategy_id, regime_state)
    if row is None or row['target_pct'] is None:
        return None
    return float(row['target_pct'])


def max_hold_days_override(strategy_id: str, regime_state: str) -> Optional[int]:
    row = get_row(strategy_id, regime_state)
    if row is None or row['max_hold_days'] is None:
        return None
    return int(row['max_hold_days'])


def configured_max_hold_days(strategy_id: str, *, default: int = 21, log=None) -> int:
    """Strategy-configured hold horizon shared by the backtest engine
    (unified_backtest._configured_max_hold_days) and the live exit-hook time
    stop (engine.update_pnl). MAX of the non-null per-regime
    strategy_regime_params.max_hold_days values; `default` when the coupling
    gate (OPENCLAW_BACKTEST_COUPLED_RECS) is off, when nothing is set, or on
    any lookup failure (reported through `log` when given)."""
    from execution import regime_param_override
    from strategies.base import CANONICAL_REGIMES
    if not regime_param_override.gate_on():
        return int(default)
    try:
        vals = [max_hold_days_override(strategy_id, r) for r in CANONICAL_REGIMES]
        vals = [int(v) for v in vals if v]
        return max(vals) if vals else int(default)
    except Exception as e:  # lookup plumbing only; never fail the caller
        if log is not None:
            log(f'{strategy_id}: configured max_hold lookup failed '
                f'({type(e).__name__}: {e}); using default {default}')
        return int(default)


def _fetch_size_scalars(regime_state: str) -> list:
    """Raw (strategy_id, size_scalar) rows for a regime where size_scalar is set."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT strategy_id, size_scalar
                  FROM strategy_regime_params
                 WHERE regime_state = %s AND size_scalar IS NOT NULL
            """, (regime_state,))
            return cur.fetchall()


def size_scalars_for_regime(regime_state: str, _fetch=None) -> dict:
    """Batch-load RAW per-strategy size_scalar for a regime → {strategy_id: float}.

    Distinct from size_scalar(): NO PHASE1_REGIME_SCALARS fallback. A strategy
    absent here is treated by the caller as 1.0 (no change). Only finite,
    non-negative values are kept; nan/inf/negative are dropped (caller → 1.0).
    An explicit 0.0 is kept (deliberate mute). Not cached — called once/cycle.
    """
    fetch = _fetch or _fetch_size_scalars
    out: dict = {}
    for sid, val in fetch(regime_state):
        try:
            v = float(val)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v) and v >= 0.0:
            out[sid] = v
    return out
