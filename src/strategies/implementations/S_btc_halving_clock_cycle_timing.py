"""
BTC Halving Clock Cycle Timing (Molnar 2026)
Source: http://arxiv.org/abs/2607.26188v1

Calendar LONG/FLAT rotation on BTC-USD anchored to halving epochs.
All price-level oscillators (Pi Cycle, MVRV, Mayer, Puell) suffer monotonic
threshold drift across epochs; the halving-anchored time signal is the only
cross-epoch stable predictor (top-cluster p~5e-6, 10k block-bootstrap paths).
"""
from __future__ import annotations

import sys
from datetime import date
from typing import List

import pandas as pd

from strategies.base import BaseStrategy, Signal

__all__ = ['BtcHalvingClockCycleTiming']

INSTRUMENT_CLASS = 'crypto'
STRATEGY_ID      = 'S_btc_halving_clock_cycle_timing'

GENESIS_DATE  = date(2009, 1, 3)
HALVING_DATES = [
    date(2012, 11, 28),
    date(2016,  7,  9),
    date(2020,  5, 11),
    date(2024,  4, 19),
]
# Known historical cycle tops, index-aligned with HALVING_DATES (current cycle TBD)
CYCLE_TOPS = [
    date(2013, 12,  4),   # 371 d after 2012-11-28
    date(2017, 12, 17),   # 526 d after 2016-07-09
    date(2021, 11,  8),   # 547 d after 2020-05-11
]

BASE_SIZE = 0.08   # 8 % gross when LONG; scaled by regime in generate_signals


def _halving_signal(as_of: date) -> str:
    """Core timing rule from Molnar 2026 §empirical timing section."""
    past = [h for h in HALVING_DATES if h <= as_of]
    if not past:
        return 'FLAT'
    lh  = max(past)
    dsh = (as_of - lh).days
    idx = HALVING_DATES.index(lh)
    top = CYCLE_TOPS[idx] if idx < len(CYCLE_TOPS) else None
    dst = (as_of - top).days if top is not None else None
    if dsh < 480:
        return 'LONG'
    if 525 <= dsh <= 546:
        return 'FLAT'
    if dst is not None and dst >= 366:
        return 'LONG'
    return 'FLAT'


class BtcHalvingClockCycleTiming(BaseStrategy):
    """
    Halving-clock cycle timing on BTC-USD (Molnar 2026).
    LONG: days_since_halving < 480 (early-to-mid cycle).
    FLAT: 525–546 d post-halving (empirical top window).
    LONG re-entry: >= 366 d after confirmed cycle top (post-bottom).
    """

    id                = STRATEGY_ID
    name              = 'BTC Halving Clock Cycle Timing'
    description       = (
        'Calendar LONG/FLAT on BTC-USD anchored to halving epochs; '
        'top-cluster p~5e-6 across 4 epochs (Molnar 2026).'
    )
    tier              = 2
    signal_frequency  = 'daily'
    calendar_edge     = True
    min_lookback      = 1
    instrument_class  = INSTRUMENT_CLASS
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = 1

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        if 'BTC-USD' not in prices.columns:
            print(f'[{STRATEGY_ID}] BTC-USD not in prices; signals=0', file=sys.stderr)
            return []
        series = prices['BTC-USD'].dropna()
        if series.empty:
            print(f'[{STRATEGY_ID}] signals=0 (empty series)', file=sys.stderr)
            return []

        last_idx = series.index[-1]
        as_of = last_idx.date() if hasattr(last_idx, 'date') else date.fromisoformat(str(last_idx)[:10])

        direction = _halving_signal(as_of)
        if direction != 'LONG':
            print(f'[{STRATEGY_ID}] signals=0 direction=FLAT as_of={as_of}', file=sys.stderr)
            return []

        current_price = float(series.iloc[-1])
        scale    = self.position_scale(regime_state)
        pos_size = round(BASE_SIZE * scale, 4)

        st = self.compute_stops_and_targets(
            series, direction='LONG', current_price=current_price,
            regime_state=regime_state,
        )

        past = [h for h in HALVING_DATES if h <= as_of]
        dsh  = (as_of - max(past)).days
        dsg  = (as_of - GENESIS_DATE).days
        power_law = float(dsg ** 1.5)   # proxy for secular uptrend magnitude (log-scaled display)
        confidence = 'HIGH' if dsh < 300 else ('MED' if dsh < 450 else 'LOW')

        sig = Signal(
            ticker            = 'BTC-USD',
            direction         = 'LONG',
            entry_price       = current_price,
            stop_loss         = float(st['stop']),
            target_1          = float(st['t1']),
            target_2          = float(st['t2']),
            target_3          = float(st['t3']),
            position_size_pct = pos_size,
            confidence        = confidence,
            signal_params     = {
                'days_since_halving': dsh,
                'days_since_genesis': dsg,
                'power_law_proxy':    round(power_law, 2),
                'as_of':              str(as_of),
                'regime':             regime_state,
            },
        )
        print(
            f'[{STRATEGY_ID}] signals=1 dsh={dsh} dsg={dsg} '
            f'price={current_price:.2f} regime={regime_state}',
            file=sys.stderr,
        )
        return [sig]


if __name__ == '__main__':
    # Regime-partitioned backtest skeleton (lifecycle promotion gate requirement).
    # Run: python3 src/strategies/implementations/S_btc_halving_clock_cycle_timing.py
    import os, json
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

    PARQUET_ROOT = os.environ.get('OPENCLAW_PARQUET_ROOT', '/root/openclaw/data/master')

    prices_s = pd.read_parquet(f'{PARQUET_ROOT}/prices.parquet', columns=['BTC-USD'])['BTC-USD'].dropna()
    reg_df   = pd.read_parquet(f'{PARQUET_ROOT}/historical_regimes.parquet')

    def _lookup_regime(dt):
        mask = reg_df.index <= dt
        return str(reg_df.loc[mask, 'state'].iloc[-1]) if mask.any() else 'LOW_VOL'

    rows, in_trade, ep, ed, er = [], False, None, None, None
    for dt, price in prices_s.items():
        as_of = dt.date() if hasattr(dt, 'date') else date.fromisoformat(str(dt)[:10])
        sig   = _halving_signal(as_of)
        reg   = _lookup_regime(dt)
        if sig == 'LONG' and not in_trade:
            in_trade, ep, ed, er = True, float(price), as_of, reg
        elif sig != 'LONG' and in_trade:
            xp  = float(price)
            pnl = (xp - ep) / ep
            rows.append({'strategy_id': STRATEGY_ID, 'signal_date': str(ed),
                         'regime_state': er, 'pnl': pnl, 'r_multiple': pnl / 0.02})
            in_trade = False

    trades_df = pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=['strategy_id', 'signal_date', 'regime_state', 'pnl', 'r_multiple'])
    print(f'[backtest] completed_trades={len(trades_df)}', file=sys.stderr)

    from backtest.quick_backtest import run_backtest_with_regime_partition
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 3, 'min_avg_r': 0.0},
    )
    print(json.dumps(result, indent=2, default=str))
