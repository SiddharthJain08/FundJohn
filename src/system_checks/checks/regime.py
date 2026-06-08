"""Regime checks — gate eligibility, params seeding, freshness."""
from __future__ import annotations

import os
from datetime import datetime, timezone

import psycopg2

from ..registry import check
from ..types import Status


def _pg():
    return psycopg2.connect(os.environ['POSTGRES_URI'])


@check(name='regime_gate_admits_at_least_one', tags=['regime'], requires=['db'])
def _regime_gate_admits_at_least_one():
    """The eligibility gate, called with the *current regime state string*, returns
    True for at least one strategy. Regression for the 2026-05-13 zero-signal cycle
    where every strategy was rejected due to the dict-vs-string bug."""
    import sys
    sys.path.insert(0, '/root/openclaw/src')
    from strategies.regime_gate import is_eligible, ALL_REGIMES

    with _pg() as conn, conn.cursor() as cur:
        cur.execute("SELECT state FROM market_regime ORDER BY updated_at DESC LIMIT 1")
        row = cur.fetchone()
        if not row:
            return Status.FAIL, 'no market_regime rows'
        state = row[0]
        if state not in ALL_REGIMES:
            return Status.FAIL, f'current regime_state {state!r} not in {ALL_REGIMES}'
        cur.execute("SELECT DISTINCT strategy_id FROM strategy_regime_params")
        strategies = [r[0] for r in cur.fetchall()]

    eligible = [s for s in strategies if is_eligible(s, state)]
    if not eligible:
        return Status.FAIL, f'gate rejects ALL {len(strategies)} strategies under regime={state!r}'
    return Status.PASS, f'{len(eligible)}/{len(strategies)} strategies eligible under {state}'


@check(name='regime_params_seeded', tags=['regime'], requires=['db'])
def _regime_params_seeded():
    """strategy_regime_params has N×4 rows where N is the active strategy count."""
    with _pg() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) AS rows,
                   COUNT(DISTINCT strategy_id) AS strategies,
                   COUNT(DISTINCT regime_state) AS regimes
            FROM strategy_regime_params
        """)
        rows, strategies, regimes = cur.fetchone()
    if regimes != 4:
        return Status.FAIL, f'expected 4 regimes, got {regimes}'
    expected = strategies * 4
    if rows != expected:
        return Status.WARN, f'{rows} rows ≠ {strategies}×4={expected}; some (strategy, regime) pairs missing'
    return Status.PASS, f'{strategies} strategies × {regimes} regimes = {rows} rows'


@check(name='regime_freshness_db', tags=['regime'], requires=['db'])
def _regime_freshness_db():
    """Intraday regime producer liveness.

    2026-06-08: the intraday HMM is the sole regime authority and writes
    intraday_regime_states every 5 min during RTH (9:00-19:55 ET Mon-Fri,
    including carry-forward ticks). market_regime now only gets a row on a
    confirmed transition, so its row-age is no longer a freshness signal —
    this check measures the intraday producer instead. Thresholds sit above
    the max expected gap (a weekend, Fri 19:55 ET -> Mon 09:00 ET ~= 61h) and
    below the engine's 80h stale-gate: WARN >66h, FAIL >72h."""
    with _pg() as conn, conn.cursor() as cur:
        cur.execute("SELECT state, ts_utc FROM intraday_regime_states ORDER BY ts_utc DESC LIMIT 1")
        row = cur.fetchone()
        if not row:
            return Status.FAIL, 'no intraday_regime_states rows'
        state, ts_utc = row
    if ts_utc.tzinfo is None:
        ts_utc = ts_utc.replace(tzinfo=timezone.utc)
    age_h = (datetime.now(timezone.utc) - ts_utc).total_seconds() / 3600
    if age_h > 72:
        return Status.FAIL, f'intraday regime producer stalled — last tick {age_h:.1f}h ago ({state})'
    if age_h > 66:
        return Status.WARN, f'intraday regime producer last tick {age_h:.1f}h ago ({state})'
    return Status.PASS, f'{state}, last intraday tick {age_h:.1f}h ago'
