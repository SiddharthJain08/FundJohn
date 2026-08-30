"""Precompute the per-strategy BACKTEST dashboard panel:
effective Sharpe, GBM-σ OUE counts (overall + per regime), and a
weekly-downsampled equity curve vs buy-and-hold SPY (dividend-adjusted
total return, the same series the sizer's S_m is computed from) with
per-point regime.

2026-08-30: the curve is built from TRUE daily marks along each trade's
close path (scaled so the lot compounds to its persisted pnl_pct — fills and
cost land on the exit day). The legacy path smeared pnl over `holding_days`
(a TRADING-day count) as CALENDAR days, compressing every lot into ~70 % of
its real span and inflating every strategy's curve (S_beta_spy: 6.3× vs the
4.44× buy-and-hold it actually tracks). Trades whose ticker has no close
path fall back to a smear over the lot's real calendar span.

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
from execution.strategy_weights import CADENCE_WEIGHT_NORM_ENV  # noqa: E402
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
    """Raw total_sharpe by default (cadence normalization retired 2026-08-29,
    spec D2 — see execution.strategy_weights.CADENCE_WEIGHT_NORM_ENV and
    ._regime_weight, the sizer's equivalent). Only under
    OPENCLAW_STRATEGY_CADENCE_WEIGHT_NORM=1 does this divide by sqrt(cadence)
    (cadence floored at 1 day) to restore the legacy formula."""
    if total_sharpe is None:
        return None
    if os.environ.get(CADENCE_WEIGHT_NORM_ENV) == '1':
        return float(total_sharpe) / math.sqrt(max(1.0, float(cadence_days or 1.0)))
    return float(total_sharpe)


def build_close_lookup(prices: pd.DataFrame) -> Callable[[str], Optional[pd.Series]]:
    """closes_for(ticker) -> date-indexed float close series (ascending, one
    row per date) or None. Built lazily per ticker from the long frame and
    cached, like build_hv21_lookup."""
    by_ticker = prices.groupby('ticker', sort=False)
    groups = set(by_ticker.groups.keys())
    cache: dict = {}

    def closes_for(tkr: str) -> Optional[pd.Series]:
        if tkr in cache:
            return cache[tkr]
        out = None
        if tkr in groups:
            g = by_ticker.get_group(tkr)[['date', 'close']].dropna()
            if len(g):
                g = g.assign(date=pd.to_datetime(g['date'])).sort_values('date')
                g = g.drop_duplicates('date', keep='last').set_index('date')
                out = g['close'].astype(float)
        cache[tkr] = out
        return out

    closes_for.cache = cache  # type: ignore[attr-defined]
    return closes_for


def trade_daily_marks(trade: dict, closes: Optional[pd.Series]):
    """[(date, daily_return), …] for one trade along its ticker's close path,
    or None when the path is unusable (no closes, no exit_date, no trading day
    in (entry, exit], or a wiped-out lot pnl_pct <= -1).

    Interior marks are close/prev_close - 1 (sign-flipped for SHORT); the LAST
    mark is rescaled so the lot compounds to exactly 1 + pnl_pct, which puts
    the fill-vs-close and cost residual on the exit day. Path-faithful and
    endpoint-exact by construction."""
    if closes is None or len(closes) == 0 or not trade.get('exit_date'):
        return None
    try:
        pnl = float(trade['pnl_pct'])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(pnl) or pnl <= -1.0:
        return None
    e = pd.Timestamp(trade['entry_date'])
    x = pd.Timestamp(trade['exit_date'])
    if x <= e:
        return None
    path = closes.loc[(closes.index > e) & (closes.index <= x)]
    if len(path) == 0:
        return None
    prev = closes.loc[closes.index <= e]
    if len(prev) == 0:
        return None
    sign = -1.0 if str(trade.get('direction') or 'LONG').upper() == 'SHORT' else 1.0
    seq = pd.concat([prev.iloc[-1:], path])
    rets = (seq.pct_change().iloc[1:] * sign).astype(float)
    if not np.all(np.isfinite(rets.values)):
        return None
    vals = rets.values.copy()
    head = float(np.prod(1.0 + vals[:-1])) if len(vals) > 1 else 1.0
    if head <= 0.0:
        return None
    vals[-1] = (1.0 + pnl) / head - 1.0
    return [(d, float(r)) for d, r in zip(path.index, vals)]


def prepare_trades_for_curve(trades: list[dict], closes_for=None) -> list[dict]:
    """Copy of `trades` ready for _portfolio_daily_returns: `daily_marks`
    attached wherever a close path exists; otherwise `holding_days` is replaced
    by the lot's real CALENDAR span (exit_date - entry_date) so the legacy
    smear lays the pnl over the days the lot actually spanned. Inputs are not
    mutated."""
    out = []
    for t in trades:
        u = dict(t)
        u.pop('daily_marks', None)
        closes = closes_for(u.get('ticker')) if closes_for is not None else None
        marks = trade_daily_marks(u, closes)
        if marks:
            u['daily_marks'] = marks
        elif u.get('exit_date') and u.get('entry_date'):
            span = (pd.Timestamp(u['exit_date']) - pd.Timestamp(u['entry_date'])).days
            if span > 0:
                u['holding_days'] = int(span)
        out.append(u)
    return out


def build_equity_curve(trades: list[dict],
                       bench_daily_ret: pd.Series,
                       regime_series_fn=historical_regimes.regime_series,
                       weekly: bool = True, closes_for=None) -> list[dict]:
    """Reconstruct the strategy's equity curve from backtest trades (reusing
    unified_backtest._portfolio_daily_returns on TRUE close-path marks — see
    prepare_trades_for_curve; `closes_for` None ⇒ calendar-span smear),
    overlay the benchmark (BENCHMARK_TICKER, SPY total return) normalized to
    the same start (1.0), tag each point with its regime, and downsample to
    weekly. Returns [{date, strat_equity, spx_equity, regime}, ...]
    (ascending date; the `spx_equity` key name is kept for the dashboard)."""
    daily_ret, dates = _portfolio_daily_returns(prepare_trades_for_curve(trades, closes_for))
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


BENCHMARK_TICKER = 'SPY'   # dividend-adjusted total return (2026-08-30; was ^GSPC, a price-only index ~20 % lower over 10 y)


def load_prices(tickers=None) -> pd.DataFrame:
    """Long [ticker, date, close] frame from the master prices parquet.

    With `tickers` (any non-empty collection) the read is a pyarrow predicate
    pushdown over just those symbols — a single-strategy rebuild touches the
    strategy's own trade tickers (avg ~340) plus BENCHMARK_TICKER, i.e. a few
    MB. None / empty ⇒ the full panel (the all-strategies weekend rebuild).

    2026-08-30: the bare full read + eager 21-day vol over all 12.5k tickers
    peaked ~3.1 GB and, running in-process after every backtest's commit
    (unified_backtest's panel hook), pushed every candidate backtest past the
    research finisher's 5 GB cgroup cap — four OOM kills on 2026-08-29, all
    AFTER `wrote run_id`. Filtered, the same rebuild peaks ~0.3 GB.
    """
    import pyarrow.parquet as pq
    cols = ['ticker', 'date', 'close']
    wanted = sorted({str(t) for t in (tickers or ()) if t})
    if not wanted:
        return pd.read_parquet(PRICES_PARQUET, columns=cols)
    return pq.read_table(str(PRICES_PARQUET), columns=cols,
                         filters=[('ticker', 'in', wanted)]).to_pandas()


def _trade_tickers(conn, strategy_id: str) -> set[str]:
    """Distinct tickers traded in the strategy's latest primary_window run
    (empty when it has no run — build_panel then returns None as before)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ticker FROM strategy_backtest_trades
             WHERE run_id = (SELECT run_id FROM strategy_backtest_runs
                              WHERE strategy_id=%s AND primary_window=TRUE
                              ORDER BY run_at DESC LIMIT 1)
        """, (strategy_id,))
        return {r[0] for r in cur.fetchall()}


def build_hv21_lookup(prices: pd.DataFrame):
    """From a long prices frame [ticker, date, close], return
    hv21(ticker, date) → Optional[float]: the ticker's 21-day annualized
    realized vol as of the nearest prior date. The per-ticker series is
    computed LAZILY on first lookup and cached (`lookup.cache`), so a
    strategy that touches 300 tickers never pays for the other 12k."""
    prices = prices[['ticker', 'date', 'close']].dropna()
    prices = prices.assign(date=pd.to_datetime(prices['date'])).sort_values(['ticker', 'date'])
    groups = prices.groupby('ticker', sort=False)
    cache: dict[str, Optional[pd.Series]] = {}

    def _series(tkr: str) -> Optional[pd.Series]:
        if tkr not in cache:
            try:
                g = groups.get_group(tkr)
            except KeyError:
                cache[tkr] = None
                return None
            s = g.set_index('date')['close'].astype(float)
            logret = np.log(s).diff()
            cache[tkr] = (logret.rolling(21).std() * math.sqrt(TRADING_DAYS)).dropna()
        return cache[tkr]

    def lookup(ticker: str, entry_date: str) -> Optional[float]:
        s = _series(ticker)
        if s is None or s.empty:
            return None
        ts = pd.Timestamp(entry_date)
        prior = s.loc[:ts]
        if prior.empty:
            return None
        v = float(prior.iloc[-1])
        return v if math.isfinite(v) and v > 0 else None
    lookup.cache = cache
    return lookup


def _sigma_gate(cur) -> float:
    try:
        cur.execute("SELECT value FROM pipeline_config WHERE key='sigma_gate'")
        r = cur.fetchone()
        return float(r[0]) if r else 2.0
    except Exception:
        return 2.0


def _benchmark_daily_returns(prices: pd.DataFrame, ticker: str = BENCHMARK_TICKER) -> pd.Series:
    g = prices[prices['ticker'] == ticker][['date', 'close']].dropna()
    g = g.assign(date=pd.to_datetime(g['date'])).sort_values('date').set_index('date')
    return g['close'].astype(float).pct_change().fillna(0.0)


def build_panel(strategy_id: str, conn, prices: pd.DataFrame,
                hv21_for, bench_ret: pd.Series, closes_for=None) -> Optional[dict]:
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
            SELECT ticker, direction, entry_date, exit_date, pnl_pct, holding_days, entry_regime
              FROM strategy_backtest_trades WHERE run_id=%s ORDER BY exit_date
        """, (run['run_id'],))
        trades = [dict(r) for r in cur.fetchall()]
        gate = _sigma_gate(cur)
    if not trades:
        return None
    overall, by_regime = classify_trades_oue(trades, hv21_for, sigma_gate=gate)
    curve = build_equity_curve(trades, bench_ret, weekly=True,
                               closes_for=closes_for if closes_for is not None else build_close_lookup(prices))
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
    conn = psycopg2.connect(os.environ['POSTGRES_URI'])
    try:
        if strategy_id:
            # Single strategy: read only its trade tickers + the benchmark
            # (see load_prices) — this path runs inside every backtest process.
            prices = load_prices(_trade_tickers(conn, strategy_id) | {BENCHMARK_TICKER})
        else:
            prices = load_prices(None)
        hv21_for = build_hv21_lookup(prices)
        closes_for = build_close_lookup(prices)
        bench_ret = _benchmark_daily_returns(prices, BENCHMARK_TICKER)
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
                panel = build_panel(sid, conn, prices, hv21_for, bench_ret, closes_for=closes_for)
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
