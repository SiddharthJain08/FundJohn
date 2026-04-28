"""
auto_backtest.py — Regime-stratified walk-forward backtest for generated strategies.

Usage:
    python3 src/strategies/auto_backtest.py src/strategies/implementations/S_xx_foo.py

Exit code 0 = backtest ran (metrics printed). Exit code 1 = strategy couldn't
execute at all (contract violation, import error, no strategy class, no prices).

This script is a *pure metrics reporter* — it does NOT gate candidate→live.
The only blocker for a successful run is code that literally can't execute;
that's captured via the `error` field (non-null when blocking). The human
judges strategy quality from the persisted regime breakdown in the dashboard.

How regime stratification works
-------------------------------
Each strategy declares `active_in_regimes` (LOW_VOL / TRANSITIONING /
HIGH_VOL / CRISIS). For every declared regime, we pull the longest
historical periods where that regime held continuously (via
`historical_regimes.find_regime_windows`) and run an OOS window per period.
At each rebalance step inside a window we look up the *actual historical
regime* (`regime_for_date(current_date)`) and pass it to `generate_signals`,
so the strategy's own self-gate (which checks `regime['state']` against
its `active_in_regimes`) sees realistic state transitions instead of one
hardcoded value.

Prints JSON:
{
  "sharpe": <trade-weighted aggregate>,
  "max_dd": <max across windows>,
  "total_return_pct": <compounded across windows>,
  "trade_count": <sum>,
  "regime_breakdown": {
     "LOW_VOL":       {"sharpe":…, "max_dd":…, "total_return_pct":…, "trade_count":…, "oos_days":…},
     "TRANSITIONING": {…},
     "HIGH_VOL":      {"note": "not_declared"} | {"note": "no_oos_window"} | {…metrics…},
     "CRISIS":        {…},
  },
  "windows": [{"window": …, "regime": …, "sharpe":…, "max_dd":…, "trade_count":…, "total_return_pct":…}],
  "method": "v2_regime_stratified",
  "error": null
}
"""

import sys
import os
import json
import traceback

import pandas as pd
import numpy as np

ROOT    = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(ROOT, 'src')
sys.path.insert(0, ROOT)
sys.path.insert(0, SRC_DIR)

from strategies.validate_strategy import validate
from strategies.historical_regimes import (
    classify_history,
    find_regime_windows,
    regime_for_date,
    CANONICAL_REGIMES,
)

TRADING_DAYS    = 252
RISK_FREE_DAILY = 0.05 / TRADING_DAYS

# ── Regime-stratified windowing parameters ───────────────────────────────────
#
# WINDOWS_PER_REGIME caps the OOS workload for strategies declaring all four
# regimes (worst case: 4 × 2 = 8 windows ≈ 4 minutes wall time at typical
# step cost — well under _codeFromQueue's 600s timeout).
#
# MIN_REGIME_OOS_DAYS is the floor for a usable window. CRISIS coverage is
# sparse (one ~80-day COVID stretch); HIGH_VOL is also limited. Strategies
# whose only declared regime is CRISIS-or-HIGH_VOL get one window — we record
# that and emit `oos_days` so the operator sees the sample size.
#
# TRAIN_RUNWAY_START is the earliest training date passed to the strategy
# (pre-OOS warmup data). prices.parquet starts 2016-04-10 so this is the
# realistic floor; strategies' min_lookback is enforced separately.
WINDOWS_PER_REGIME    = 2
MIN_REGIME_OOS_DAYS   = 60
TRAIN_RUNWAY_START    = '2016-01-01'

REBALANCE_FREQ   = 21   # trading-day step between signal generation calls
MAX_HOLD_DAYS    = 21   # max days in any position
INITIAL_CAPITAL  = 1_000_000.0
MAX_POSITION_PCT = 0.05  # cap per-position at 5% of portfolio


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_prices() -> pd.DataFrame:
    """Load prices.parquet and pivot to wide format (date index, ticker columns).

    Set OPENCLAW_APPLY_SPLIT_ADJUSTMENT=1 to apply forward/reverse split
    adjustments via `src/backtest/adjust_for_corporate_actions.adjust_dataframe`.
    Default OFF: the current upstream sources (yfinance / Polygon) return
    auto-adjusted close prices, so applying our adjuster on top would
    DOUBLE-adjust and corrupt pre-split data. The wire-up exists so that
    (a) raw unadjusted sources (e.g. Alpaca data bars) can opt in, and
    (b) one-off backtests can compare adjusted vs raw with parity.
    Master parquet is never mutated — adjustments are read-side only.
    """
    long = pd.read_parquet(os.path.join(ROOT, 'data', 'master', 'prices.parquet'))
    if os.environ.get('OPENCLAW_APPLY_SPLIT_ADJUSTMENT') == '1':
        try:
            from backtest.adjust_for_corporate_actions import adjust_dataframe
            long = adjust_dataframe(long, ticker_col='ticker', date_col='date',
                                    close_cols=('open', 'high', 'low', 'close'))
        except ImportError:
            pass
    wide = long.pivot_table(index='date', columns='ticker', values='close')
    wide.index = pd.to_datetime(wide.index)
    wide.sort_index(inplace=True)
    return wide


# Default per-regime feature payloads. Strategies that read fields beyond
# `state` (e.g. stress_score, vix) get a typical value for the regime so
# numerical gates inside generate_signals don't crash on a missing key.
_REGIME_FEATURE_DEFAULTS = {
    'LOW_VOL':       {'vix': 13.0, 'stress_score': 12, 'roro_score': 60.0,
                      'vix_term_slope': 1.4, 'spx_rv_20d':  9.0,
                      'hy_ig_spread': 0.008, 'spx_5d_return':  0.01,
                      'vix_5d_chg': -0.3},
    'TRANSITIONING': {'vix': 18.0, 'stress_score': 25, 'roro_score': 45.0,
                      'vix_term_slope': 1.0, 'spx_rv_20d': 14.0,
                      'hy_ig_spread': 0.012, 'spx_5d_return':  0.0,
                      'vix_5d_chg':  0.2},
    'HIGH_VOL':      {'vix': 26.0, 'stress_score': 50, 'roro_score': 25.0,
                      'vix_term_slope': 0.5, 'spx_rv_20d': 22.0,
                      'hy_ig_spread': 0.020, 'spx_5d_return': -0.02,
                      'vix_5d_chg':  0.6},
    'CRISIS':        {'vix': 45.0, 'stress_score': 85, 'roro_score': 10.0,
                      'vix_term_slope': -0.5, 'spx_rv_20d': 40.0,
                      'hy_ig_spread': 0.060, 'spx_5d_return': -0.06,
                      'vix_5d_chg':  3.0},
}


def _make_regime_payload(state: str, current_date: pd.Timestamp,
                         vix_value: float | None = None) -> dict:
    """Build the regime dict passed to generate_signals. `state` is the
    actual historical regime for current_date (per the deterministic
    classifier). Other features are reasonable defaults for that regime —
    not historically exact but good enough for gating logic."""
    if state not in CANONICAL_REGIMES:
        state = 'TRANSITIONING'
    feats = dict(_REGIME_FEATURE_DEFAULTS[state])
    if vix_value is not None and not pd.isna(vix_value):
        feats['vix'] = float(vix_value)
    one_hot = {r: (1.0 if r == state else 0.0) for r in CANONICAL_REGIMES}
    return {
        'state': state,
        'state_probabilities': one_hot,
        'confidence': 1.0,
        # Deterministic backtest doesn't need predictive transition probs;
        # fix to "stay" so any strategy looking at this gets a non-degenerate
        # value.
        'transition_probs_tomorrow': {**one_hot, state: 0.85,
                                       **{r: (0.05 if r != state else 0.85)
                                          for r in CANONICAL_REGIMES}},
        'stress_score': feats['stress_score'],
        'roro_score':   feats['roro_score'],
        'features':     {k: v for k, v in feats.items()
                         if k not in ('stress_score', 'roro_score')},
        'regime_change_alert': False,
        'days_in_current_state': 20,
        'position_scale': 1.0,
        'classifier': 'vix_tier_5d_median',
        'as_of_date':  current_date.date().isoformat()
                       if hasattr(current_date, 'date') else str(current_date),
    }


def regime_windows_for_strategy(active_in_regimes: list[str]) -> list[dict]:
    """Pick OOS windows for a strategy declaring the given active regimes.

    Returns a list of {label, regime, train_start, oos_start, oos_end},
    capped at WINDOWS_PER_REGIME per declared regime. Spans shorter than
    MIN_REGIME_OOS_DAYS are dropped.

    For each declared regime we sort the available historical windows by
    duration (longest first) and take the top WINDOWS_PER_REGIME. The
    training prefix is the same for all windows (TRAIN_RUNWAY_START → the
    OOS start), so warmup data is always available."""
    out: list[dict] = []
    for regime in active_in_regimes or []:
        if regime not in CANONICAL_REGIMES:
            continue
        windows = find_regime_windows(regime, min_days=MIN_REGIME_OOS_DAYS)
        # Sort longest-first so we keep the most informative spans when
        # capped.
        windows = sorted(
            windows,
            key=lambda se: (pd.to_datetime(se[1]) - pd.to_datetime(se[0])).days,
            reverse=True,
        )[:WINDOWS_PER_REGIME]
        for s, e in windows:
            out.append({
                'label':       f'{regime}@{s}',
                'regime':      regime,
                'train_start': TRAIN_RUNWAY_START,
                'oos_start':   s,
                'oos_end':     e,
            })
    # Sort chronologically by oos_start so the dashboard reads the result
    # left-to-right by time, not by regime.
    out.sort(key=lambda w: w['oos_start'])
    return out


def _build_vix_lookup() -> pd.Series:
    """date → VIX close, indexed by `datetime.date`. Forward-fills to handle
    weekends/holidays the OOS step might land on."""
    df = classify_history()
    if df.empty:
        return pd.Series(dtype=float)
    s = df.set_index('date')['vix']
    return s


def _run_regime_window(strategy_cls,
                      prices: pd.DataFrame,
                      train_start: str,
                      oos_start: str,
                      oos_end: str,
                      vix_lookup: pd.Series) -> dict:
    """
    Run one OOS window with time-varying regime. Returns
    {sharpe, max_dd, trade_count, total_return_pct, regime_step_counts, ...}.

    At each rebalance step, the regime payload passed to generate_signals
    reflects the *actual historical regime* of current_date — so the
    strategy's own self-gate (should_run / regime['state'] checks) sees
    realistic regime transitions instead of one hardcoded value.

    The strategy can see all prices from train_start up to the current
    rebalance date (point-in-time safe). We score only on the OOS period.
    """
    universe = [c for c in prices.columns if not c.startswith('^') and '-USD' not in c and '=F' not in c]

    oos_start_dt = pd.Timestamp(oos_start)
    oos_end_dt   = pd.Timestamp(oos_end)
    train_start_dt = pd.Timestamp(train_start)

    # OOS trading days
    oos_dates = prices.loc[oos_start_dt:oos_end_dt].index
    if len(oos_dates) == 0:
        return {'sharpe': 0.0, 'max_dd': 0.0, 'trade_count': 0,
                'total_return_pct': 0.0, 'note': 'no OOS dates'}

    # Portfolio state
    cash         = INITIAL_CAPITAL
    positions    = {}   # {(ticker, open_date): {shares, stop, target, hold_until}}
    equity_curve = {}   # {date: portfolio_value}
    trade_count  = 0
    regime_step_counts: dict[str, int] = {}  # how many rebalance steps fell in each regime

    instance = strategy_cls()

    # Step through OOS at rebalance frequency
    step_indices = list(range(0, len(oos_dates), REBALANCE_FREQ))
    if step_indices[-1] != len(oos_dates) - 1:
        step_indices.append(len(oos_dates) - 1)

    for idx in step_indices:
        current_date = oos_dates[idx]

        # Look up the actual historical regime for this step's date and
        # build the regime payload the strategy will receive. This is the
        # core of regime stratification — the strategy's own self-gate
        # (should_run / regime['state'] checks inside generate_signals)
        # sees the realistic regime at this point in time, not a constant.
        cur_d = current_date.date() if hasattr(current_date, 'date') else current_date
        regime_state = regime_for_date(cur_d)
        if regime_state == 'UNKNOWN':
            # No VIX data for this date (gap before backfill, etc.). Skip
            # the step — better than fabricating a regime.
            continue
        vix_val = vix_lookup.get(cur_d)
        regime = _make_regime_payload(regime_state, current_date, vix_val)
        regime_step_counts[regime_state] = regime_step_counts.get(regime_state, 0) + 1

        # Mark-to-market open positions and check exits
        to_close = []
        for key, pos in positions.items():
            ticker, _ = key
            if ticker not in prices.columns:
                to_close.append(key)
                continue
            current_price_series = prices.loc[:current_date, ticker].dropna()
            if current_price_series.empty:
                continue
            current_price = float(current_price_series.iloc[-1])

            # Exit: stop hit, target hit, or max hold reached
            hit_stop   = current_price <= pos['stop']
            hit_target = current_price >= pos['target']
            max_hold   = current_date >= pos['hold_until']

            if hit_stop or hit_target or max_hold:
                proceeds = pos['shares'] * current_price
                cash    += proceeds
                trade_count += 1
                to_close.append(key)

        for key in to_close:
            positions.pop(key, None)

        # Compute current portfolio value
        portfolio_val = cash
        for key, pos in positions.items():
            ticker, _ = key
            if ticker in prices.columns:
                price_s = prices.loc[:current_date, ticker].dropna()
                if not price_s.empty:
                    portfolio_val += pos['shares'] * float(price_s.iloc[-1])
        equity_curve[current_date] = portfolio_val

        # Generate new signals (strategy sees all prices up to current_date)
        try:
            prices_to_date = prices.loc[train_start_dt:current_date]
            if len(prices_to_date) < getattr(instance, 'min_lookback', 20) + 5:
                continue
            # Load options aux_data panel for this date (enables HV-series strategies).
            # Empty panel for pre-backfill dates is fine — strategies fall back to defaults.
            try:
                from strategies.aux_data_loader import load_aux_data
                aux_data = load_aux_data(current_date)
            except Exception:
                aux_data = {'options': {}}
            signals = instance.generate_signals(prices_to_date, regime, universe, aux_data=aux_data)
        except TypeError:
            # Older strategies may not accept aux_data kwarg — try without it.
            try:
                signals = instance.generate_signals(prices_to_date, regime, universe)
            except Exception:
                continue
        except Exception:
            continue

        if not signals:
            continue

        # Open positions for new signals (don't double-enter same ticker)
        active_tickers = {k[0] for k in positions}
        for sig in signals[:10]:  # cap at 10 signals per rebalance
            if sig.ticker in active_tickers:
                continue
            if sig.ticker not in prices.columns:
                continue
            entry_px_series = prices.loc[:current_date, sig.ticker].dropna()
            if entry_px_series.empty:
                continue
            entry_price = float(entry_px_series.iloc[-1])
            if entry_price <= 0:
                continue

            # Size: min(signal's position_size_pct, MAX_POSITION_PCT) × portfolio
            size_pct = min(float(sig.position_size_pct or 0.02), MAX_POSITION_PCT)
            alloc    = portfolio_val * size_pct
            shares   = alloc / entry_price
            cost     = shares * entry_price

            if cost > cash * 0.95 or cost <= 0:
                continue

            # Stop and target
            stop   = float(sig.stop_loss)   if sig.stop_loss   and sig.stop_loss > 0   else entry_price * 0.93
            target = float(sig.target_1)    if sig.target_1    and sig.target_1 > 0    else entry_price * 1.08
            # Ensure stop < entry < target for LONG (invert for SHORT)
            if sig.direction == 'LONG' and stop >= entry_price:
                stop = entry_price * 0.93
            if sig.direction in ('SHORT', 'SELL_VOL'):
                stop   = entry_price * 1.07
                target = entry_price * 0.93

            hold_until_pos = min(idx + MAX_HOLD_DAYS, len(oos_dates) - 1)
            hold_until = oos_dates[hold_until_pos]

            cash -= cost
            positions[(sig.ticker, current_date)] = {
                'shares':     shares,
                'stop':       stop,
                'target':     target,
                'hold_until': hold_until,
                'direction':  sig.direction,
            }
            active_tickers.add(sig.ticker)

    # Force-close all remaining positions at end
    final_date = oos_dates[-1]
    for key, pos in positions.items():
        ticker, _ = key
        if ticker in prices.columns:
            price_s = prices.loc[:final_date, ticker].dropna()
            if not price_s.empty:
                cash += pos['shares'] * float(price_s.iloc[-1])
                trade_count += 1
    equity_curve[final_date] = cash  # approximate final value

    # ── Compute metrics ────────────────────────────────────────────────────────
    oos_days = (oos_end_dt - oos_start_dt).days + 1

    if len(equity_curve) < 5:
        return {'sharpe': 0.0, 'max_dd': 0.0, 'total_return_pct': 0.0,
                'trade_count': trade_count, 'note': 'too few equity points',
                'regime_step_counts': regime_step_counts, 'oos_days': oos_days}

    eq = pd.Series(equity_curve).sort_index()
    daily_ret = eq.pct_change().dropna()

    if len(daily_ret) < 2:
        return {'sharpe': 0.0, 'max_dd': 0.0, 'total_return_pct': 0.0,
                'trade_count': trade_count, 'note': 'too few returns',
                'regime_step_counts': regime_step_counts, 'oos_days': oos_days}

    # If the strategy fired no trades, the equity curve is flat at INITIAL_CAPITAL
    # and daily_ret ≈ 0 with std → 0; the sharpe formula then blows up to ~-3M
    # driven entirely by (0 - RISK_FREE_DAILY) / 1e-9. That's noise, not signal.
    # Return 0.0 so downstream writers can recognize "no-op window" cleanly.
    if trade_count == 0 or daily_ret.std() < 1e-8:
        return {
            'sharpe': 0.0, 'max_dd': 0.0, 'total_return_pct': 0.0,
            'trade_count': trade_count,
            'note': 'no trades / flat equity',
            'regime_step_counts': regime_step_counts,
            'oos_days': oos_days,
        }

    excess = daily_ret - RISK_FREE_DAILY
    sharpe  = float(excess.mean() / (excess.std() + 1e-9) * np.sqrt(TRADING_DAYS))

    roll_max = eq.cummax()
    dd       = (eq - roll_max) / (roll_max + 1e-9)
    max_dd   = float(abs(dd.min()))

    # Total return over the OOS window (end/start - 1), reported as a percent.
    start_val = float(eq.iloc[0])
    end_val   = float(eq.iloc[-1])
    total_return_pct = ((end_val / start_val) - 1.0) * 100.0 if start_val > 0 else 0.0

    return {
        'sharpe':              round(sharpe, 4),
        'max_dd':              round(max_dd, 4),
        'total_return_pct':    round(total_return_pct, 2),
        'trade_count':         trade_count,
        'regime_step_counts':  regime_step_counts,
        'oos_days':            oos_days,
    }


def run_backtest(filepath: str) -> dict:
    """Run regime-stratified backtest. Returns full verdict dict."""
    # Step 1: validate contract first
    val = validate(filepath)
    if not val['ok']:
        return {'error': f"Contract validation failed: {'; '.join(val['errors'])}",
                'windows': [], 'regime_breakdown': {}, 'method': 'v2_regime_stratified'}

    # Step 2: load strategy class
    import importlib
    abs_path  = os.path.abspath(filepath)
    import inspect as _inspect
    from strategies.base import BaseStrategy, Signal

    def _is_strategy_class(obj):
        if not _inspect.isclass(obj) or obj.__name__ == 'BaseStrategy':
            return False
        try:
            if issubclass(obj, BaseStrategy):
                return True
        except TypeError:
            pass
        return any(b.__name__ == 'BaseStrategy' for b in obj.__mro__[1:])

    module_name = None
    if SRC_DIR in abs_path:
        rel = os.path.relpath(abs_path, SRC_DIR).replace(os.sep, '.')
        if rel.endswith('.py'):
            module_name = rel[:-3]

    if module_name:
        sys.modules.pop(module_name, None)
        try:
            module = importlib.import_module(module_name)
        except Exception as e:
            return {'error': f'Import error: {e}', 'windows': [],
                    'regime_breakdown': {}, 'method': 'v2_regime_stratified'}
    else:
        import importlib.util
        spec = importlib.util.spec_from_file_location('_bt_strat', abs_path)
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as e:
            return {'error': f'Import error: {e}', 'windows': [],
                    'regime_breakdown': {}, 'method': 'v2_regime_stratified'}

    classes = [obj for _, obj in _inspect.getmembers(module, _inspect.isclass) if _is_strategy_class(obj)]
    if not classes:
        return {'error': 'No strategy class found', 'windows': [],
                'regime_breakdown': {}, 'method': 'v2_regime_stratified'}
    strategy_cls = classes[0]

    # Step 3: load prices once
    try:
        prices = _load_prices()
    except Exception as e:
        return {'error': f'Failed to load prices: {e}', 'windows': [],
                'regime_breakdown': {}, 'method': 'v2_regime_stratified'}

    # Step 4: pick regime windows for this strategy. The plan honours the
    # strategy's `active_in_regimes` declaration — we only run OOS windows
    # whose label is in that list. Windows are picked longest-first per
    # regime, capped at WINDOWS_PER_REGIME.
    declared = list(getattr(strategy_cls, 'active_in_regimes', None) or [])
    if not declared:
        # Defensive: if a strategy didn't normalize active_in_regimes for
        # any reason, default to the canonical-three (matches base.py
        # default and preserves prior behavior on weird subclasses).
        declared = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    plan = regime_windows_for_strategy(declared)
    vix_lookup = _build_vix_lookup()

    # Step 5: run each planned window. Per-regime breakdown is built from
    # the labelled windows; a strategy declaring CRISIS-only with no
    # historical CRISIS span still produces a clean response (no error)
    # so the operator sees the gap explicitly in the breakdown.
    window_results: list[dict] = []
    for w in plan:
        try:
            res = _run_regime_window(
                strategy_cls, prices,
                w['train_start'], w['oos_start'], w['oos_end'],
                vix_lookup,
            )
        except Exception:
            res = {'sharpe': 0.0, 'max_dd': 0.0, 'trade_count': 0,
                   'total_return_pct': 0.0,
                   'error': traceback.format_exc()}
        res['window'] = f'{w["oos_start"]}–{w["oos_end"]}'
        res['regime'] = w['regime']
        window_results.append(res)

    # Build per-regime breakdown. Categories:
    #   1. Regime declared and at least one window succeeded → metrics.
    #   2. Regime declared but no eligible historical window → no_oos_window.
    #   3. Regime not declared by the strategy → not_declared.
    regime_breakdown: dict[str, dict] = {}
    for r in CANONICAL_REGIMES:
        if r not in declared:
            regime_breakdown[r] = {'note': 'not_declared'}
            continue
        regime_wins = [w for w in window_results if w.get('regime') == r]
        if not regime_wins:
            regime_breakdown[r] = {'note': 'no_oos_window'}
            continue
        # Trade-weighted sharpe; max max_dd; compounded return.
        trades_r = [w.get('trade_count', 0) or 0 for w in regime_wins]
        total_trades = int(sum(trades_r))
        if total_trades > 0:
            tw_sharpe = sum((w.get('sharpe', 0.0) or 0.0) * t for w, t in zip(regime_wins, trades_r)) / total_trades
        else:
            # Fallback to simple mean when no trades fired in any window
            # (still meaningful because some strategies' sharpe ≈ 0 with no trades).
            tw_sharpe = float(np.mean([w.get('sharpe', 0.0) or 0.0 for w in regime_wins]))
        max_dd_r = float(max(w.get('max_dd', 0.0) or 0.0 for w in regime_wins))
        # Compounded total return: ∏(1 + r/100) - 1.
        comp = 1.0
        for w in regime_wins:
            comp *= (1.0 + (w.get('total_return_pct', 0.0) or 0.0) / 100.0)
        total_return_r = (comp - 1.0) * 100.0
        oos_days_r = int(sum(w.get('oos_days', 0) or 0 for w in regime_wins))
        regime_breakdown[r] = {
            'sharpe':           round(float(tw_sharpe), 4),
            'max_dd':           round(max_dd_r, 4),
            'total_return_pct': round(float(total_return_r), 2),
            'trade_count':      total_trades,
            'oos_days':         oos_days_r,
            'window_count':     len(regime_wins),
        }

    # Aggregate across all windows (strategy-level scorecard).
    if window_results:
        agg_trades  = int(sum((w.get('trade_count', 0) or 0) for w in window_results))
        if agg_trades > 0:
            agg_sharpe = sum((w.get('sharpe', 0.0) or 0.0) * (w.get('trade_count', 0) or 0)
                             for w in window_results) / agg_trades
        else:
            agg_sharpe = float(np.mean([w.get('sharpe', 0.0) or 0.0 for w in window_results]))
        agg_max_dd = float(max((w.get('max_dd', 0.0) or 0.0) for w in window_results))
        comp_all = 1.0
        for w in window_results:
            comp_all *= (1.0 + (w.get('total_return_pct', 0.0) or 0.0) / 100.0)
        agg_return = (comp_all - 1.0) * 100.0
    else:
        # No windows planned (strategy declared a regime with no historical
        # span). Not an error — return empty metrics + clear breakdown.
        agg_trades = 0
        agg_sharpe = 0.0
        agg_max_dd = 0.0
        agg_return = 0.0

    return {
        'sharpe':                  round(float(agg_sharpe), 4),
        'max_dd':                  round(agg_max_dd, 4),
        'total_return_pct':        round(float(agg_return), 2),
        'trade_count':             agg_trades,
        'regime_breakdown':        regime_breakdown,
        'windows':                 window_results,
        'method':                  'v2_regime_stratified',
        'declared_regimes':        declared,
        'error':                   None,
    }


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: python3 auto_backtest.py <path/to/strategy.py>', file=sys.stderr)
        sys.exit(1)

    result = run_backtest(sys.argv[1])
    print(json.dumps(result, indent=2))
    # Exit 0 if metrics were produced. Exit 1 only when the strategy couldn't
    # execute at all (error field set by one of the hard-fail return paths).
    sys.exit(1 if result.get('error') else 0)
