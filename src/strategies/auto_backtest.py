"""
auto_backtest.py — Portfolio-level walk-forward backtest for generated strategies.

Usage:
    python3 src/strategies/auto_backtest.py src/strategies/implementations/S_xx_foo.py

Exit code 0 = passed gate. Exit code 1 = failed gate.
Prints JSON: {"passed": bool, "sharpe": float, "max_dd": float, "trade_count": int,
              "windows": [...], "error": null | str}

Gate criteria (all must pass in ≥ 2 of 3 convergence windows):
    Sharpe >= 0.5
    Max drawdown <= 40%
    Trade count >= 20
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

# ── Gate thresholds ────────────────────────────────────────────────────────────
MIN_SHARPE      = 0.50
MAX_DRAWDOWN    = 0.40   # absolute value
MIN_TRADES      = 20
TRADING_DAYS    = 252
RISK_FREE_DAILY = 0.05 / TRADING_DAYS

# ── Walk-forward windows (train_end, oos_start, oos_end) ──────────────────────
# Strategy receives prices up to current date; OOS period determines scoring.
# Windows are tuned to match actual data availability:
#   prices.parquet covers 2016+;
#   options_aggregates covers 2024-04-22 → 2026-04-20 (Massive Starter plan).
# To give options-dependent strategies 3 non-overlapping OOS windows inside
# that ~2-year coverage, we use 8-month OOS slices (each ≥ REBALANCE_FREQ × 8).
# Price-only strategies still see the full training runway (2016+).
CONVERGENCE_WINDOWS = [
    ('2022-01-01', '2024-06-01', '2024-12-31'),   # window A — options coverage + iv_rank warmup
    ('2022-01-01', '2025-01-01', '2025-08-31'),   # window B
    ('2022-01-01', '2025-09-01', '2026-04-15'),   # window C
]
# With REBALANCE_FREQ=21 trading days and 8-month OOS, we get ~8 rebalance
# opportunities per window — MIN_TRADES=20 stays achievable with active strategies.

REBALANCE_FREQ   = 21   # trading-day step between signal generation calls
MAX_HOLD_DAYS    = 21   # max days in any position
INITIAL_CAPITAL  = 1_000_000.0
MAX_POSITION_PCT = 0.05  # cap per-position at 5% of portfolio


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_prices() -> pd.DataFrame:
    """Load prices.parquet and pivot to wide format (date index, ticker columns)."""
    long = pd.read_parquet(os.path.join(ROOT, 'data', 'master', 'prices.parquet'))
    wide = long.pivot_table(index='date', columns='ticker', values='close')
    wide.index = pd.to_datetime(wide.index)
    wide.sort_index(inplace=True)
    return wide


def _dummy_regime(state='TRANSITIONING') -> dict:
    return {
        'state': state,
        'state_probabilities': {state: 1.0, 'LOW_VOL': 1.0 if state == 'LOW_VOL' else 0.0,
                                 'TRANSITIONING': 0.0, 'HIGH_VOL': 0.0, 'CRISIS': 0.0},
        'confidence': 1.0,
        'transition_probs_tomorrow': {'LOW_VOL': 0.9, 'TRANSITIONING': 0.1, 'HIGH_VOL': 0.0, 'CRISIS': 0.0},
        'stress_score': 15,
        'roro_score': 40.0,
        'features': {'vix': 14.0, 'vix_5d_chg': -0.5, 'vix_term_slope': 1.2,
                     'spx_rv_20d': 10.0, 'hy_ig_spread': 0.01, 'spx_5d_return': 0.02},
        'regime_change_alert': False,
        'days_in_current_state': 20,
        'position_scale': 1.0,
    }


def _run_window(strategy_cls, prices: pd.DataFrame, train_start: str, oos_start: str, oos_end: str) -> dict:
    """
    Run one OOS window. Returns {sharpe, max_dd, trade_count, passed}.

    The strategy can see all prices from train_start up to the current rebalance date
    (point-in-time safe). We score only on the OOS period.
    """
    universe = [c for c in prices.columns if not c.startswith('^') and '-USD' not in c and '=F' not in c]

    oos_start_dt = pd.Timestamp(oos_start)
    oos_end_dt   = pd.Timestamp(oos_end)
    train_start_dt = pd.Timestamp(train_start)

    # OOS trading days
    oos_dates = prices.loc[oos_start_dt:oos_end_dt].index
    if len(oos_dates) == 0:
        return {'sharpe': 0.0, 'max_dd': 0.0, 'trade_count': 0, 'passed': False, 'note': 'no OOS dates'}

    # Portfolio state
    cash         = INITIAL_CAPITAL
    positions    = {}   # {(ticker, open_date): {shares, stop, target, hold_until}}
    equity_curve = {}   # {date: portfolio_value}
    trade_count  = 0

    instance = strategy_cls()
    # Use the strategy's first active regime so HV strategies (which gate on
    # active_in_regimes) don't trivially exit. Price-only strategies default
    # to TRANSITIONING which is in every strategy's active list.
    regime_state = 'TRANSITIONING'
    active = getattr(instance, 'active_in_regimes', None)
    if active and isinstance(active, (list, tuple)) and active:
        # Prefer TRANSITIONING if the strategy allows it; else first.
        regime_state = 'TRANSITIONING' if 'TRANSITIONING' in active else active[0]
    regime = _dummy_regime(regime_state)

    # Step through OOS at rebalance frequency
    step_indices = list(range(0, len(oos_dates), REBALANCE_FREQ))
    if step_indices[-1] != len(oos_dates) - 1:
        step_indices.append(len(oos_dates) - 1)

    for idx in step_indices:
        current_date = oos_dates[idx]

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
    if len(equity_curve) < 5:
        return {'sharpe': 0.0, 'max_dd': 0.0, 'total_return_pct': 0.0, 'trade_count': trade_count, 'passed': False, 'note': 'too few equity points'}

    eq = pd.Series(equity_curve).sort_index()
    daily_ret = eq.pct_change().dropna()

    if len(daily_ret) < 2:
        return {'sharpe': 0.0, 'max_dd': 0.0, 'total_return_pct': 0.0, 'trade_count': trade_count, 'passed': False, 'note': 'too few returns'}

    # If the strategy fired no trades, the equity curve is flat at INITIAL_CAPITAL
    # and daily_ret ≈ 0 with std → 0; the sharpe formula then blows up to ~-3M
    # driven entirely by (0 - RISK_FREE_DAILY) / 1e-9. That's noise, not signal.
    # Return 0.0 so downstream writers can recognize "no-op window" cleanly.
    if trade_count == 0 or daily_ret.std() < 1e-8:
        return {
            'sharpe': 0.0, 'max_dd': 0.0, 'total_return_pct': 0.0,
            'trade_count': trade_count, 'passed': False,
            'note': 'no trades / flat equity',
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

    passed = (
        sharpe  >= MIN_SHARPE and
        max_dd  <= MAX_DRAWDOWN and
        trade_count >= MIN_TRADES
    )

    return {
        'sharpe':           round(sharpe, 4),
        'max_dd':           round(max_dd, 4),
        'total_return_pct': round(total_return_pct, 2),
        'trade_count':      trade_count,
        'passed':           passed,
    }


def run_backtest(filepath: str) -> dict:
    """Run 3-window convergence gate. Returns full verdict dict."""
    # Step 1: validate contract first
    val = validate(filepath)
    if not val['ok']:
        return {'passed': False, 'error': f"Contract validation failed: {'; '.join(val['errors'])}", 'windows': []}

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
            return {'passed': False, 'error': f'Import error: {e}', 'windows': []}
    else:
        import importlib.util
        spec = importlib.util.spec_from_file_location('_bt_strat', abs_path)
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as e:
            return {'passed': False, 'error': f'Import error: {e}', 'windows': []}

    classes = [obj for _, obj in _inspect.getmembers(module, _inspect.isclass) if _is_strategy_class(obj)]
    if not classes:
        return {'passed': False, 'error': 'No strategy class found', 'windows': []}
    strategy_cls = classes[0]

    # Step 3: load prices once
    try:
        prices = _load_prices()
    except Exception as e:
        return {'passed': False, 'error': f'Failed to load prices: {e}', 'windows': []}

    # Step 4: run 3 convergence windows
    window_results = []
    for train_start, oos_start, oos_end in CONVERGENCE_WINDOWS:
        try:
            res = _run_window(strategy_cls, prices, train_start, oos_start, oos_end)
        except Exception as e:
            res = {'sharpe': 0.0, 'max_dd': 0.0, 'trade_count': 0, 'passed': False,
                   'error': traceback.format_exc()}
        res['window'] = f'{oos_start}–{oos_end}'
        window_results.append(res)

    windows_passed = sum(1 for w in window_results if w.get('passed'))
    overall_passed = windows_passed >= 2

    # Aggregate metrics (average across windows)
    sharpes  = [w['sharpe']             for w in window_results]
    max_dds  = [w['max_dd']             for w in window_results]
    returns  = [w.get('total_return_pct', 0.0) for w in window_results]
    trades   = [w['trade_count']        for w in window_results]

    return {
        'passed':           overall_passed,
        'windows_passed':   windows_passed,
        'sharpe':           round(float(np.mean(sharpes)), 4),
        'max_dd':           round(float(np.mean(max_dds)),  4),
        'total_return_pct': round(float(np.mean(returns)),  2),
        'trade_count':      int(np.sum(trades)),
        'windows':          window_results,
        'error':       None,
    }


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: python3 auto_backtest.py <path/to/strategy.py>', file=sys.stderr)
        sys.exit(1)

    result = run_backtest(sys.argv[1])
    print(json.dumps(result, indent=2))
    sys.exit(0 if result['passed'] else 1)
