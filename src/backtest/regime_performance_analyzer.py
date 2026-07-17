"""Canonical per-regime performance analyzer.

Reads either backtest output OR live `signal_pnl`, partitions by regime,
computes Sharpe/win-rate/trade-count/avg-R-multiple per (strategy, regime),
and proposes `eligible_regimes` for each strategy based on configurable
thresholds.

Used by:
  - Phase 1 manifest backfill (one-shot script over all live strategies)
  - lifecycle.validate_regime_eligibility_present() at candidate→staging
  - comprehensive_review.js Saturday refresh

Spec: docs/archive/superpowers/specs/2026-05-11-regime-blended-position-sizing-design.md
"""
from __future__ import annotations
import math
from typing import Iterable
import pandas as pd

ALL_REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')

def compute_regime_stats(df: pd.DataFrame, strategy_id: str, regime: str) -> dict:
    """Compute trade_count, win_rate, avg_r_multiple, sharpe for one (strategy, regime) pair."""
    sub = df[(df['strategy_id'] == strategy_id) & (df['regime_state'] == regime)]
    n = len(sub)
    if n == 0:
        return {'trade_count': 0, 'win_rate': 0.0, 'avg_r_multiple': 0.0, 'sharpe': 0.0}

    wins = (sub['pnl'] > 0).sum()
    win_rate = wins / n
    avg_r = float(sub['r_multiple'].mean())

    # Sharpe on per-trade R-multiples (annualization factor neutral for ranking).
    rm = sub['r_multiple']
    sharpe = float(rm.mean() / rm.std()) if rm.std() > 0 else 0.0

    return {'trade_count': n, 'win_rate': float(win_rate), 'avg_r_multiple': avg_r, 'sharpe': sharpe}

def propose_eligible_regimes(df: pd.DataFrame, strategy_id: str, thresholds: dict) -> list[str]:
    """Return regime names the strategy qualifies for under given thresholds."""
    eligible = []
    for r in ALL_REGIMES:
        s = compute_regime_stats(df, strategy_id, r)
        if (s['sharpe'] >= thresholds['min_sharpe'] and
                s['trade_count'] >= thresholds['min_trade_count'] and
                s['avg_r_multiple'] > thresholds['min_avg_r']):
            eligible.append(r)
    return eligible

def analyze_dataframe(df: pd.DataFrame, thresholds: dict,
                      strategy_ids: Iterable[str] | None = None) -> dict:
    """Analyze one or more strategies; return {strategy_id: {eligible_regimes, stats}}."""
    if strategy_ids is None:
        strategy_ids = sorted(df['strategy_id'].unique())
    out = {}
    for sid in strategy_ids:
        out[sid] = {
            'eligible_regimes': propose_eligible_regimes(df, sid, thresholds),
            'stats': {r: compute_regime_stats(df, sid, r) for r in ALL_REGIMES},
        }
    return out

def load_thresholds_from_db(uri: str) -> dict:
    """Load current thresholds from regime_eligibility_thresholds table."""
    import psycopg2
    with psycopg2.connect(uri) as conn, conn.cursor() as cur:
        cur.execute('SELECT threshold_name, value FROM regime_eligibility_thresholds')
        return {name: float(val) for name, val in cur.fetchall()}

def load_signal_pnl(uri: str, days: int = 730) -> pd.DataFrame:
    """Load live signal_pnl with regime tag for each closed trade.

    Maps real signal_pnl columns to the analyzer's expected DataFrame shape:
      - pnl ← realized_pnl_pct (signed % return; works for win-rate and Sharpe)
      - r_multiple ← realized_pnl_pct (proxy; same column drives both metrics
        until a true R-multiple column is added in a future migration)
      - signal_date ← execution_signals.signal_date (joined via signal_id)
      - regime_state ← execution_signals.regime_state (defaults UNKNOWN if NULL)
      - closed_at gates: only include trades that have actually closed
    """
    import psycopg2
    sql = """
        SELECT sp.strategy_id,
               es.signal_date::date AS signal_date,
               COALESCE(es.regime_state, 'UNKNOWN') AS regime_state,
               sp.realized_pnl_pct AS pnl,
               sp.realized_pnl_pct AS r_multiple
          FROM signal_pnl sp
          JOIN execution_signals es ON es.id = sp.signal_id
         WHERE sp.closed_at IS NOT NULL
           AND es.signal_date >= CURRENT_DATE - INTERVAL '%s days'
    """ % days
    with psycopg2.connect(uri) as conn:
        return pd.read_sql(sql, conn)


def run_with_resolver(strategy, start, end, resolver, **kwargs):
    """SP-2 Phase A: per-bar resolver opt-in stub.

    Phase A acceptance is "the option exists". This iterates trading days
    and calls resolver.resolve(strategy.id, as_of=bar_date) so callers
    can wire per-bar universes through this engine. Phase B/C will join
    this universe up to the engine's existing logic (which is shaped
    differently across engines — manifest-driven, features-driven, etc.).
    """
    from src.backtest._trading_calendar import trading_days
    results = []
    for bar_date in trading_days(start, end):
        universe = resolver.resolve(strategy.id, as_of=bar_date)
        results.append({"date": bar_date, "universe": universe})
    return results
