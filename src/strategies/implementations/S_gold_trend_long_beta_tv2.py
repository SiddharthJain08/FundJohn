"""S_gold_trend_long_beta — Gold 200-day SMA trend filter (falsification study).

Source: https://kruegeralgorithms.com/en/research/gold-trend-following-is-long-beta
"Gold Trend Followers Are Long Beta: Profits Only in Rising Phases and Almost
Only Long, in None More Than Buy and Hold" (Krueger, 2026).

Thesis: the paper decomposes simple gold trend rules (e.g. 200-day SMA) across
four market phases since 2005 and shows the resulting equity curve tracks
buy-and-hold almost exactly, because the rule is long gold ~91% of the time.
The "edge" is long-beta exposure to gold, not genuine trend-timing skill.
This is a negative-result replication, implemented as written so production
data can confirm or refute the finding — not because we expect net alpha
over buy-and-hold.

Interpretation variant 2 of 2 (the 200-day SMA rule itself —
"IF gold_price > SMA(gold_price, 200) THEN LONG ELSE FLAT" — is unambiguous;
the same four implementation details that variant 1
(S_gold_trend_long_beta_tv1) left to deliberate, non-standard choices are
instead resolved here with the repo's conventional defaults, to give a
genuinely different, equally defensible reading:

  - Instrument: GLD, not IAU. GLD is the larger, more liquid gold ETP (far
    higher AUM and daily volume) and the instrument most commonly cited as
    "the" gold ETF benchmark in practitioner and academic usage, including
    the kind of buy-and-hold comparator this paper is built around. Its
    higher expense ratio (0.40% vs IAU's 0.25%) is a cost both the trend
    rule and the buy-and-hold leg pay equally, so it doesn't bias the
    decomposition either way — it's simply the more conventional proxy.
  - Persistent trend-state, not an event trigger: this variant re-emits a
    signal every bar — LONG on every bar price is above its 200-day SMA,
    FLAT on every bar it is below — re-reading "IF...THEN...ELSE" as the
    literal per-bar conditional it is written as, matching how the repo's
    other trend filters (e.g. the tv2 golden-cross convention referenced in
    variant 1) already treat persistent conditions. Variant 1 took the
    event-trigger-only reading instead; both are defensible, but this one
    is the "typical trend-follower" default.
  - Regime scope: LOW_VOL + TRANSITIONING only — the standard scoping for a
    trend/momentum strategy per repo convention — rather than all four
    canonical regimes. Variant 1 argued gold's flight-to-safety behavior
    justifies running through HIGH_VOL/CRISIS too; this variant takes the
    more conservative, conventional stance that a long-only trend filter
    should sit out the regimes where whipsaw risk (and the live book's
    existing CRISIS-regime hedges) are already elevated.
  - ATR stop at the repo default 2x (not variant 1's widened 3x): the stop
    is a volatility backstop, not the primary exit (the SMA cross still
    governs the economic exposure via the daily re-emission above); there
    is no reason implied by the paper to deviate from the standard
    multiplier used elsewhere in the strategy suite.
"""
from __future__ import annotations

import sys
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal

__all__ = ['GoldTrendLongBetaTV2']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID      = 'S_gold_trend_long_beta'
TICKER           = 'GLD'

SMA_PERIOD = 200
BASE_SIZE  = 0.08
ATR_MULT   = 2.0


class GoldTrendLongBetaTV2(BaseStrategy):
    """200-day SMA persistent trend filter on GLD (gold).

    LONG on every bar price is above its 200-day SMA; FLAT on every bar it
    is below (persistent per-bar state, re-emitted daily — not an event
    trigger). Source paper: the resulting equity curve is expected to track
    buy-and-hold almost exactly, since the rule is long gold ~91% of the
    time — this is a falsification replication, not an alpha claim.
    """

    id                = STRATEGY_ID
    name              = 'GoldTrendLongBetaTV2'
    description       = (
        '200-day SMA persistent trend filter on GLD (gold): LONG every bar '
        'above the SMA, FLAT every bar below. Per Krueger (2026), expected '
        'to track buy-and-hold since the rule is long gold ~91% of the time '
        '(variant 2 of 2).'
    )
    tier              = 3
    min_lookback      = SMA_PERIOD + 1
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    instrument_class  = INSTRUMENT_CLASS
    MAX_SIGNALS       = 1

    def default_parameters(self) -> dict:
        return {'sma_period': SMA_PERIOD, 'base_size': BASE_SIZE}

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []

        if TICKER not in prices.columns:
            print('[debug] signals=0', file=sys.stderr)
            return []

        sma_period = int(self.parameters.get('sma_period', SMA_PERIOD))
        series = prices[TICKER].dropna()
        if len(series) < sma_period + 1:
            print('[debug] signals=0', file=sys.stderr)
            return []

        sma = series.rolling(sma_period).mean()
        if pd.isna(sma.iloc[-1]):
            print('[debug] signals=0', file=sys.stderr)
            return []

        is_above      = bool(series.iloc[-1] > sma.iloc[-1])
        current_price = float(series.iloc[-1])
        base_size     = float(self.parameters.get('base_size', BASE_SIZE))
        scale         = self.position_scale(regime_state)
        spread_pct    = (current_price - float(sma.iloc[-1])) / current_price

        signals: List[Signal] = []
        if is_above:
            # Persistent state: re-emit LONG every bar the condition holds.
            pos_size = round(min(base_size * scale, 1.0), 4)
            confidence = 'HIGH' if abs(spread_pct) > 0.03 else 'MED'
            stops = self.compute_stops_and_targets(
                series, direction='LONG', current_price=current_price,
                atr_multiplier=ATR_MULT, regime_state=regime_state,
            )
            signals.append(Signal(
                ticker            = TICKER,
                direction         = 'LONG',
                entry_price       = current_price,
                stop_loss         = float(stops['stop']),
                target_1          = float(stops['t1']),
                target_2          = float(stops['t2']),
                target_3          = float(stops['t3']),
                position_size_pct = pos_size,
                confidence        = confidence,
                signal_params     = {
                    'sma_period':  sma_period,
                    'trend_state': 'above_sma',
                    'spread_pct':  round(spread_pct, 4),
                    'regime':      regime_state,
                },
            ))
        else:
            # Persistent state: re-emit FLAT every bar the condition holds.
            signals.append(Signal(
                ticker            = TICKER,
                direction         = 'FLAT',
                entry_price       = current_price,
                stop_loss         = current_price,
                target_1          = current_price,
                target_2          = current_price,
                target_3          = current_price,
                position_size_pct = 0.0,
                confidence        = 'LOW',
                signal_params     = {
                    'sma_period':  sma_period,
                    'trend_state': 'below_sma',
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
        if TICKER in wide.columns:
            s = wide[TICKER].dropna()
            if len(s) >= SMA_PERIOD + 10:
                arr   = s.values.astype(float)
                dates = s.index
                sma   = s.rolling(SMA_PERIOD).mean().values

                in_position = False
                entry_idx   = None
                for idx in range(SMA_PERIOD, len(arr)):
                    if pd.isna(sma[idx]):
                        continue
                    above = arr[idx] > sma[idx]

                    if in_position and not above:
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

                    if not in_position and above:
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
