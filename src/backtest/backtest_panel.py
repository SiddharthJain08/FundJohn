"""Precompute the per-strategy BACKTEST dashboard panel:
effective Sharpe, GBM-σ OUE counts (overall + per regime), and a
weekly-downsampled equity curve vs SP500 with per-point regime.

This is the dashboard's backtest panel — SEPARATE from the live
#trade-reports OUE digest. Persisted to strategy_backtest_panel.
"""
from __future__ import annotations
import json
import math
import os
import sys
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

from execution.oue_classifier import classify          # noqa: E402
from strategies import historical_regimes               # noqa: E402

PRICES_PARQUET = ROOT / 'data' / 'master' / 'prices.parquet'


def classify_trades_oue(trades: list[dict],
                        hv21_for: Callable[[str, str], Optional[float]],
                        sigma_gate: float = 2.0) -> tuple[dict, dict]:
    """Classify each backtest trade Over/Under/Expected vs a zero-drift GBM
    expectation, reusing oue_classifier.classify (ev_gbm=0). Returns
    (overall_counts, by_regime_counts). Trades with no computable hv21 →
    'expected' (mirrors the live classifier's missing-EV fallback), so
    O+U+E == len(trades) always holds."""
    overall = {'over': 0, 'under': 0, 'expected': 0}
    by_regime: dict[str, dict] = {}
    for t in trades:
        regime = t.get('entry_regime') or 'UNKNOWN'
        slot = by_regime.setdefault(regime, {'over': 0, 'under': 0, 'expected': 0})
        hv = hv21_for(t['ticker'], str(t['entry_date']))
        if hv is None or not math.isfinite(hv) or hv <= 0:
            kind = 'expected'
        else:
            kind, _ = classify(float(t['pnl_pct']), int(t.get('holding_days') or 1),
                               ev_gbm=0.0, hv21=float(hv), sigma_gate=sigma_gate)
        overall[kind] += 1
        slot[kind] += 1
    return overall, by_regime


from backtest.unified_backtest import _portfolio_daily_returns   # noqa: E402


def effective_sharpe(total_sharpe: Optional[float], cadence_days: Optional[float]) -> Optional[float]:
    """Sharpe / sqrt(cadence). cadence floored at 1 day."""
    if total_sharpe is None:
        return None
    return float(total_sharpe) / math.sqrt(max(1.0, float(cadence_days or 1.0)))


def build_equity_curve(trades: list[dict],
                       bench_daily_ret: pd.Series,
                       regime_series_fn=historical_regimes.regime_series,
                       weekly: bool = True) -> list[dict]:
    """Reconstruct the strategy's equity curve from backtest trades (reusing
    unified_backtest._portfolio_daily_returns), overlay the benchmark
    (^GSPC) normalized to the same start (1.0), tag each point with its
    regime, and downsample to weekly. Returns [{date, strat_equity,
    spx_equity, regime}, ...] (ascending date)."""
    daily_ret, dates = _portfolio_daily_returns(trades)
    if len(daily_ret) == 0:
        return []
    idx = pd.DatetimeIndex(dates)
    strat_eq = pd.Series(np.cumprod(1.0 + daily_ret), index=idx)
    b = bench_daily_ret.reindex(pd.date_range(idx.min(), idx.max(), freq='D')).fillna(0.0)
    bench_eq_full = (1.0 + b).cumprod()
    bench_eq = bench_eq_full.reindex(idx, method='ffill')
    regimes = regime_series_fn(idx)
    regimes.index = idx
    frame = pd.DataFrame({'strat_equity': strat_eq,
                          'spx_equity': bench_eq.values,
                          'regime': regimes.values}, index=idx)
    if weekly:
        frame = frame.groupby(frame.index.to_period('W')).tail(1)
    # Normalize both equity series to 1.0 at the first sampled point.
    first_strat = float(frame['strat_equity'].iloc[0])
    first_bench = float(frame['spx_equity'].iloc[0])
    frame = frame.copy()
    frame['strat_equity'] = frame['strat_equity'] / first_strat
    frame['spx_equity'] = frame['spx_equity'] / first_bench
    out = []
    for ts, row in frame.iterrows():
        # Non-finite equity (NaN/inf) is possible when a strategy's normalized
        # base is 0 (a −100% equity point, more reachable under conservative
        # t+1 fills). Bare NaN/Infinity is INVALID JSON and aborts the panel
        # INSERT, so emit None (renders as a gap on the dashboard line).
        se = float(row['strat_equity']); sx = float(row['spx_equity'])
        out.append({'date': ts.strftime('%Y-%m-%d'),
                    'strat_equity': round(se, 6) if np.isfinite(se) else None,
                    'spx_equity': round(sx, 6) if np.isfinite(sx) else None,
                    'regime': None if pd.isna(row['regime']) else str(row['regime'])})
    return out


TRADING_DAYS = 252


def build_hv21_lookup(prices: pd.DataFrame):
    """From a long prices frame [ticker, date, close], build a per-ticker
    21-day annualized realized-vol series and return hv21(ticker, date) →
    Optional[float] (asof nearest prior date)."""
    prices = prices[['ticker', 'date', 'close']].dropna()
    prices = prices.assign(date=pd.to_datetime(prices['date']))
    hv_by_ticker: dict[str, pd.Series] = {}
    for tkr, g in prices.sort_values('date').groupby('ticker'):
        s = g.set_index('date')['close'].astype(float)
        logret = np.log(s).diff()
        hv = logret.rolling(21).std() * math.sqrt(TRADING_DAYS)
        hv_by_ticker[tkr] = hv.dropna()

    def lookup(ticker: str, entry_date: str) -> Optional[float]:
        s = hv_by_ticker.get(ticker)
        if s is None or s.empty:
            return None
        ts = pd.Timestamp(entry_date)
        prior = s.loc[:ts]
        if prior.empty:
            return None
        v = float(prior.iloc[-1])
        return v if math.isfinite(v) and v > 0 else None
    return lookup


def _sigma_gate(cur) -> float:
    try:
        cur.execute("SELECT value FROM pipeline_config WHERE key='sigma_gate'")
        r = cur.fetchone()
        return float(r[0]) if r else 2.0
    except Exception:
        return 2.0


def _benchmark_daily_returns(prices: pd.DataFrame, ticker: str = '^GSPC') -> pd.Series:
    g = prices[prices['ticker'] == ticker][['date', 'close']].dropna()
    g = g.assign(date=pd.to_datetime(g['date'])).sort_values('date').set_index('date')
    return g['close'].astype(float).pct_change().fillna(0.0)


def build_panel(strategy_id: str, conn, prices: pd.DataFrame,
                hv21_for, bench_ret: pd.Series) -> Optional[dict]:
    """Compute the panel dict for one strategy from its primary_window run.
    Returns None if the strategy has no primary_window backtest trades."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT run_id, total_sharpe, avg_holding_days
              FROM strategy_backtest_runs
             WHERE strategy_id=%s AND primary_window=TRUE
             ORDER BY run_at DESC LIMIT 1
        """, (strategy_id,))
        run = cur.fetchone()
        if not run:
            return None
        cur.execute("""
            SELECT ticker, entry_date, pnl_pct, holding_days, entry_regime
              FROM strategy_backtest_trades WHERE run_id=%s ORDER BY exit_date
        """, (run['run_id'],))
        trades = [dict(r) for r in cur.fetchall()]
        gate = _sigma_gate(cur)
    if not trades:
        return None
    overall, by_regime = classify_trades_oue(trades, hv21_for, sigma_gate=gate)
    curve = build_equity_curve(trades, bench_ret, weekly=True)
    eff = effective_sharpe(run['total_sharpe'], run['avg_holding_days'])
    return {
        'strategy_id': strategy_id,
        'run_id': run['run_id'],
        'effective_sharpe': eff,
        'cadence_days': float(run['avg_holding_days'] or 1.0),
        'oue_over': overall['over'], 'oue_under': overall['under'],
        'oue_expected': overall['expected'], 'oue_by_regime': by_regime,
        'oue_sigma_gate': gate, 'equity_curve': curve, 'n_trades': len(trades),
    }


def persist_panel(conn, panel: dict) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO strategy_backtest_panel
              (strategy_id, run_id, effective_sharpe, cadence_days,
               oue_over, oue_under, oue_expected, oue_by_regime,
               oue_sigma_gate, equity_curve, n_trades, computed_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW())
            ON CONFLICT (strategy_id) DO UPDATE SET
               run_id=EXCLUDED.run_id, effective_sharpe=EXCLUDED.effective_sharpe,
               cadence_days=EXCLUDED.cadence_days, oue_over=EXCLUDED.oue_over,
               oue_under=EXCLUDED.oue_under, oue_expected=EXCLUDED.oue_expected,
               oue_by_regime=EXCLUDED.oue_by_regime, oue_sigma_gate=EXCLUDED.oue_sigma_gate,
               equity_curve=EXCLUDED.equity_curve, n_trades=EXCLUDED.n_trades,
               computed_at=NOW()
        """, (panel['strategy_id'], panel['run_id'], panel['effective_sharpe'],
              panel['cadence_days'], panel['oue_over'], panel['oue_under'],
              panel['oue_expected'], json.dumps(panel['oue_by_regime']),
              panel['oue_sigma_gate'], json.dumps(panel['equity_curve']),
              panel['n_trades']))
    conn.commit()


def rebuild(strategy_id: Optional[str] = None) -> dict:
    """Build + persist panels. If strategy_id is None, rebuild all strategies
    that have a primary_window run."""
    prices = pd.read_parquet(PRICES_PARQUET, columns=['ticker', 'date', 'close'])
    hv21_for = build_hv21_lookup(prices)
    bench_ret = _benchmark_daily_returns(prices, '^GSPC')
    conn = psycopg2.connect(os.environ['POSTGRES_URI'])
    try:
        if strategy_id:
            sids = [strategy_id]
        else:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT strategy_id FROM strategy_backtest_runs WHERE primary_window=TRUE")
                sids = [r[0] for r in cur.fetchall()]
        stats = {'built': 0, 'skipped': 0, 'failed': 0}
        for sid in sids:
            # Per-strategy isolation: one bad panel (e.g. a JSON/NaN error or a
            # missing-data strategy) must not abort the whole rebuild and leave
            # every later strategy's panel stale. Roll back just this sid and
            # continue; the failure is counted + logged.
            try:
                panel = build_panel(sid, conn, prices, hv21_for, bench_ret)
                if panel is None:
                    stats['skipped'] += 1
                    continue
                persist_panel(conn, panel)
                stats['built'] += 1
            except Exception as e:
                conn.rollback()
                stats['failed'] += 1
                print(f"[backtest_panel] FAILED {sid}: {type(e).__name__}: {e}")
        return stats
    finally:
        conn.close()


if __name__ == '__main__':
    import argparse
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument('--rebuild', action='store_true')
    ap.add_argument('--strategy-id', default=None)
    a = ap.parse_args()
    if a.rebuild:
        print(rebuild(a.strategy_id))
    else:
        ap.print_help()
