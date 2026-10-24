"""S_9sig_sma200_override — 9Sig TQQQ/AGG quarterly rebalance with a
200-day SMA de-risking override.

Source: https://bestfolio.app/blog/9sig-deep-history-sma-override
"9Sig reached practical ruin in 42% of resampled histories. A 200-day
override removed it, and cost 8 points a year when nothing crashed." (2026).

Thesis: Kelly's 9Sig (TQQQ/AGG quarterly true-up to a 9%/quarter TQQQ-sleeve
growth line) hits near-ruin in ~42% of 24-month-block bootstrap-resampled
histories since Dec 1986. Overlaying a 200-day SMA de-risking override —
flee to cash whenever price is below its 200-day trend — eliminates the
ruin tail, at the cost of ~8 points of annual return in years nothing
crashes (the override is a tax on every false alarm, not just the real
crashes it catches).

Interpretation variant 1 of 2. The pseudocode leaves four details
underspecified; a colleague implementing variant 2 would be expected to
resolve each the opposite way:

  - SMA reference asset: "price(TQQQ or QQQ) < SMA_200(same)" is an
    explicit either/or. This variant uses TQQQ's OWN 200-day SMA (the
    literal leveraged-decay-aware filter on the instrument actually
    held) rather than QQQ's unleveraged SMA, which would be smoother
    and trigger later/less often.
  - Override scope: "allocation = 100% cash" is read LITERALLY — the
    ENTIRE book (TQQQ leg AND AGG leg) is flattened to cash when the
    override fires. A softer reading would leave AGG standing, since
    it is already the strategy's own defensive sleeve; this variant
    treats "cash" as cash, full stop.
  - Override check cadence: evaluated DAILY, not only at the quarterly
    9Sig rebalance point. A crash-protection override exists to act
    before the next scheduled quarterly date, so this variant lets it
    arm/disarm on any trading day rather than waiting for a quarter
    boundary.
  - Target-line vs. allocation decoupling: "target_TQQQ_value *= 1.09"
    carries no override clause, so the 9%/quarter signal line compounds
    UNCONDITIONALLY every quarter regardless of override state — only
    the ALLOCATION step is replaced by cash. A quarter-end that lands
    while overridden skips the band comparison entirely; the sleeve
    weight freezes at its last computed value until a future
    quarter-end where the override is inactive.

No universe_filter (fixed two-ticker ETP pair, per INFERRED_UNIVERSE_FILTER).
"""
from __future__ import annotations

import sys
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal
from strategies.safe_ops import EPS

__all__ = ['Sig9Sma200Override']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID       = 'S_9sig_sma200_override'
TQQQ              = 'TQQQ'
AGG               = 'AGG'

QUARTERLY_GROWTH  = 0.09    # 9% per quarter, unconditional target-line compounding
BAND              = 0.05    # symmetric 9Sig no-trade band around the target line
SMA_WINDOW        = 200     # de-risking override lookback, on TQQQ's own close
BASE_GROSS        = 0.12    # total sleeve gross exposure (TQQQ + AGG legs)
INCEPTION_TQQQ_WT = 1.00    # pure signal-line inception: 100% TQQQ until first true-up
ATR_MULT          = 2.0


def _quarter_key(ts: pd.Timestamp) -> tuple:
    return (ts.year, ts.quarter)


def _simulate_9sig_sma_override(tqqq: pd.Series, agg: pd.Series):
    """Walk the daily-aligned TQQQ/AGG series forward: 9Sig quarterly
    true-up against an unconditionally-compounding 9%/quarter target line,
    overridden to 100% cash (both legs) on any day TQQQ closes below its
    own 200-day SMA (checked daily, not just at quarter-end).

    Returns (effective_weight_tqqq, sig_weight_tqqq, overridden, trigger_rows).
    `effective_weight_tqqq` is what the book holds today (0 if overridden);
    `sig_weight_tqqq` is the underlying 9Sig allocation the override is
    suppressing / will resume to once it lifts.
    """
    idx = tqqq.index
    r_tqqq = tqqq.pct_change().fillna(0.0)
    r_agg  = agg.pct_change().fillna(0.0)
    sma200 = tqqq.rolling(SMA_WINDOW, min_periods=SMA_WINDOW).mean()

    nav        = 1.0
    nav_target = 1.0
    sig_weight = INCEPTION_TQQQ_WT
    prev_effective = None
    trigger_rows = []

    for i in range(1, len(idx)):
        sma_i = sma200.iloc[i]
        overridden = bool(pd.notna(sma_i) and float(tqqq.iloc[i]) < float(sma_i))
        effective_weight = 0.0 if overridden else sig_weight

        if overridden:
            daily_ret = 0.0   # full cash — neither leg participates
        else:
            daily_ret = effective_weight * float(r_tqqq.iloc[i]) + (1.0 - effective_weight) * float(r_agg.iloc[i])
        nav *= (1.0 + daily_ret)

        this_q = _quarter_key(idx[i])
        next_q = _quarter_key(idx[i + 1]) if i + 1 < len(idx) else None
        is_quarter_end = (this_q != next_q) or (next_q is None)

        if is_quarter_end:
            nav_target *= (1.0 + QUARTERLY_GROWTH)   # unconditional, override-independent
            if not overridden:
                gap_pct = ((nav_target - nav) / nav_target) if nav_target else 0.0
                if gap_pct > BAND:
                    sig_weight = min(1.0, nav_target / nav) if nav > 0 else 1.0
                elif gap_pct < -BAND:
                    sig_weight = max(0.0, min(1.0, nav_target / nav)) if nav > 0 else 0.0
                # else: within band — sig_weight unchanged
                sig_weight = max(0.0, min(1.0, sig_weight))
            # overridden at quarter-end: band comparison skipped, sig_weight frozen

        if prev_effective is None or effective_weight != prev_effective or is_quarter_end:
            trigger_rows.append({
                'date':             idx[i],
                'effective_weight': effective_weight,
                'sig_weight':       sig_weight,
                'overridden':       overridden,
                'nav':              nav,
                'nav_target':       nav_target,
            })
        prev_effective = effective_weight

    final_overridden = trigger_rows[-1]['overridden'] if trigger_rows else False
    final_effective  = trigger_rows[-1]['effective_weight'] if trigger_rows else 0.0
    return final_effective, sig_weight, final_overridden, trigger_rows


class Sig9Sma200Override(BaseStrategy):
    """9Sig TQQQ/AGG quarterly true-up to a 9%/quarter signal line,
    overridden to 100% cash (both legs) on any day TQQQ closes below its
    own 200-day SMA. Variant 1 of 2: TQQQ's own SMA, full-book cash scope,
    daily override cadence, target line compounds unconditionally."""

    id                = STRATEGY_ID
    name              = 'Sig9Sma200Override'
    description       = (
        "Kelly '9Sig' TQQQ/AGG quarterly true-up to a 9%/quarter signal line, "
        'overridden to 100% cash whenever TQQQ closes below its own 200-day SMA '
        '(checked daily); removes the resampled-ruin tail at the cost of '
        'return in no-crash years.'
    )
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 504
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    instrument_class  = INSTRUMENT_CLASS
    MAX_SIGNALS       = 2

    def default_parameters(self) -> dict:
        return {
            'quarterly_growth':  QUARTERLY_GROWTH,
            'band':              BAND,
            'sma_window':        SMA_WINDOW,
            'base_gross':        BASE_GROSS,
            'inception_tqqq_wt': INCEPTION_TQQQ_WT,
        }

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

        if TQQQ not in prices.columns or AGG not in prices.columns:
            print('[debug] signals=0', file=sys.stderr)
            return []

        both = prices[[TQQQ, AGG]].dropna()
        if len(both) < self.min_lookback:
            print('[debug] signals=0', file=sys.stderr)
            return []

        effective_weight, sig_weight, overridden, trigger_rows = _simulate_9sig_sma_override(
            both[TQQQ], both[AGG]
        )

        last_date = both.index[-1]
        prev_date = both.index[-2] if len(both) > 1 else last_date
        is_quarter_end_today = _quarter_key(last_date) != _quarter_key(prev_date)
        override_changed_today = (
            trigger_rows[-1]['overridden'] != trigger_rows[-2]['overridden']
            if len(trigger_rows) >= 2 else bool(trigger_rows)
        )

        if not (is_quarter_end_today or override_changed_today or self.cadence_reset(regime)):
            print('[debug] signals=0', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)
        tqqq_price = float(both[TQQQ].iloc[-1])
        agg_price  = float(both[AGG].iloc[-1])
        signals: List[Signal] = []

        if overridden:
            for ticker, price in ((TQQQ, tqqq_price), (AGG, agg_price)):
                signals.append(Signal(
                    ticker            = ticker,
                    direction         = 'FLAT',
                    entry_price       = price,
                    stop_loss         = price,
                    target_1          = price,
                    target_2          = price,
                    target_3          = price,
                    position_size_pct = 0.0,
                    confidence        = 'HIGH',
                    signal_params     = {
                        'trigger':    'sma200_override_cash',
                        'sig_weight': round(sig_weight, 4),
                        'sma_window': SMA_WINDOW,
                    },
                ))
        else:
            confidence = 'MED' if abs(sig_weight - INCEPTION_TQQQ_WT) > EPS else 'LOW'
            rebalance_tag = 'quarterly_quarter_end' if is_quarter_end_today else 'override_lifted'

            tqqq_size = round(effective_weight * BASE_GROSS * scale, 4)
            if tqqq_size >= 0.005:
                stops = self.compute_stops_and_targets(
                    both[TQQQ], direction='LONG', current_price=tqqq_price,
                    atr_multiplier=ATR_MULT, regime_state=regime_state,
                )
                signals.append(Signal(
                    ticker            = TQQQ,
                    direction         = 'LONG',
                    entry_price       = tqqq_price,
                    stop_loss         = float(stops['stop']),
                    target_1          = float(stops['t1']),
                    target_2          = float(stops['t2']),
                    target_3          = float(stops['t3']),
                    position_size_pct = tqqq_size,
                    confidence        = confidence,
                    signal_params     = {'sig_weight': round(sig_weight, 4), 'rebalance': rebalance_tag, 'leg': 'leveraged'},
                ))

            agg_weight = 1.0 - effective_weight
            agg_size   = round(agg_weight * BASE_GROSS * scale, 4)
            if agg_size >= 0.005:
                stops = self.compute_stops_and_targets(
                    both[AGG], direction='LONG', current_price=agg_price,
                    atr_multiplier=ATR_MULT, regime_state=regime_state,
                )
                signals.append(Signal(
                    ticker            = AGG,
                    direction         = 'LONG',
                    entry_price       = agg_price,
                    stop_loss         = float(stops['stop']),
                    target_1          = float(stops['t1']),
                    target_2          = float(stops['t2']),
                    target_3          = float(stops['t3']),
                    position_size_pct = agg_size,
                    confidence        = confidence,
                    signal_params     = {'weight_agg': round(agg_weight, 4), 'rebalance': rebalance_tag, 'leg': 'defensive'},
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
        if TQQQ in wide.columns and AGG in wide.columns:
            both = wide[[TQQQ, AGG]].dropna()
            if len(both) >= 504:
                _, _, _, trigger_rows = _simulate_9sig_sma_override(both[TQQQ], both[AGG])
                prev_nav = 1.0
                for t in trigger_rows:
                    raw_ret = (t['nav'] / prev_nav) - 1.0 if prev_nav else 0.0
                    rows.append({
                        'strategy_id':  STRATEGY_ID,
                        'signal_date':  str(t['date'].date()),
                        'regime_state': regime_map.get(t['date'], 'LOW_VOL'),
                        'pnl':          float(raw_ret),
                        'r_multiple':   float(raw_ret / 0.09) if raw_ret else 0.0,
                    })
                    prev_nav = t['nav']

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
