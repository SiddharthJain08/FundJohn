#!/usr/bin/env python3
"""unified_backtest.py — single source-of-truth strategy backtest.

Methodology (locked 2026-05-14):
  - Time scope:  full historical_regimes coverage (~2016-04 → today).
                 Sparse pre-2024 universe is accepted; statistical mass
                 lives in the dense modern era anyway.
  - Regime mode: discovery. Strategy runs on EVERY trading day in the
                 window; we ignore the strategy's own `active_in_regimes`
                 gate (by temporarily overriding it on the instance) so
                 we can observe regime-conditional performance from data.
  - Fidelity:    strategy signals + bracket walk. For each emitted signal
                 we walk forward day-by-day using OHLC bars from
                 prices.parquet, exiting on target_1 hit / stop_loss hit /
                 max_hold_days (default 21). No prefilter, no sizer
                 simulation, no slippage / commission model.
  - Storage:     three tables (strategy_backtest_runs +
                 strategy_backtest_regimes + strategy_backtest_trades),
                 append-only. The latest primary_window=true run per
                 strategy is what the dashboard / sizer / eligibility
                 assigner read.

CLI:
  python3 -m backtest.unified_backtest --strategy-id S_xx [--max-hold-days 21]
  python3 -m backtest.unified_backtest --all-live
  python3 -m backtest.unified_backtest --strategy-file src/strategies/implementations/S_xx.py

Exit codes:
  0  success (run_id printed)
  1  validation failed / import failed
  2  DB unavailable / write failed
"""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import inspect as _inspect
import json
import math
import os
import subprocess
import sys
import uuid
from dataclasses import asdict
from datetime import date as _date, datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = str(ROOT / 'src')
sys.path.insert(0, SRC_DIR)
sys.path.insert(0, str(ROOT))

from strategies.base import BaseStrategy, Signal, CANONICAL_REGIMES  # noqa: E402
from strategies.validate_strategy import validate                     # noqa: E402

# ── Configuration ────────────────────────────────────────────────────────────
TRADING_DAYS_PER_YEAR = 252
RISK_FREE_DAILY       = 0.05 / TRADING_DAYS_PER_YEAR
DEFAULT_MAX_HOLD_DAYS = 21
DEFAULT_START_DATE    = '2016-04-11'   # earliest historical_regimes row

PRICES_PARQUET = ROOT / 'data' / 'master' / 'prices.parquet'
REGIMES_PARQUET = ROOT / 'data' / 'master' / 'historical_regimes.parquet'


def _log(msg: str) -> None:
    print(f'[unified_backtest] {msg}', flush=True)


# ── Strategy loading (adapted from auto_backtest.py) ─────────────────────────

def _is_strategy_class(obj, module_name=None):
    if not _inspect.isclass(obj) or obj.__name__ == 'BaseStrategy':
        return False
    if _inspect.isabstract(obj):
        return False
    if module_name and getattr(obj, '__module__', None) != module_name:
        return False
    try:
        if issubclass(obj, BaseStrategy):
            return True
    except TypeError:
        pass
    return any(b.__name__ == 'BaseStrategy' for b in obj.__mro__[1:])


def load_strategy_class(filepath: str):
    """Load and return a strategy class from a .py file. Raises on failure."""
    val = validate(filepath)
    if not val['ok']:
        raise RuntimeError(f'contract validation failed: {"; ".join(val["errors"])}')
    abs_path = os.path.abspath(filepath)
    module_name = None
    if SRC_DIR in abs_path:
        rel = os.path.relpath(abs_path, SRC_DIR).replace(os.sep, '.')
        if rel.endswith('.py'):
            module_name = rel[:-3]
    if module_name:
        sys.modules.pop(module_name, None)
        module = importlib.import_module(module_name)
    else:
        spec = importlib.util.spec_from_file_location('_bt_strat', abs_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    classes = [obj for _, obj in _inspect.getmembers(module, _inspect.isclass)
               if _is_strategy_class(obj, module_name=module.__name__)]
    if not classes:
        raise RuntimeError(f'no strategy class in {filepath}')
    return classes[0]


def find_strategy_file(strategy_id: str) -> Optional[str]:
    """Search manifest + implementations dir for the .py file for a strategy_id."""
    impl_dir = ROOT / 'src' / 'strategies' / 'implementations'
    candidate = impl_dir / f'{strategy_id}.py'
    if candidate.exists():
        return str(candidate)
    manifest_path = ROOT / 'src' / 'strategies' / 'manifest.json'
    try:
        m = json.loads(manifest_path.read_text())
        entry = (m.get('strategies') or {}).get(strategy_id) or {}
        f = (entry.get('metadata') or {}).get('canonical_file')
        if f:
            full = impl_dir / f
            if full.exists():
                return str(full)
    except Exception:
        pass
    return None


def _code_sha(filepath: str) -> str:
    """git rev for the file at run-time, or content sha-256 if dirty / not tracked."""
    try:
        r = subprocess.run(['git', '-C', str(ROOT), 'log', '-1', '--format=%H', '--', filepath],
                           capture_output=True, text=True, timeout=5)
        sha = (r.stdout or '').strip()
        if sha:
            # Check if file has uncommitted changes
            diff = subprocess.run(['git', '-C', str(ROOT), 'diff', '--quiet', '--', filepath],
                                  capture_output=True, timeout=5)
            if diff.returncode == 0:
                return sha
            # Dirty — annotate
            return f'{sha[:12]}+dirty'
    except Exception:
        pass
    # Fallback: content hash
    import hashlib
    h = hashlib.sha256()
    h.update(Path(filepath).read_bytes())
    return f'sha256:{h.hexdigest()[:16]}'


# ── Data loading ─────────────────────────────────────────────────────────────

def load_prices_panels() -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    """Load prices.parquet and return:
      - close_wide: date × ticker close panel for strategy.generate_signals
      - bars_by_ticker: {ticker: DataFrame indexed by date with open/high/low/close}
        for the bracket walk-forward
    """
    p = pd.read_parquet(PRICES_PARQUET)
    # SP-2 Phase B Task 5: drop quarantined (ticker, date) rows before any
    # downstream conversion. Filter expects string dates (compares against
    # PG affected_date::TEXT), so it must run BEFORE pd.to_datetime below.
    from src.pipeline.quarantine_filter import filter_quarantined
    p = filter_quarantined(p, 'prices.parquet')
    p['date'] = pd.to_datetime(p['date'])
    p = p.sort_values(['ticker', 'date'])
    # Close panel (wide). Strategies expect index = date, columns = ticker, values = close.
    close_wide = p.pivot(index='date', columns='ticker', values='close')
    close_wide.index.name = 'date'
    bars_by_ticker = {t: g.set_index('date')[['open','high','low','close']]
                      for t, g in p.groupby('ticker')}
    return close_wide, bars_by_ticker


def load_regimes() -> pd.Series:
    """Return a Series of regime_state indexed by date (datetime)."""
    r = pd.read_parquet(REGIMES_PARQUET)
    r['date'] = pd.to_datetime(r['date'])
    r = r.sort_values('date').set_index('date')
    return r['regime'].astype(str)


# ── Simulation core ──────────────────────────────────────────────────────────

def _signal_to_long_short(direction: str) -> int:
    """Normalize a Signal.direction to +1 (long) or -1 (short). Returns 0 if unsupported."""
    if direction is None:
        return 0
    u = str(direction).strip().upper()
    if u in ('LONG', 'BUY', 'BUY_VOL'):
        return 1
    if u in ('SHORT', 'SELL', 'SELL_VOL'):
        return -1
    return 0


def simulate_trade(bars: pd.DataFrame, entry_date: pd.Timestamp,
                   direction: int, entry_price: float,
                   stop_loss: float, target_1: float,
                   max_hold_days: int) -> dict:
    """Walk forward from entry_date+1, returning the exit dict:
       {exit_date, exit_price, exit_reason, holding_days, pnl_pct}.

    Long: target hit when high >= target_1; stop when low <= stop_loss.
    Short: target hit when low <= target_1; stop when high >= stop_loss.
    Bracket is checked at each daily bar in priority: target first, then stop.
    (Bar-priority bias is a known limitation — the bar's intra-day path isn't
    available; we use the conservative interpretation that target fires first
    when both are touched in the same session.)

    If neither fires within max_hold_days → exit at the final day's close.
    If price data ends before max_hold → exit at the last available close
    with reason='end_of_data'.
    """
    bars_future = bars.loc[bars.index > entry_date]
    if bars_future.empty:
        return {'exit_date': entry_date, 'exit_price': entry_price,
                'exit_reason': 'end_of_data', 'holding_days': 0, 'pnl_pct': 0.0}
    bars_window = bars_future.iloc[:max_hold_days]
    for i, (dt, bar) in enumerate(bars_window.iterrows(), start=1):
        high, low, close = float(bar['high']), float(bar['low']), float(bar['close'])
        if direction > 0:   # long
            if high >= target_1:
                pnl = (target_1 - entry_price) / entry_price
                return {'exit_date': dt, 'exit_price': float(target_1),
                        'exit_reason': 'target', 'holding_days': i, 'pnl_pct': pnl}
            if low <= stop_loss:
                pnl = (stop_loss - entry_price) / entry_price
                return {'exit_date': dt, 'exit_price': float(stop_loss),
                        'exit_reason': 'stop', 'holding_days': i, 'pnl_pct': pnl}
        else:               # short
            if low <= target_1:
                pnl = (entry_price - target_1) / entry_price
                return {'exit_date': dt, 'exit_price': float(target_1),
                        'exit_reason': 'target', 'holding_days': i, 'pnl_pct': pnl}
            if high >= stop_loss:
                pnl = (entry_price - stop_loss) / entry_price
                return {'exit_date': dt, 'exit_price': float(stop_loss),
                        'exit_reason': 'stop', 'holding_days': i, 'pnl_pct': pnl}
    # Neither fired within max_hold → exit at last close
    last_dt = bars_window.index[-1]
    last_close = float(bars_window.iloc[-1]['close'])
    pnl_raw = (last_close - entry_price) / entry_price
    pnl = pnl_raw if direction > 0 else -pnl_raw
    reason = 'max_hold' if len(bars_window) == max_hold_days else 'end_of_data'
    return {'exit_date': last_dt, 'exit_price': last_close,
            'exit_reason': reason, 'holding_days': len(bars_window), 'pnl_pct': pnl}


# ── Metric aggregation ───────────────────────────────────────────────────────

def _portfolio_daily_returns(trades: list[dict]) -> tuple[np.ndarray, list[pd.Timestamp]]:
    """Build an equal-weighted daily portfolio return series.

    Methodology: each trade's total pnl_pct is distributed evenly across its
    holding_days as a per-day return contribution. On any given day, the
    portfolio return is the *equal-weighted average* of all trades that are
    open on that day (this is the marked-to-market view of a portfolio that
    rebalances daily to equal weight across currently-active positions).

    Returns (daily_returns_array, sorted_dates_list). Empty arrays for
    trades that have zero holding_days (degenerate same-day exits).
    """
    if not trades:
        return np.array([]), []
    daily_pnls: dict[pd.Timestamp, list[float]] = {}
    for t in trades:
        hold = int(t.get('holding_days') or 0)
        if hold <= 0:
            continue
        per_day = float(t['pnl_pct']) / hold
        start = pd.Timestamp(t['entry_date'])
        # Distribute over `hold` calendar days starting the day after entry.
        # Bracket-walk's holding_days counts trading days, but for portfolio
        # marking calendar-day spacing is close enough and avoids
        # re-computing trading-day calendars here. Drawdown shape is
        # insensitive to this small approximation.
        for i in range(1, hold + 1):
            d = start + pd.Timedelta(days=i)
            daily_pnls.setdefault(d, []).append(per_day)
    if not daily_pnls:
        return np.array([]), []
    sorted_dates = sorted(daily_pnls.keys())
    daily_returns = np.array([
        sum(daily_pnls[d]) / len(daily_pnls[d])  # equal-weight across active trades
        for d in sorted_dates
    ])
    return daily_returns, sorted_dates


def aggregate_metrics(trades: list[dict]) -> dict:
    """Total-level aggregate built from an equal-weight daily portfolio
    equity curve (Fix A, 2026-05-14). Replaces the prior cumulative-product
    of sequential trade pnls which produced spurious 99% drawdowns whenever
    many trades fired in parallel.

    Sharpe, max_dd, and return_pct all derive from the daily portfolio
    return series; hit_rate and avg_pnl_pct stay per-trade for interpretability.

    SP-2 Phase C: also emits sortino and calmar alongside existing keys.
    - sortino: annualized Sortino ratio (downside semi-deviation target=0);
               None if <2 daily points or no downside deviation.
    - calmar: annualized_return_pct / max_dd_pct; None if max_dd_pct==0.
    """
    if not trades:
        return {'sharpe': None, 'max_dd_pct': 0.0, 'return_pct': 0.0,
                'total_trades': 0, 'hit_rate': None, 'avg_holding_days': None,
                'avg_pnl_pct': 0.0, 'sortino': None, 'calmar': None}

    pnl = np.array([t['pnl_pct'] for t in trades], dtype=float)
    avg_hold = float(np.mean([t['holding_days'] for t in trades]) or 1.0)
    mean_pnl = float(pnl.mean())
    hit_rate = float((pnl > 0).mean())

    # Portfolio metrics from daily-marked equity curve.
    daily_returns, _dates = _portfolio_daily_returns(trades)
    if len(daily_returns) == 0:
        return {
            'sharpe':           None,
            'max_dd_pct':       0.0,
            'return_pct':       0.0,
            'total_trades':     len(trades),
            'hit_rate':         round(hit_rate, 4),
            'avg_holding_days': round(avg_hold, 2),
            'avg_pnl_pct':      round(mean_pnl * 100.0, 4),
            'sortino':          None,
            'calmar':           None,
        }

    eq = np.cumprod(1.0 + daily_returns)
    roll_max = np.maximum.accumulate(eq)
    dd = (eq - roll_max) / roll_max
    max_dd = float(abs(dd.min()))
    return_pct = float((eq[-1] - 1.0) * 100.0)

    std_dr = float(daily_returns.std(ddof=1)) if len(daily_returns) > 1 else 0.0
    if std_dr < 1e-9:
        sharpe = None
    else:
        sharpe = float((daily_returns.mean() - RISK_FREE_DAILY) / std_dr *
                       math.sqrt(TRADING_DAYS_PER_YEAR))

    # Sortino ratio (annualized, target=0).
    # Convention: downside_dev = RMS of negative-only daily returns,
    # i.e. sqrt(mean(r_i**2)) over all r_i < 0. This is equivalent to
    # the full-N denominator form used by many practitioners (only negative
    # return days contribute; the denominator is the total number of
    # *negative* return days, not the total sample size). This differs from
    # the textbook semi-deviation which uses the full-N denominator. Both
    # conventions annualize by multiplying by sqrt(252).
    sortino: Optional[float] = None
    if len(daily_returns) >= 2:
        downside = daily_returns[daily_returns < 0.0]
        if len(downside) > 0:
            downside_dev = float(np.sqrt(np.mean(downside ** 2)))
            if downside_dev > 1e-9:
                sortino = float(daily_returns.mean() / downside_dev *
                                math.sqrt(TRADING_DAYS_PER_YEAR))

    # Calmar ratio: annualized return / max drawdown.
    # Annualized return derived from the equity curve's CAGR over the sample.
    calmar: Optional[float] = None
    if max_dd > 1e-9:
        n_days = len(daily_returns)
        if n_days > 0:
            annualized_return_pct = float((eq[-1] ** (TRADING_DAYS_PER_YEAR / n_days) - 1.0) * 100.0)
            calmar = float(annualized_return_pct / (max_dd * 100.0))

    return {
        'sharpe':           None if sharpe is None else round(sharpe, 4),
        'max_dd_pct':       round(max_dd * 100.0, 4),
        'return_pct':       round(return_pct, 4),
        'total_trades':     len(trades),
        'hit_rate':         round(hit_rate, 4),
        'avg_holding_days': round(avg_hold, 2),
        'avg_pnl_pct':      round(mean_pnl * 100.0, 4),
        'sortino':          None if sortino is None else round(sortino, 4),
        'calmar':           None if calmar is None else round(calmar, 4),
    }


def aggregate_per_regime(trades: list[dict], regimes: pd.Series) -> dict[str, dict]:
    """Group trades by entry_regime, compute aggregates per regime. Also
    counts oos_days_in_regime so the operator can see sample size context.
    """
    out: dict[str, dict] = {}
    by_regime: dict[str, list[dict]] = {r: [] for r in CANONICAL_REGIMES}
    for t in trades:
        r = t.get('entry_regime')
        if r in CANONICAL_REGIMES:
            by_regime[r].append(t)
    regime_day_counts = regimes.value_counts().to_dict()
    for regime, regime_trades in by_regime.items():
        agg = aggregate_metrics(regime_trades)
        agg['trade_count']        = agg.pop('total_trades')
        agg['oos_days_in_regime'] = int(regime_day_counts.get(regime, 0))
        # NULL the sharpe/sortino/calmar under low-sample regimes to avoid noise downstream
        if agg['trade_count'] < 5:
            agg['sharpe'] = None
            agg['sortino'] = None
            agg['calmar'] = None
        out[regime] = agg
    return out


# ── Per-bar simulation core (shared by run_backtest and universe_grid_cli) ────

def _per_bar_simulate(
    instance,
    close_wide: pd.DataFrame,
    bars_by_ticker: dict,
    regimes: pd.Series,
    start_dt: pd.Timestamp,
    end_dt: pd.Timestamp,
    *,
    strategy_id: Optional[str] = None,
    resolver=None,
    max_hold_days: int = DEFAULT_MAX_HOLD_DAYS,
) -> dict:
    """Single source-of-truth for the per-bar simulation loop.

    Encapsulates: min_lookback gate, regime_payload construction,
    generate_signals 4-arg→3-arg fallback, entry_regime tagging,
    stop/target sanity skips, simulate_trade, and per-bar universe-size
    tracking.

    Returns a dict with keys:
      - trades: list[dict]
      - universe_sizes: list[int]  (non-empty only when resolver is not None)
      - days_processed: int
      - days_with_signals: int
      - static_universe: list[str]  (computed from close_wide columns)
      - min_lookback: int           (taken from instance attr or 20)

    ``instance`` must already have ``active_in_regimes`` set to cover all
    regimes (discovery mode); callers are responsible for this.
    ``strategy_id`` is only required when ``resolver`` is not None.
    """
    # Static universe: equity tickers only (no indices, crypto, futures).
    static_universe = [c for c in close_wide.columns
                       if not c.startswith('^') and '-USD' not in c and '=F' not in c]
    min_lookback = getattr(instance, 'min_lookback', 20)

    # Aux-data loader — strategies that need options/financials get it.
    try:
        from strategies.aux_data_loader import load_aux_data
    except Exception:
        load_aux_data = None

    oos_dates = close_wide.loc[start_dt:end_dt].index

    trades: list[dict] = []
    days_processed = 0
    days_with_signals = 0
    # Track per-bar universe sizes when resolver is active.
    universe_sizes: list[int] = []

    for current_date in oos_dates:
        # Need at least min_lookback days of history before strategy can run
        prices_to_date = close_wide.loc[:current_date]
        if len(prices_to_date) < min_lookback + 5:
            continue

        cur_d = current_date.date() if hasattr(current_date, 'date') else current_date
        regime_state = regimes.get(current_date, None)
        if regime_state is None or pd.isna(regime_state):
            continue

        # SP-2 Phase C: when resolver is set, replace the static universe with
        # a point-in-time resolved one for this specific bar.
        if resolver is not None:
            bar_universe = resolver.resolve(strategy_id, as_of=cur_d)
            universe_sizes.append(len(bar_universe))
        else:
            bar_universe = static_universe

        regime_payload = {
            'state':            str(regime_state),
            'date':             cur_d.isoformat(),
            'one_hot':          {r: (1.0 if r == regime_state else 0.0) for r in CANONICAL_REGIMES},
            'transition_probs': {r1: {r2: (1.0 if r1 == r2 else 0.0) for r2 in CANONICAL_REGIMES}
                                 for r1 in CANONICAL_REGIMES},
        }

        # Aux data is point-in-time-safe — loader keys by date.
        aux = {'options': {}}
        if load_aux_data is not None:
            try:
                aux = load_aux_data(current_date)
            except Exception:
                aux = {'options': {}}

        try:
            signals = instance.generate_signals(prices_to_date, regime_payload, bar_universe, aux_data=aux)
        except TypeError:
            try:
                signals = instance.generate_signals(prices_to_date, regime_payload, bar_universe)
            except Exception:
                continue
        except Exception:
            continue

        days_processed += 1
        if not signals:
            continue
        days_with_signals += 1

        for sig in signals[:instance.MAX_SIGNALS]:
            direction = _signal_to_long_short(sig.direction)
            if direction == 0:
                continue
            ticker = sig.ticker
            if ticker not in bars_by_ticker:
                continue
            ticker_bars = bars_by_ticker[ticker]
            # Need the signal-day bar so we can use its close as the entry price
            if current_date not in ticker_bars.index:
                continue
            entry_price = float(sig.entry_price) if (sig.entry_price and sig.entry_price > 0) \
                          else float(ticker_bars.loc[current_date, 'close'])
            stop_loss = float(sig.stop_loss) if (sig.stop_loss and sig.stop_loss > 0) \
                        else (entry_price * 0.93 if direction > 0 else entry_price * 1.07)
            target_1 = float(sig.target_1) if (sig.target_1 and sig.target_1 > 0) \
                       else (entry_price * 1.08 if direction > 0 else entry_price * 0.92)
            # Defensive: a bad signal with stop/target on the wrong side of
            # entry will produce a guaranteed-loss trade. Skip rather than
            # carry the bug through.
            if direction > 0 and (stop_loss >= entry_price or target_1 <= entry_price):
                continue
            if direction < 0 and (stop_loss <= entry_price or target_1 >= entry_price):
                continue
            exit_info = simulate_trade(ticker_bars, current_date, direction,
                                       entry_price, stop_loss, target_1, max_hold_days)
            trades.append({
                'ticker':         ticker,
                'direction':      'long' if direction > 0 else 'short',
                'entry_date':     cur_d,
                'entry_price':    entry_price,
                'exit_date':      exit_info['exit_date'].date() if hasattr(exit_info['exit_date'], 'date') else exit_info['exit_date'],
                'exit_price':     exit_info['exit_price'],
                'exit_reason':    exit_info['exit_reason'],
                'holding_days':   exit_info['holding_days'],
                'pnl_pct':        exit_info['pnl_pct'],
                'entry_regime':   str(regime_state),
                'signal_stop':    stop_loss,
                'signal_target':  target_1,
            })

    return {
        'trades':           trades,
        'universe_sizes':   universe_sizes,
        'days_processed':   days_processed,
        'days_with_signals': days_with_signals,
        'static_universe':  static_universe,
        'min_lookback':     min_lookback,
    }


# ── Main run ─────────────────────────────────────────────────────────────────

def run_backtest(strategy_id: str, *,
                 filepath: Optional[str] = None,
                 start_date: str = DEFAULT_START_DATE,
                 end_date: Optional[str] = None,
                 max_hold_days: int = DEFAULT_MAX_HOLD_DAYS,
                 conn: Optional[psycopg2.extensions.connection] = None,
                 commit: bool = True,
                 resolver=None) -> str:
    """Execute the unified backtest for one strategy. Returns the run_id (UUID).

    Side effect: writes one row to strategy_backtest_runs, up to 4 rows
    to strategy_backtest_regimes (one per regime that produced trades),
    and N rows to strategy_backtest_trades.

    SP-2 Phase C: optional ``resolver`` kwarg. When set (not None), the per-bar
    universe is resolved via ``resolver.resolve(strategy_id, as_of=cur_d)``
    instead of the static universe computed once from close_wide columns.
    The mean of per-bar universe sizes is stored in ``_universe_sizes_out``
    on the resolver object after the run, and also returned via the
    ``run_backtest_grid`` wrapper. When resolver is None the function is
    byte-identical to the pre-resolver implementation.
    """
    filepath = filepath or find_strategy_file(strategy_id)
    if not filepath:
        raise FileNotFoundError(f'no implementation file for {strategy_id}')
    strategy_cls = load_strategy_class(filepath)
    _log(f'loaded {strategy_cls.__name__} from {Path(filepath).relative_to(ROOT)}')

    # Discovery mode: bypass should_run() by widening active_in_regimes on
    # the instance. Strategies that branch on regime['state'] *inside*
    # generate_signals still see the live regime; they just don't get to
    # early-exit on it.
    instance = strategy_cls()
    instance.active_in_regimes = list(CANONICAL_REGIMES)

    close_wide, bars_by_ticker = load_prices_panels()
    regimes = load_regimes()
    _log(f'prices: {close_wide.shape[0]} dates × {close_wide.shape[1]} tickers; '
         f'regimes: {len(regimes)} days')

    start_dt = pd.Timestamp(start_date)
    end_dt   = pd.Timestamp(end_date) if end_date else close_wide.index.max()

    sim = _per_bar_simulate(
        instance, close_wide, bars_by_ticker, regimes, start_dt, end_dt,
        strategy_id=strategy_id,
        resolver=resolver,
        max_hold_days=max_hold_days,
    )
    trades         = sim['trades']
    universe_sizes = sim['universe_sizes']
    days_processed = sim['days_processed']
    days_with_signals = sim['days_with_signals']
    universe       = sim['static_universe']
    min_lookback   = sim['min_lookback']

    _log(f'simulation: {days_processed} active days, {days_with_signals} signal days, {len(trades)} trades')

    # SP-2 Phase C: store mean universe size on resolver for caller retrieval.
    if resolver is not None and universe_sizes:
        resolver._universe_sizes_out = universe_sizes
    elif resolver is not None:
        resolver._universe_sizes_out = []

    # ── Aggregate metrics ────────────────────────────────────────────────
    total_metrics = aggregate_metrics(trades)
    per_regime    = aggregate_per_regime(trades, regimes.loc[start_dt:end_dt])

    # ── Persist ──────────────────────────────────────────────────────────
    own_conn = False
    if conn is None:
        uri = os.environ.get('POSTGRES_URI')
        if not uri:
            raise RuntimeError('POSTGRES_URI not set')
        conn = psycopg2.connect(uri)
        own_conn = True

    run_id = str(uuid.uuid4())
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO strategy_backtest_runs
              (run_id, strategy_id, code_sha, window_kind, start_date, end_date,
               oos_days, total_sharpe, total_max_dd_pct, total_return_pct,
               total_trades, total_hit_rate, avg_holding_days, primary_window,
               config_json, notes)
            VALUES (%s,%s,%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s, %s,%s)
        """, (
            run_id, strategy_id, _code_sha(filepath), 'full_history',
            start_dt.date(), end_dt.date(), int((end_dt - start_dt).days + 1),
            total_metrics['sharpe'], total_metrics['max_dd_pct'],
            total_metrics['return_pct'], total_metrics['total_trades'],
            total_metrics['hit_rate'], total_metrics['avg_holding_days'],
            True,
            json.dumps({
                'max_hold_days':  max_hold_days,
                'min_lookback':   min_lookback,
                'start_date':     start_dt.date().isoformat(),
                'end_date':       end_dt.date().isoformat(),
                'universe_size':  (round(sum(universe_sizes) / len(universe_sizes), 2)
                                   if resolver is not None and universe_sizes
                                   else len(universe)),
                'methodology':    'discovery',
            }),
            None,
        ))
        # 2026-05-19: always write a row per canonical regime, even when
        # the strategy produced 0 trades in that regime. The dashboard's
        # per-regime BT Sharpe view (renderBacktestRegimeBreakdown) reads
        # one breakdown row per (strategy, regime) — pre-fix we only wrote
        # regimes with trades, so candidates whose strategy only fires in
        # 1-2 regimes silently lost their "By Regime" cell coverage on the
        # rest. Matches the auto_backtest.py behaviour shipped in ea5fafa.
        # Zero-trade regimes get trade_count=0 + NULLs for the metric
        # columns (schema allows it; only trade_count is NOT NULL).
        regime_rows = []
        for regime in CANONICAL_REGIMES:
            agg = per_regime.get(regime, {})
            n_trades = int(agg.get('trade_count', 0) or 0)
            if n_trades == 0:
                regime_rows.append((
                    run_id, regime,
                    0, None, None, None, None, None, None,
                    int(agg.get('oos_days_in_regime') or 0),
                ))
                continue
            regime_rows.append((
                run_id, regime,
                agg['trade_count'], agg['sharpe'], agg['max_dd_pct'],
                agg['return_pct'], agg['hit_rate'], agg['avg_pnl_pct'],
                agg['avg_holding_days'], agg['oos_days_in_regime'],
            ))
        if regime_rows:
            psycopg2.extras.execute_values(cur, """
                INSERT INTO strategy_backtest_regimes
                  (run_id, regime_state, trade_count, sharpe, max_dd_pct,
                   return_pct, hit_rate, avg_pnl_pct, avg_holding_days,
                   oos_days_in_regime)
                VALUES %s
            """, regime_rows)
        if trades:
            trade_rows = [(
                run_id, i + 1, strategy_id, t['ticker'], t['direction'],
                t['entry_date'], t['entry_price'], t['exit_date'],
                t['exit_price'], t['exit_reason'], t['pnl_pct'],
                t['holding_days'], t['entry_regime'], t['signal_stop'],
                t['signal_target'],
            ) for i, t in enumerate(trades)]
            psycopg2.extras.execute_values(cur, """
                INSERT INTO strategy_backtest_trades
                  (run_id, trade_seq, strategy_id, ticker, direction,
                   entry_date, entry_price, exit_date, exit_price,
                   exit_reason, pnl_pct, holding_days, entry_regime,
                   signal_stop, signal_target)
                VALUES %s
            """, trade_rows, page_size=500)

        # Demote prior primary_window=true for this strategy so dashboard
        # / sizer / eligibility reads converge on the new run as the single
        # source of truth. The prior rows stay (audit history) but get
        # primary_window=false.
        cur.execute("""
            UPDATE strategy_backtest_runs
            SET primary_window = FALSE
            WHERE strategy_id = %s AND run_id <> %s
        """, (strategy_id, run_id))

        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if own_conn:
            conn.close()

    _log(f'wrote run_id={run_id}  total_sharpe={total_metrics["sharpe"]}  '
         f'trades={total_metrics["total_trades"]}  '
         f'regimes={list(per_regime.keys())}')
    return run_id


# ── CLI ──────────────────────────────────────────────────────────────────────

def _all_live_strategies() -> list[str]:
    manifest_path = ROOT / 'src' / 'strategies' / 'manifest.json'
    m = json.loads(manifest_path.read_text())
    return [sid for sid, entry in (m.get('strategies') or {}).items()
            if entry.get('state') in ('live', 'candidate', 'staging')]


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--strategy-id', help='Run backtest for one strategy_id')
    g.add_argument('--strategy-file', help='Run backtest for a .py file directly')
    g.add_argument('--all-live', action='store_true',
                   help='Run for all live + candidate + staging strategies')
    ap.add_argument('--start-date', default=DEFAULT_START_DATE)
    ap.add_argument('--end-date',   default=None)
    ap.add_argument('--max-hold-days', type=int, default=DEFAULT_MAX_HOLD_DAYS)
    args = ap.parse_args()

    if args.all_live:
        sids = _all_live_strategies()
        _log(f'running {len(sids)} strategies')
        ok = 0; fail = 0
        for sid in sids:
            try:
                run_backtest(sid,
                             start_date=args.start_date, end_date=args.end_date,
                             max_hold_days=args.max_hold_days)
                ok += 1
            except Exception as e:
                _log(f'FAIL {sid}: {type(e).__name__}: {e}')
                fail += 1
        _log(f'done: ok={ok} fail={fail}')
        return 0 if fail == 0 else 1

    if args.strategy_id:
        try:
            run_backtest(args.strategy_id,
                         start_date=args.start_date, end_date=args.end_date,
                         max_hold_days=args.max_hold_days)
            return 0
        except FileNotFoundError as e:
            _log(f'FAIL: {e}'); return 1
        except Exception as e:
            _log(f'FAIL: {type(e).__name__}: {e}')
            return 1

    if args.strategy_file:
        sid = Path(args.strategy_file).stem
        try:
            run_backtest(sid, filepath=args.strategy_file,
                         start_date=args.start_date, end_date=args.end_date,
                         max_hold_days=args.max_hold_days)
            return 0
        except Exception as e:
            _log(f'FAIL: {type(e).__name__}: {e}')
            return 1
    return 1


def run_backtest_with_resolver(strategy, start, end, resolver, **kwargs):
    """SP-2 Phase A: per-bar universe resolution.

    Strategies invoked via this entry point receive a fresh `universe` list
    each bar from `resolver.resolve(strategy.id, as_of=bar_date)`. Existing
    `run_backtest` keeps its current signature for backward compat.
    Returns list[{"date": d, "signals": [...]}].
    """
    from src.backtest._trading_calendar import trading_days
    results = []
    for bar_date in trading_days(start, end):
        universe = resolver.resolve(strategy.id, as_of=bar_date)
        signals = strategy.generate(bar_date, universe)
        results.append({"date": bar_date, "signals": signals})
    return results


if __name__ == '__main__':
    sys.exit(main())
