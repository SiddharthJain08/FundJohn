"""
Stock-Bond Correlation Crash Warning
Source: https://bestfolio.app/blog/stock-bond-correlation-crash-hedge
"Can Stock-Bond Correlation Warn You Before Treasuries Fail?"

Hypothesis: rising 60-day rolling correlation between stock returns (SPY) and
bond returns (TLT) signals that Treasuries are breaking down as an equity-
selloff hedge, and warrants de-risking ahead of a fast equity drawdown.

Rule: corr_60d = rolling_correlation(SPY_ret, TLT_ret, window=60d)
      IF corr_60d > UPPER_THRESHOLD: FLAT (de-risk — hedge unreliable)
      ELSE: LONG SPY (stock-bond correlation negative/benign, hedge intact)

Source blog reports mixed results (2/5 hit rate on crash calls, frequent
false positives) — flagged `short_backtest` by the extraction pipeline, so
this is sized conservatively and gated to stress-adjacent regimes where a
correlation-breakdown warning is actually actionable.
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['StockBondCorrCrashWarning']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_stock_bond_corr_crash_warning'
STOCK_PROXY      = 'SPY'
BOND_PROXY       = 'TLT'
CORR_WINDOW      = 60
UPPER_THRESHOLD  = 0.30     # corr_60d above this ⇒ hedge warning
BASE_SIZE        = 0.10


def _corr_series(stock: pd.Series, bond: pd.Series, window: int) -> pd.Series:
    stock_ret = stock.pct_change()
    bond_ret  = bond.pct_change()
    return stock_ret.rolling(window, min_periods=window).corr(bond_ret)


class StockBondCorrCrashWarning(BaseStrategy):
    """LONG SPY when 60d stock-bond return correlation is below threshold
    (Treasuries still a viable hedge); FLAT when correlation rises above
    the threshold (hedge breakdown warning — de-risk)."""

    id                = STRATEGY_ID
    name              = 'StockBondCorrCrashWarning'
    description       = (
        'Rolling 60-day SPY/TLT return correlation: FLAT when correlation '
        'rises above threshold (Treasury-hedge breakdown warning), else LONG SPY.'
    )
    instrument_class  = INSTRUMENT_CLASS
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = 504
    active_in_regimes = ['TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = 1

    def default_parameters(self) -> dict:
        return {
            'corr_window':     CORR_WINDOW,
            'upper_threshold': UPPER_THRESHOLD,
            'base_size':       BASE_SIZE,
        }

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print(f'[debug] {STRATEGY_ID}: signals=0 (empty prices)', file=sys.stderr)
            return []

        if STOCK_PROXY not in prices.columns or BOND_PROXY not in prices.columns:
            print(f'[debug] {STRATEGY_ID}: signals=0 (SPY/TLT missing)', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print(f'[debug] {STRATEGY_ID}: signals=0 (regime={regime_state} excluded)', file=sys.stderr)
            return []

        params       = self.parameters
        corr_window  = int(params.get('corr_window', CORR_WINDOW))
        upper_thresh = float(params.get('upper_threshold', UPPER_THRESHOLD))
        min_lookback = int(self.min_lookback)

        spy = prices[STOCK_PROXY].dropna()
        tlt = prices[BOND_PROXY].dropna()
        if not isinstance(spy.index, pd.DatetimeIndex):
            spy.index = pd.to_datetime(spy.index)
        if not isinstance(tlt.index, pd.DatetimeIndex):
            tlt.index = pd.to_datetime(tlt.index)

        aligned = pd.DataFrame({'spy': spy, 'tlt': tlt}).dropna()
        if len(aligned) < min_lookback:
            print(f'[debug] {STRATEGY_ID}: signals=0 (bars={len(aligned)} < {min_lookback})',
                  file=sys.stderr)
            return []

        corr = _corr_series(aligned['spy'], aligned['tlt'], corr_window)
        if corr.empty or pd.isna(corr.iloc[-1]):
            print(f'[debug] {STRATEGY_ID}: signals=0 (corr unavailable)', file=sys.stderr)
            return []

        corr_current = float(corr.iloc[-1])
        spy_price    = float(aligned['spy'].iloc[-1])
        if spy_price <= 0:
            print(f'[debug] {STRATEGY_ID}: signals=0 (invalid SPY price)', file=sys.stderr)
            return []

        if corr_current > upper_thresh:
            # Treasury hedge warning — de-risk, stay FLAT
            print(f'[debug] {STRATEGY_ID}: signals=0 (corr={corr_current:.4f} > {upper_thresh} — FLAT)',
                  file=sys.stderr)
            return []

        scale    = self.position_scale(regime_state)
        pos_size = round(float(params.get('base_size', BASE_SIZE)) * scale, 4)
        pos_size = max(0.01, min(pos_size, 0.20))

        # Confidence scales inversely with proximity to the breakdown threshold —
        # the further corr sits below the threshold, the more intact the hedge.
        margin = upper_thresh - corr_current
        confidence = 'HIGH' if margin > 0.30 else ('MED' if margin > 0.10 else 'LOW')

        stops = self.compute_stops_and_targets(
            aligned['spy'],
            direction='LONG',
            current_price=spy_price,
            regime_state=regime_state,
        )

        signal = Signal(
            ticker            = STOCK_PROXY,
            direction         = 'LONG',
            entry_price       = spy_price,
            stop_loss         = float(stops['stop']),
            target_1          = float(stops['t1']),
            target_2          = float(stops['t2']),
            target_3          = float(stops['t3']),
            position_size_pct = pos_size,
            confidence        = confidence,
            signal_params     = {
                'corr_60d':        round(corr_current, 4),
                'upper_threshold': upper_thresh,
                'regime':          regime_state,
                'scale':           scale,
            },
        )

        print(
            f'[debug] {STRATEGY_ID}: signals=1 corr={corr_current:.4f} '
            f'thresh={upper_thresh} entry={spy_price:.2f} regime={regime_state}',
            file=sys.stderr,
        )
        return [signal]


def run_regime_backtest():
    """Offline helper: compute regime-partitioned backtest metrics for the
    lifecycle promotion gate. Loads SPY/TLT prices + historical regimes from
    master parquets, runs the correlation-threshold logic over full history,
    tags each trade day with the active regime, then calls
    run_backtest_with_regime_partition() so the gate can read
    result['eligible_regimes_proposed']."""
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
    from backtest.quick_backtest import run_backtest_with_regime_partition

    base = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'data', 'master')
    prices_path = os.path.join(base, 'prices.parquet')
    regime_path = os.path.join(base, 'historical_regimes.parquet')

    raw = pd.read_parquet(prices_path, columns=['ticker', 'date', 'close'])
    wide = raw.pivot(index='date', columns='ticker', values='close')
    wide.index = pd.to_datetime(wide.index)
    wide.sort_index(inplace=True)

    if STOCK_PROXY not in wide.columns or BOND_PROXY not in wide.columns:
        print('[backtest] SPY/TLT not in prices — aborting', file=sys.stderr)
        return None

    aligned = wide[[STOCK_PROXY, BOND_PROXY]].dropna()
    aligned.columns = ['spy', 'tlt']
    corr = _corr_series(aligned['spy'], aligned['tlt'], CORR_WINDOW)
    spy_fwd_ret = aligned['spy'].pct_change().shift(-1)

    reg = pd.read_parquet(regime_path)
    reg['date'] = pd.to_datetime(reg['date'] if 'date' in reg.columns else reg.index)
    if 'date' in reg.columns:
        reg = reg.set_index('date')
    regime_col = next((c for c in ('regime_state', 'state', 'regime') if c in reg.columns), None)

    rows = []
    for i in range(CORR_WINDOW, len(aligned) - 1):
        c = corr.iloc[i]
        if pd.isna(c):
            continue
        signal_date = aligned.index[i]
        regime_state = 'LOW_VOL'
        if regime_col and signal_date in reg.index:
            regime_state = str(reg.loc[signal_date, regime_col])
        direction = 'FLAT' if c > UPPER_THRESHOLD else 'LONG'
        pnl = float(spy_fwd_ret.iloc[i]) if direction == 'LONG' else 0.0
        rows.append({
            'strategy_id':  STRATEGY_ID,
            'signal_date':  str(signal_date.date()),
            'regime_state': regime_state,
            'direction':    direction,
            'pnl':          pnl,
            'r_multiple':   pnl / max(abs(pnl), 1e-6) if direction == 'LONG' else 0.0,
        })

    trades_df = pd.DataFrame(rows)
    if trades_df.empty:
        print('[backtest] no trades generated', file=sys.stderr)
        return None

    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(f'[backtest] eligible_regimes_proposed={result.get("eligible_regimes_proposed")}',
          file=sys.stderr)
    return result


if __name__ == '__main__':
    run_regime_backtest()
