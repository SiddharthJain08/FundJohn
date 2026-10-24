"""S_9sig_tqqq_nav_rebalance_tv2 — Jason Kelly's "9Sig" TQQQ/AGG signal-line
rebalance.

Source: https://bestfolio.app/blog/kelly-signal-danger
"9Sig Backtest: Kelly's TQQQ Rules Since 1999" (2026).

Thesis: hold a leveraged-Nasdaq sleeve (TQQQ) against a bond sleeve (AGG) and
true the TQQQ sub-account up/down to a NAV line that is mandated to grow 9%
every quarter. The source paper is itself a rejection-log writeup (reported
max_drawdown=0.997) — this is a faithful replication of a publicly
documented rule, not a proposed edge.

Interpretation variant 2 of 2. The pseudocode is unambiguous that (a) the
target line is denominated in 9%-per-quarter growth, (b) shortfalls buy
TQQQ funded by AGG, (c) surpluses (by a "sell-band") sell TQQQ into AGG,
and (d) there is no shorting. The same four details variant 1 left
underspecified are resolved the OTHER way here:

  - Target-line independence: `NAV_target` RESETS to `actual_NAV * 1.09`
    at every quarter trigger (a "soft" reading — the target line tracks
    the real book, not its own prior value). This is the self-healing
    interpretation: a drawn-down account gets a smaller absolute target
    next quarter rather than a line that keeps compounding in isolation
    and mandates ever-larger TQQQ buys into a falling book. Variant 1
    instead let the target compound from itself with no reset, which is
    what produces the pathological 99.7% drawdown the source paper
    documents — this variant will NOT reproduce that number, by design,
    since it is testing the alternate, more literal-to-"NAV" (not
    "NAV_target") reading of the recurrence name.
  - Sell-band width: applied SYMMETRICALLY to both sides — a shortfall
    must exceed 5% of the target before TQQQ is bought, not just before
    it is sold. Variant 1 made the buy side unconditional (any shortfall
    trued up in full) and reserved the band for the sell side only; this
    variant treats "band" as a general whipsaw filter Kelly applies to
    both legs of the true-up, which is an equally defensible reading of
    an underspecified single word ("sell-band") used once in the
    pseudocode for a mechanism the paper never fully disambiguates.
  - Inception allocation: 60% TQQQ / 40% AGG at the first day both
    tickers have data — a conservative starting split that treats AGG as
    a real defensive sleeve from day one, vs. variant 1's 100% TQQQ
    (the more aggressive, "pure signal-line" reading).
  - Rebalance trigger: the LAST trading day of each quarter, sized off
    that same day's own close (the quarter's own final mark decides the
    quarter's own trade) — vs. variant 1's first-day-of-next-quarter
    trigger. Both are defensible: this variant reads "each period" as
    "at period end," not "at the first opportunity after period end."

No universe_filter (fixed two-ticker ETP pair, per INFERRED_UNIVERSE_FILTER).
"""
from __future__ import annotations

import sys
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal

__all__ = ['Sig9TqqqNavRebalanceTv2']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID      = 'S_9sig_tqqq_nav_rebalance_tv2'
TQQQ             = 'TQQQ'
AGG              = 'AGG'

QUARTERLY_GROWTH  = 0.09     # 9% per quarter, applied to a self-healing reset target
BAND              = 0.05     # symmetric whipsaw band on BOTH buy and sell legs
BASE_GROSS        = 0.12     # total sleeve gross exposure (TQQQ + AGG legs)
INCEPTION_TQQQ_WT = 0.60     # conservative 60/40 inception split
ATR_MULT          = 2.0


def _quarter_key(ts: pd.Timestamp) -> tuple:
    return (ts.year, ts.quarter)


def _simulate_9sig_tv2(tqqq: pd.Series, agg: pd.Series):
    """Walk the daily-aligned TQQQ/AGG series forward from inception, applying
    the variant-2 9Sig recurrence at each quarter-END trigger (self-healing
    target reset, symmetric band). Returns (weight_tqqq, trigger_rows)."""
    idx = tqqq.index
    r_tqqq = tqqq.pct_change().fillna(0.0)
    r_agg  = agg.pct_change().fillna(0.0)

    weight_tqqq = INCEPTION_TQQQ_WT   # inception allocation (variant 2 choice)
    nav         = 1.0
    nav_target  = 1.0
    cur_q       = _quarter_key(idx[0])
    trigger_rows = []

    for i in range(1, len(idx)):
        daily_ret = weight_tqqq * float(r_tqqq.iloc[i]) + (1.0 - weight_tqqq) * float(r_agg.iloc[i])
        nav *= (1.0 + daily_ret)

        this_q   = _quarter_key(idx[i])
        next_q   = _quarter_key(idx[i + 1]) if i + 1 < len(idx) else None
        is_quarter_end = (this_q != next_q) or (next_q is None)

        if is_quarter_end:
            # self-healing reset: next quarter's target grows 9% off TODAY's
            # actual NAV, not off the old target line.
            nav_target = nav * (1.0 + QUARTERLY_GROWTH)
            gap = nav_target - nav   # positive = shortfall (buy TQQQ), negative = surplus
            gap_pct = (gap / nav_target) if nav_target else 0.0

            if gap_pct > BAND:
                new_tqqq_value = min(nav, nav_target)
                weight_tqqq = (new_tqqq_value / nav) if nav > 0 else 1.0
            elif gap_pct < -BAND:
                new_tqqq_value = max(0.0, nav_target)
                weight_tqqq = min(1.0, new_tqqq_value / nav) if nav > 0 else 0.0
            # else: within the symmetric band on EITHER side — carry weight forward
            weight_tqqq = max(0.0, min(1.0, weight_tqqq))
            trigger_rows.append({
                'date':        idx[i],
                'weight_tqqq': weight_tqqq,
                'nav':         nav,
                'nav_target':  nav_target,
                'gap_pct':     gap_pct,
            })
            cur_q = this_q

    return weight_tqqq, trigger_rows


class Sig9TqqqNavRebalanceTv2(BaseStrategy):
    """Kelly 9Sig (variant 2 of 2): quarter-END TQQQ/AGG true-up against a
    self-healing 9%-per-quarter NAV growth line (reset off actual NAV each
    quarter, not off the prior target). Symmetric 5% whipsaw band gates both
    buys and sells; 60/40 inception split; no shorting. Deliberately resolves
    all four underspecified pseudocode details the opposite way from
    variant 1, so it will NOT reproduce the source paper's reported 99.7%
    drawdown pathology."""

    id                = STRATEGY_ID
    name              = 'Sig9TqqqNavRebalanceTv2'
    description       = (
        "Kelly '9Sig' (variant 2 of 2): quarter-end TQQQ/AGG true-up against "
        'a self-healing 9%/quarter NAV line (resets off actual NAV); '
        'symmetric 5% band on both buy and sell legs; 60/40 inception split.'
    )
    tier              = 3
    signal_frequency  = 'quarterly'
    min_lookback      = 756
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    instrument_class  = INSTRUMENT_CLASS
    MAX_SIGNALS       = 2

    def default_parameters(self) -> dict:
        return {
            'quarterly_growth':  QUARTERLY_GROWTH,
            'band':              BAND,
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

        last_date = both.index[-1]
        prev_date = both.index[-2] if len(both) > 1 else last_date
        # Quarter-END trigger: today is the last day of its quarter if the
        # NEXT bar (or, at the live edge, "now") belongs to a new quarter.
        # On the live edge we treat the most recent bar as a tentative
        # quarter-end candidate, confirmed once `cadence_reset` fires.
        is_quarter_end_candidate = (
            _quarter_key(last_date) != _quarter_key(prev_date)
            or self.cadence_reset(regime)
        )

        if not is_quarter_end_candidate:
            print('[debug] signals=0', file=sys.stderr)
            return []

        weight_tqqq, trigger_rows = _simulate_9sig_tv2(both[TQQQ], both[AGG])
        gap_pct = trigger_rows[-1]['gap_pct'] if trigger_rows else 0.0

        scale = self.position_scale(regime_state)
        tqqq_price = float(both[TQQQ].iloc[-1])
        agg_price  = float(both[AGG].iloc[-1])

        if abs(gap_pct) > 0.15:
            confidence = 'HIGH'
        elif abs(gap_pct) > 0.05:
            confidence = 'MED'
        else:
            confidence = 'LOW'

        signals: List[Signal] = []

        tqqq_size = round(weight_tqqq * BASE_GROSS * scale, 4)
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
                signal_params     = {
                    'weight_tqqq': round(weight_tqqq, 4),
                    'gap_pct':     round(gap_pct, 4),
                    'rebalance':   'quarterly_quarter_end',
                    'leg':         'leveraged',
                },
            ))

        agg_weight = 1.0 - weight_tqqq
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
                signal_params     = {
                    'weight_agg':  round(agg_weight, 4),
                    'gap_pct':     round(gap_pct, 4),
                    'rebalance':   'quarterly_quarter_end',
                    'leg':         'defensive',
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
        if TQQQ in wide.columns and AGG in wide.columns:
            both = wide[[TQQQ, AGG]].dropna()
            if len(both) >= 756:
                _, trigger_rows = _simulate_9sig_tv2(both[TQQQ], both[AGG])
                prev_nav = 1.0
                for t in trigger_rows:
                    raw_ret = (t['nav'] / prev_nav) - 1.0 if prev_nav else 0.0
                    rows.append({
                        'strategy_id':  STRATEGY_ID,
                        'signal_date':  str(t['date'].date()),
                        'regime_state': regime_map.get(t['date'], 'LOW_VOL'),
                        'pnl':          float(raw_ret),
                        'r_multiple':   float(raw_ret / 0.09),
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
