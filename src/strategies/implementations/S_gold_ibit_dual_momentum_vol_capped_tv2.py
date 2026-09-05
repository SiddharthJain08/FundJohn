"""S_gold_ibit_dual_momentum_vol_capped (variant 2) — Dual Momentum Between
Gold and Bitcoin.

Source: https://aligrithm.com/dual-momentum-between-gold-and-bitcoin-two-stores-of-value/
(ali askar, 2026, "6.56 Dual Momentum Between Gold and Bitcoin (Two Stores of Value)")

Thesis: GLD and IBIT are both "store of value" hedges against currency
debasement. Ranking them by recent momentum and rotating into whichever is
currently stronger captures trend persistence in the leading hard-asset
proxy without having to forecast which one wins over any single cycle.

Interpretation variant 2 (source is an abstract-only blog post — no disclosed
parameters, rebalance cadence, or absolute-momentum overlay). Deliberately
DIFFERENT reading from variant 1 everywhere the source is ambiguous:

  - Momentum definition: TRUE dual momentum, Antonacci-style. "Dual" is read
    as referring to the classic two-part construction — RELATIVE momentum
    (rank GLD vs. IBIT) gated by an ABSOLUTE momentum filter (the chosen
    asset's own trailing return must be positive vs. a flat/cash baseline).
    When both assets' absolute momentum is negative, the strategy goes FLAT
    (no position) rather than being forced into whichever asset merely lost
    less — this is the standard academic meaning of "dual momentum" and is
    at least as defensible as variant 1's relative-only reading, and it
    gives the strategy its own drawdown control independent of vol-sizing.
  - Lookback: 8 weeks read LITERALLY as 8 calendar weeks — closes are
    resampled to weekly (Friday) bars first, then the 8-period lookback is
    applied to that weekly series. This is a lower-resolution, smoother
    reading than variant 1's 40-raw-trading-day approach.
  - Rebalance cadence: WEEKLY (signals only regenerate on the last trading
    day of the ISO week), matching the weekly resample used for the
    momentum calc, rather than recomputing on every daily bar.
  - Vol targeting: a LONGER realized-vol lookback (13 weekly bars, ~1
    quarter) computed on the same weekly return series, so position size
    responds to the vol regime on the same clock as the rotation signal
    rather than reacting to daily noise.
  - Regime eligibility: because the absolute-momentum/cash leg is now the
    primary risk control (the strategy can sit out entirely), it is scoped
    to LOW_VOL/TRANSITIONING only — unlike variant 1, it does NOT claim
    HIGH_VOL eligibility, since a stale weekly rebalance is a poor fit for
    fast-moving elevated-vol tape.
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal

__all__ = ['GoldIbitDualMomentumVolCappedV2']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID      = 'S_gold_ibit_dual_momentum_vol_capped'

BASKET               = ['GLD', 'IBIT']
LOOKBACK_WEEKS       = 8    # literal 8 calendar weeks, on a weekly-resampled series
VOL_LOOKBACK_WEEKS   = 13   # ~1 quarter of weekly bars
VOL_TARGET           = 0.20 # 20% annualized vol target
WEEKS_PER_YEAR       = 52.0
BASE_SIZE            = 0.75
MIN_WEEKLY_BARS      = LOOKBACK_WEEKS + VOL_LOOKBACK_WEEKS + 1


class GoldIbitDualMomentumVolCappedV2(BaseStrategy):
    """True dual momentum: LONG whichever of GLD/IBIT has the higher 8-week
    (weekly-resampled) return, but ONLY if that asset's own absolute
    momentum is positive; otherwise FLAT. Sized to a 20% annualized
    realized-vol target computed on the same weekly clock. Weekly rebalance."""

    id                = STRATEGY_ID
    name              = 'GoldIbitDualMomentumVolCappedV2'
    description       = (
        'True dual momentum rotation between GLD and IBIT: rank by 8-week '
        '(weekly-resampled) return, LONG the stronger asset only if its '
        'absolute momentum is positive (else FLAT/cash), sized to a 20% '
        'annualized realized-vol target, rebalanced weekly.'
    )
    tier              = 2
    signal_frequency  = 'weekly'
    min_lookback      = MIN_WEEKLY_BARS * 7  # weekly bars -> approx calendar days
    instrument_class  = INSTRUMENT_CLASS
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 1

    def default_parameters(self) -> dict:
        return {
            'lookback_weeks':     LOOKBACK_WEEKS,
            'vol_lookback_weeks': VOL_LOOKBACK_WEEKS,
            'vol_target':         VOL_TARGET,
            'base_size':          BASE_SIZE,
        }

    def _weekly_momentum(self, weekly_series: pd.Series, lookback: int) -> float:
        """Weekly-bar lookback return. NaN if insufficient history."""
        if len(weekly_series) <= lookback:
            return float('nan')
        p0 = float(weekly_series.iloc[-1 - lookback])
        p1 = float(weekly_series.iloc[-1])
        if p0 <= 0:
            return float('nan')
        return p1 / p0 - 1.0

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

        available = [t for t in BASKET if t in prices.columns]
        if len(available) < 2:
            print(f'[{STRATEGY_ID}_v2] fewer than 2 basket tickers available; signals=0', file=sys.stderr)
            return []

        # Weekly rebalance clock: only regenerate on the last trading day of
        # the ISO week (or on a regime-flip cadence reset), unlike variant 1
        # which recomputes daily.
        last_date = prices.index[-1] if hasattr(prices.index[-1], 'weekday') else None
        if last_date is not None and not self.cadence_reset(regime):
            is_week_end = (last_date + pd.tseries.offsets.BDay(1)).week != last_date.week
            if not is_week_end:
                print(f'[{STRATEGY_ID}_v2] non-rebalance day; signals=0', file=sys.stderr)
                return []

        lookback     = int(self.parameters.get('lookback_weeks', LOOKBACK_WEEKS))
        vol_lookback = int(self.parameters.get('vol_lookback_weeks', VOL_LOOKBACK_WEEKS))
        vol_target   = float(self.parameters.get('vol_target', VOL_TARGET))
        base_size    = float(self.parameters.get('base_size', BASE_SIZE))
        scale        = self.position_scale(regime_state)

        weekly = prices[available].resample('W-FRI').last().dropna(how='all')

        momentum = {}
        weekly_by_ticker = {}
        daily_by_ticker  = {}
        for ticker in available:
            w = weekly[ticker].dropna()
            weekly_by_ticker[ticker] = w
            daily_by_ticker[ticker]  = prices[ticker].dropna()
            if len(w) < lookback + 1:
                continue
            momentum[ticker] = self._weekly_momentum(w, lookback)

        momentum = {t: m for t, m in momentum.items() if pd.notna(m)}
        if len(momentum) < len(available):
            print(f'[{STRATEGY_ID}_v2] insufficient weekly history to rank; signals=0', file=sys.stderr)
            return []

        chosen_ticker = max(momentum, key=momentum.get)
        chosen_mom    = momentum[chosen_ticker]

        # Absolute-momentum filter (the "dual" in dual momentum): the
        # leading asset must ALSO be positive on its own trailing return,
        # else the strategy goes FLAT rather than being forced long.
        if chosen_mom <= 0:
            print(f'[{STRATEGY_ID}_v2] absolute momentum negative for {chosen_ticker}; going FLAT, signals=0', file=sys.stderr)
            return []

        price_series = daily_by_ticker[chosen_ticker]
        current_price = float(price_series.iloc[-1])
        if current_price <= 0:
            print(f'[{STRATEGY_ID}_v2] bad price; signals=0', file=sys.stderr)
            return []

        # Realized-vol targeting on the chosen asset's weekly returns —
        # a longer, smoother window than variant 1's daily short window.
        vol_weight = 1.0
        chosen_weekly = weekly_by_ticker[chosen_ticker]
        weekly_rets = chosen_weekly.pct_change().dropna()
        if len(weekly_rets) >= vol_lookback:
            recent = weekly_rets.iloc[-vol_lookback:]
            ann_vol = float(recent.std() * np.sqrt(WEEKS_PER_YEAR))
            if ann_vol > 0:
                vol_weight = min(1.0, vol_target / ann_vol)

        position_size = round(base_size * vol_weight * scale, 4)
        if position_size < 0.01:
            print(f'[{STRATEGY_ID}_v2] position size below floor; signals=0', file=sys.stderr)
            return []

        other_mom = [m for t, m in momentum.items() if t != chosen_ticker]
        spread = chosen_mom - (other_mom[0] if other_mom else 0.0)
        if spread > 0.10:
            confidence = 'HIGH'
        elif spread > 0.04:
            confidence = 'MED'
        else:
            confidence = 'LOW'

        st = self.compute_stops_and_targets(
            price_series, direction='LONG', current_price=current_price,
            regime_state=regime_state,
        )

        signals = [Signal(
            ticker            = chosen_ticker,
            direction         = 'LONG',
            entry_price       = current_price,
            stop_loss         = float(st['stop']),
            target_1          = float(st['t1']),
            target_2          = float(st['t2']),
            target_3          = float(st['t3']),
            position_size_pct = position_size,
            confidence        = confidence,
            signal_params     = {
                'momentum':    {t: round(m, 6) for t, m in momentum.items()},
                'chosen':      chosen_ticker,
                'vol_weight':  round(vol_weight, 4),
                'lookback':    lookback,
                'rebalance':   'weekly',
                'abs_momentum_gate': True,
            },
        )]
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

        cols = [c for c in BASKET if c in wide.columns]
        rows = []
        if len(cols) == 2:
            weekly = wide[cols].resample('W-FRI').last().dropna(how='any')
            dates  = weekly.index
            gld_w  = weekly[cols[0]].values.astype(float)
            ibit_w = weekly[cols[1]].values.astype(float)

            held_ticker = None
            entry_idx   = None
            entry_price = None

            for idx in range(MIN_WEEKLY_BARS, len(weekly) - 1):
                mom0 = gld_w[idx] / gld_w[idx - 1 - LOOKBACK_WEEKS] - 1.0
                mom1 = ibit_w[idx] / ibit_w[idx - 1 - LOOKBACK_WEEKS] - 1.0
                winner     = cols[0] if mom0 >= mom1 else cols[1]
                winner_mom = mom0 if winner == cols[0] else mom1
                winner_arr = gld_w if winner == cols[0] else ibit_w

                target = winner if winner_mom > 0 else None

                if held_ticker != target:
                    if held_ticker is not None:
                        held_arr = gld_w if held_ticker == cols[0] else ibit_w
                        exit_price = held_arr[idx]
                        raw_ret = (exit_price - entry_price) / entry_price
                        sig_date = dates[entry_idx]
                        rows.append({
                            'strategy_id':  STRATEGY_ID,
                            'signal_date':  str(sig_date.date()),
                            'regime_state': regime_map.get(sig_date, 'LOW_VOL'),
                            'pnl':          float(raw_ret),
                            'r_multiple':   float(raw_ret / 0.05),
                        })
                    held_ticker = target
                    if target is not None:
                        entry_idx   = idx
                        entry_price = winner_arr[idx]
                    else:
                        entry_idx   = None
                        entry_price = None

            if held_ticker is not None and entry_idx is not None:
                held_arr = gld_w if held_ticker == cols[0] else ibit_w
                exit_price = held_arr[-1]
                raw_ret = (exit_price - entry_price) / entry_price
                sig_date = dates[entry_idx]
                rows.append({
                    'strategy_id':  STRATEGY_ID,
                    'signal_date':  str(sig_date.date()),
                    'regime_state': regime_map.get(sig_date, 'LOW_VOL'),
                    'pnl':          float(raw_ret),
                    'r_multiple':   float(raw_ret / 0.05),
                })

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
