"""
Crypto Time-Series Momentum (Liu & Tsyvinski 2020, RFS)
Source: https://doi.org/10.1093/rfs/hhaa113

Variant 2/2: the paper tests TSMOM at three horizons {1w, 1m, 1y}. This
variant takes the 1-MONTH leg (21 trading days, counted in bars rather than
calendar days — the paper's formation windows are trading-day windows, and
a calendar-day offset drifts across weekends/exchange-holidays with no
upside) and restricts the universe to BTC-USD and ETH-USD only — the two
names this repo's crypto instrument-class convention names explicitly,
and the two coins with by far the deepest, cleanest daily history, so a
1-month momentum estimate is least contaminated by illiquid-altcoin noise.
Rebalance is weekly, but anchored to Friday close (end-of-week) rather than
Monday — the freshest possible read of the trailing month going into the
weekend gap, instead of trading on a stale prior-Friday close. Direction is
LONG/FLAT only, per the paper's unambiguous sign rule (it never shorts) and
this repo's crypto convention. Sizing departs from naive equal-weight: legs
are inverse-volatility weighted (risk parity) against a fixed vol-target
budget, rather than split evenly by rank bucket, so a calm coin gets more
capital than a frothy one at the same signal strength.
"""
from __future__ import annotations

import sys
from typing import List

import pandas as pd

from strategies.base import BaseStrategy, Signal

__all__ = ['CryptoTimeSeriesMomentum']

INSTRUMENT_CLASS = 'crypto'
STRATEGY_ID      = 'S_crypto_time_series_momentum'

LOOKBACK_BARS    = 21    # variant 2 pick: the paper's 1-month TSMOM leg, in trading bars
MIN_LOOKBACK     = 504   # paper's min_lookback_required (~2y of daily history) — unambiguous
VOL_WINDOW       = 21    # matches the formation window
BASE_SIZE        = 0.05  # total book budget split across legs by inverse-vol weight
VOL_TARGET       = 0.60  # annualized vol target used to scale the inverse-vol weights
TICKERS          = ['BTC-USD', 'ETH-USD']   # variant 2: the two names, not the whole panel


def _empty(msg: str) -> List[Signal]:
    print(msg, file=sys.stderr)
    print('[debug] signals=0', file=sys.stderr)
    return []


class CryptoTimeSeriesMomentum(BaseStrategy):
    """Time-series momentum on BTC-USD/ETH-USD (Liu & Tsyvinski 2020). Sign
    of trailing 1-month (21-bar) return: LONG if positive, FLAT (skip) if
    non-positive. Weekly rebalance anchored to Friday close, inverse-vol
    weighted sizing."""

    id                = STRATEGY_ID
    name              = 'Crypto Time-Series Momentum'
    description       = (
        'Sign of trailing 1-month return on BTC-USD/ETH-USD: LONG when '
        'positive, FLAT otherwise. Weekly (Friday) rebalance, inverse-vol '
        'weighted sizing (Liu & Tsyvinski 2020).'
    )
    tier              = 2
    signal_frequency  = 'weekly'
    min_lookback      = MIN_LOOKBACK
    instrument_class  = INSTRUMENT_CLASS
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = len(TICKERS)

    def default_parameters(self) -> dict:
        return {'base_size': BASE_SIZE}

    def generate_signals(
        self, prices: pd.DataFrame, regime: dict, universe: List[str], aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return _empty('[debug] no prices')
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return _empty('[debug] regime gated')

        today = prices.index[-1]
        if today.weekday() != 4:   # weekly rebalance: Fridays only (variant 2)
            return _empty('[debug] not a rebalance day')

        coins = [t for t in TICKERS if t in prices.columns]
        if not coins:
            return _empty('[debug] no crypto tickers in panel')

        scale = self.position_scale(regime_state)
        base_size = float(self.parameters.get('base_size', BASE_SIZE))

        candidates = []
        for tkr in coins:
            series = prices[tkr].dropna()
            if len(series) < max(MIN_LOOKBACK, LOOKBACK_BARS + VOL_WINDOW + 1):
                continue
            p0, p1 = float(series.iloc[-1 - LOOKBACK_BARS]), float(series.iloc[-1])
            if p0 <= 0:
                continue
            r = p1 / p0 - 1.0
            if r <= 0:
                continue   # FLAT per the paper's sign rule
            daily_rets = series.pct_change().dropna()
            vol = float(daily_rets.iloc[-VOL_WINDOW:].std()) if len(daily_rets) >= VOL_WINDOW else 0.0
            ann_vol = vol * (252 ** 0.5) if vol > 0 else 0.0
            z = r / (vol * (LOOKBACK_BARS ** 0.5)) if vol > 0 else 0.0
            inv_vol = (1.0 / ann_vol) if ann_vol > 0 else 0.0
            candidates.append((tkr, r, z, inv_vol, p1, series))

        if not candidates:
            return _empty(f'[{STRATEGY_ID}] no positive-momentum coins this week')

        inv_vol_sum = sum(c[3] for c in candidates) or 1.0

        signals = []
        for tkr, r, z, inv_vol, price, series in candidates:
            confidence = 'HIGH' if z >= 2.0 else ('MED' if z >= 0.75 else 'LOW')
            conf_mult = {'HIGH': 1.0, 'MED': 0.7, 'LOW': 0.45}[confidence]
            risk_parity_weight = (inv_vol / inv_vol_sum) if inv_vol > 0 else (1.0 / len(candidates))
            pos_size = round(base_size * risk_parity_weight * conf_mult * scale * len(candidates), 4)
            pos_size = min(pos_size, base_size)
            if pos_size < 0.001:
                continue
            st = self.compute_stops_and_targets(
                series, direction='LONG', current_price=price, regime_state=regime_state)
            signals.append(Signal(
                ticker=tkr, direction='LONG', entry_price=price,
                stop_loss=float(st['stop']), target_1=float(st['t1']),
                target_2=float(st['t2']), target_3=float(st['t3']),
                position_size_pct=pos_size, confidence=confidence,
                signal_params={
                    'r_1m': round(r, 6), 'z': round(z, 3),
                    'lookback_bars': LOOKBACK_BARS, 'regime': regime_state,
                },
            ))
        print(f'[{STRATEGY_ID}] legs={len(signals)} regime={regime_state}', file=sys.stderr)
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


if __name__ == '__main__':
    import os, json
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))
    ROOT = os.environ.get('OPENCLAW_PARQUET_ROOT', '/root/openclaw/data/master')

    long_df = pd.read_parquet(f'{ROOT}/prices.parquet', columns=['date', 'ticker', 'close'])
    long_df = long_df[long_df['ticker'].isin(TICKERS)]
    wide = (long_df.assign(date=pd.to_datetime(long_df['date']))
            .pivot(index='date', columns='ticker', values='close').sort_index())

    reg_df = pd.read_parquet(f'{ROOT}/historical_regimes.parquet')
    reg_df['date'] = pd.to_datetime(reg_df['date'])
    reg_df = reg_df.sort_values('date')

    def _lookup_regime(dt):
        mask = reg_df['date'] <= dt
        return str(reg_df.loc[mask, 'regime'].iloc[-1]) if mask.any() else 'LOW_VOL'

    rows = []
    fridays_idx = [i for i, d in enumerate(wide.index) if d.weekday() == 4]
    for i in range(len(fridays_idx) - 1):
        idx, nxt_idx = fridays_idx[i], fridays_idx[i + 1]
        dt, nxt = wide.index[idx], wide.index[nxt_idx]
        window = wide.iloc[:idx + 1]
        reg = _lookup_regime(dt)
        for tkr in TICKERS:
            if tkr not in window.columns:
                continue
            series = window[tkr].dropna()
            if len(series) < max(MIN_LOOKBACK, LOOKBACK_BARS + VOL_WINDOW + 1):
                continue
            p0, p1 = float(series.iloc[-1 - LOOKBACK_BARS]), float(series.iloc[-1])
            if p0 <= 0:
                continue
            r = p1 / p0 - 1.0
            if r <= 0:
                continue
            exit_series = wide[tkr].reindex([dt, nxt]).dropna()
            if len(exit_series) < 2:
                continue
            pnl = float(exit_series.iloc[-1] / exit_series.iloc[0] - 1.0)
            rows.append({'strategy_id': STRATEGY_ID, 'signal_date': str(dt)[:10],
                         'regime_state': reg, 'pnl': pnl, 'r_multiple': pnl / 0.07})
    trades_df = pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=['strategy_id', 'signal_date', 'regime_state', 'pnl', 'r_multiple'])
    print(f'[backtest] completed_trades={len(trades_df)}', file=sys.stderr)
    from backtest.quick_backtest import run_backtest_with_regime_partition
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(json.dumps(result, indent=2, default=str))
