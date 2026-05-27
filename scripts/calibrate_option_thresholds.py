"""SP-4 Phase 0 option threshold calibration (data-gathering).

Runs short-straddle VRP strategies on the SUPPORTED index/ETF underlyings
(SPY/QQQ/IWM) through the VIX-anchored synthetic options engine and reports the
Sharpe/MaxDD distribution. Also reads S_btc_momentum's latest backtest for the
crypto min_sharpe proposal. OUTPUT IS A PROPOSAL — the lifecycle.py threshold
edit is applied separately after operator sign-off. Thresholds are FRACTIONS;
engine total_max_dd_pct is a PERCENTAGE (divide by 100).
"""
from __future__ import annotations
import sys, os
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, OptionSpec
from backtest.unified_backtest import load_prices_panels, load_regimes, aggregate_metrics
from backtest import options_backtest


def _straddle_strat(underlying: str):
    class _S(BaseStrategy):
        id = f'_cal_straddle_{underlying}'; name = id; min_lookback = 30
        instrument_class = 'option'; MAX_SIGNALS = 1
        active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
        def generate_signals(self, prices, regime, universe, aux_data=None):
            if underlying not in prices.columns or len(prices) < self.min_lookback:
                return []
            if not self.should_run(regime.get('state', 'LOW_VOL')):
                return []
            s = prices[underlying].dropna()
            if len(s) < self.min_lookback or (len(s) % 21) != 0:
                return []
            S = float(s.iloc[-1])
            return [Signal(ticker=underlying, direction='SELL_VOL', entry_price=S,
                           stop_loss=S*0.9, target_1=S*1.1, target_2=0.0, target_3=0.0,
                           position_size_pct=0.05, confidence='MED',
                           option_spec=OptionSpec(underlying=underlying, structure='straddle',
                                                  hedge='delta', dte_target=30, roll_dte=7))]
    return _S()


def run():
    close_wide, bars = load_prices_panels()
    regimes = load_regimes()
    start, end = close_wide.index.min(), close_wide.index.max()
    print('=== Supported-envelope option archetype backtests (VIX-anchored) ===')
    results = []
    for ul in ['SPY', 'QQQ', 'IWM']:
        if ul not in close_wide.columns:
            print(f'{ul}: not in prices panel, skip'); continue
        inst = _straddle_strat(ul)
        sim = options_backtest.simulate(inst, close_wide, bars, regimes, start, end,
                                        strategy_id=inst.id, max_hold_days=21)
        m = aggregate_metrics(sim['trades'])
        results.append((ul, m))
        print(f"{ul:5s} short-straddle: trades={m['total_trades']:4d} sharpe={m['sharpe']} "
              f"max_dd_pct={m['max_dd_pct']} return_pct={m['return_pct']} sortino={m['sortino']} calmar={m['calmar']}")

    # crypto S_btc_momentum latest backtest
    print('\n=== crypto reference (S_btc_momentum) latest backtest ===')
    try:
        import psycopg2
        c = psycopg2.connect(os.environ['POSTGRES_URI']); cur = c.cursor()
        cur.execute("SELECT total_sharpe, total_max_dd_pct, total_return_pct FROM strategy_backtest_runs "
                    "WHERE strategy_id='S_btc_momentum' ORDER BY run_at DESC LIMIT 1")
        row = cur.fetchone(); c.close()
        print('S_btc_momentum:', row)
    except Exception as e:
        print('could not read S_btc_momentum backtest:', e)

    print('\n=== Distribution summary (for the controller to propose thresholds) ===')
    sh = [m['sharpe'] for _, m in results if m['sharpe'] is not None]
    dd = [m['max_dd_pct'] for _, m in results if m['max_dd_pct'] is not None]
    if sh:
        print(f'option sharpe: min={min(sh):.3f} max={max(sh):.3f} mean={sum(sh)/len(sh):.3f}')
        print(f'option max_dd_pct: min={min(dd):.2f} max={max(dd):.2f} (FRACTION = /100)')


if __name__ == '__main__':
    run()
