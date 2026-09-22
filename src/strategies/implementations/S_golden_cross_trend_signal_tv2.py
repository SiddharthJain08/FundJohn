"""S_golden_cross_trend_signal — Golden Cross / Death Cross trend-state signal.

Source: https://therefutation.com/essays/does-the-golden-cross-work/ (2026) —
"Did the golden cross lose its shine?"

Thesis: the paper re-tests the classic golden cross (SMA50 crosses above
SMA200) trend signal out-of-sample across six timeframes and six market
regimes and finds it does NOT show a persistent edge. This is explicitly a
negative-result replication, not a new-alpha claim — we implement it as
written so production data can confirm or refute the finding, not because we
expect it to pass the promotion gate.

Interpretation variant 2 of 2 (the spec is unambiguous on the 50/200 rule
itself — signal_formula_pseudocode names SMA_50/SMA_200 explicitly — but
silent on several implementation details; where silent this variant
deliberately makes the OPPOSITE defensible choice from variant 1 on every
axis variant 1 flagged as an open interpretive fork):

  - Persistent trend-state, not event trigger: variant 1 fires only on the
    single bar the SMA50/SMA200 spread changes sign (a strict "crosses"
    reading). This variant instead re-checks "is SMA50 currently above
    SMA200" every cycle and holds/re-emits the signal for as long as the
    stacking persists — the convention already used by comparable
    trend-followers in this repo (S_ma_tsmom_crossover,
    S_kalman_laguerre_trend_crossover). A reader of "golden cross" as a
    TREND FILTER rather than a one-shot EVENT is at least as defensible,
    and it is the reading that actually produces the six-regime coverage
    the source paper is testing (an event-only reading starves most
    regimes of trades entirely).
  - Death cross -> FLAT exit only, not SHORT: direction_vocab in the spec
    lists both LONG and SHORT, but golden-cross literature (including the
    refutation essay itself) frames the death cross as a "get out" signal
    for a long-only trend filter, not a standalone short thesis. This
    variant treats SMA50 < SMA200 as "flat" (no position), never opening a
    symmetric short — the more conservative, textbook-trend-following
    reading.
  - Regime scope: regime_applicability was left empty in the spec. Rather
    than register across all four canonical regimes (variant 1's choice),
    this variant restricts eligibility to LOW_VOL and TRANSITIONING only —
    the standard scope for a slow trend-following filter, which is known
    to whipsaw and underperform in HIGH_VOL/CRISIS chop. This is the
    "typical trend-follower" convention variant 1 explicitly said it was
    deliberately avoiding.
  - MA windows: 50/200 trading days exactly as given — not varied, even
    though the source paper itself sweeps six timeframe pairs. Unambiguous
    in the spec, so both variants agree here.
"""
from __future__ import annotations

import sys
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal

__all__ = ['GoldenCrossTrendStateTV2']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_golden_cross_trend_signal'

SMA_FAST   = 50
SMA_SLOW   = 200
BASE_SIZE  = 0.05
MAX_WEIGHT = 0.08


class GoldenCrossTrendStateTV2(BaseStrategy):
    """LONG whenever SMA50 is stacked above SMA200 (persistent trend state);
    FLAT whenever SMA50 is at or below SMA200 — no symmetric short leg."""

    id                = STRATEGY_ID
    name              = 'GoldenCrossTrendStateTV2'
    description       = (
        'Golden/death cross (SMA50 x SMA200) persistent trend-state filter, '
        'long-only, LOW_VOL/TRANSITIONING scope, per the "Did the golden '
        'cross lose its shine?" (2026) OOS teardown (variant 2 of 2).'
    )
    tier              = 3
    min_lookback      = 504
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    instrument_class  = INSTRUMENT_CLASS

    def default_parameters(self) -> dict:
        return {'base_size': BASE_SIZE}

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
        scale     = self.position_scale(regime_state)
        base_size = float(self.parameters.get('base_size', BASE_SIZE))

        valid = [t for t in universe if t in prices.columns]
        if not valid or len(prices) < SMA_SLOW + 1:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        bullish = []
        for ticker in valid:
            series = prices[ticker].dropna()
            if len(series) < SMA_SLOW:
                continue

            sma_fast = series.rolling(SMA_FAST).mean()
            sma_slow = series.rolling(SMA_SLOW).mean()
            if pd.isna(sma_fast.iloc[-1]) or pd.isna(sma_slow.iloc[-1]):
                continue

            # Persistent state check, not an event: stacked bullish today?
            if sma_fast.iloc[-1] <= sma_slow.iloc[-1]:
                continue  # death-cross state -> flat, no short leg

            current_price = float(series.iloc[-1])
            if current_price <= 0:
                continue

            spread_pct = float(sma_fast.iloc[-1] - sma_slow.iloc[-1]) / current_price
            bullish.append((ticker, spread_pct, series, current_price))

        if not bullish:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        bullish.sort(key=lambda x: x[1], reverse=True)
        bullish = bullish[:self.MAX_SIGNALS]
        pos_size = round(min(base_size * scale, MAX_WEIGHT), 4)

        signals: List[Signal] = []
        for ticker, spread_pct, series, current_price in bullish:
            if spread_pct > 0.03:
                confidence = 'HIGH'
            elif spread_pct > 0.01:
                confidence = 'MED'
            else:
                confidence = 'LOW'

            st = self.compute_stops_and_targets(
                series, direction='LONG', current_price=current_price,
                regime_state=regime_state,
            )

            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=current_price,
                stop_loss=float(st['stop']),
                target_1=float(st['t1']),
                target_2=float(st['t2']),
                target_3=float(st['t3']),
                position_size_pct=pos_size,
                confidence=confidence,
                signal_params={
                    'sma_fast':    SMA_FAST,
                    'sma_slow':    SMA_SLOW,
                    'trend_state': 'bullish_stack',
                    'spread_pct':  round(spread_pct, 4),
                    'regime':      regime_state,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


# ── Regime-partitioned backtest ───────────────────────────────────────────────
if __name__ == '__main__':
    import json
    import os

    ROOT = os.environ.get('OPENCLAW_PARQUET_ROOT', '/root/openclaw/data/master')
    try:
        long_df = pd.read_parquet(os.path.join(ROOT, 'prices.parquet'))
        wide    = long_df.pivot_table(index='date', columns='ticker', values='close')
        wide.index = pd.to_datetime(wide.index)
        wide = wide.sort_index().loc['2017-01-01':'2025-12-31']

        reg_df = pd.read_parquet(os.path.join(ROOT, 'historical_regimes.parquet'))
        reg_df['date'] = pd.to_datetime(reg_df['date'])
        regime_map = dict(zip(reg_df['date'], reg_df['regime']))

        rows = []
        for ticker in wide.columns:
            s = wide[ticker].dropna()
            if len(s) < SMA_SLOW + 10:
                continue
            arr   = s.values.astype(float)
            dates = s.index
            sma_fast = s.rolling(SMA_FAST).mean().values
            sma_slow = s.rolling(SMA_SLOW).mean().values

            in_position = False
            entry_idx   = None
            for idx in range(SMA_SLOW, len(arr)):
                if pd.isna(sma_fast[idx]) or pd.isna(sma_slow[idx]):
                    continue
                bullish_state = sma_fast[idx] > sma_slow[idx]

                if in_position and not bullish_state:
                    # trend-state flip to flat closes the open long
                    exit_price  = arr[idx]
                    entry_price = arr[entry_idx]
                    raw_ret = (exit_price - entry_price) / entry_price
                    sig_date = dates[entry_idx]
                    rows.append({
                        'strategy_id':  STRATEGY_ID,
                        'signal_date':  str(sig_date.date()),
                        'regime_state': regime_map.get(sig_date, 'LOW_VOL'),
                        'pnl':          float(raw_ret),
                        'r_multiple':   float(raw_ret / 0.05),
                    })
                    in_position = False

                if not in_position and bullish_state:
                    in_position = True
                    entry_idx = idx

        trades_df = pd.DataFrame(rows)
        print(f'[backtest] total trades: {len(trades_df)}', file=sys.stderr)

        sys.path.insert(0, '/root/openclaw/src')
        from backtest.quick_backtest import run_backtest_with_regime_partition
        result = run_backtest_with_regime_partition(
            trades_df,
            strategy_id=STRATEGY_ID,
            thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
        )
        print(json.dumps(result, indent=2, default=str))
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f'[backtest] error: {e}', file=sys.stderr)
        sys.exit(1)
