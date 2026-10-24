"""S_leveraged_portfolio_trend_brake_tv2 — a long-run trend "brake" applied
at the multi-asset leveraged-SLEEVE level, keyed off each sleeve's
UNLEVERAGED underlying index.

Source: BestFolio (2026), "A simple safety brake for leveraged portfolios"
(https://bestfolio.app/blog/catastrophe-brake-leveraged-portfolios).

Hypothesis: a long-run trend filter (price vs. a moving average) switches a
leveraged portfolio to cash/unlevered whenever the trend turns down,
sidestepping the worst of a secular bear market's compounding decay on
leveraged exposure while preserving most of the leveraged upside during
uptrends.

Interpretation variant 2 of 2. The abstract leaves the same three details
underspecified as variant 1; each is resolved the OPPOSITE way here:

  - Trend reference: variant 1 reads the trend filter literally on each
    leveraged ETP's OWN price. This variant instead runs the filter on the
    UNLEVERAGED underlying index each ETP tracks (SPY for UPRO/SSO, QQQ for
    TQQQ/QLD, TLT for TMF, GLD for UGL) — the underlying's own 200-day SMA
    is free of the leveraged ETP's daily-reset volatility drag, so this is
    the smoother, decay-agnostic reading of "price vs. moving average."
  - Re-evaluation cadence: variant 1 checks DAILY with a 1% hysteresis
    band. This variant re-evaluates only on the first trading day of each
    calendar MONTH (a bare zero-buffer crossover on that one day; between
    re-evaluation days the brake state is simply held, which is itself a
    much stronger whipsaw damper than a hysteresis band).
  - Four leveraged portfolio variants: variant 1 reads this as four
    single-ticker leveraged ETPs. This variant instead reads "four existing
    popular leveraged portfolios" literally as four MULTI-ASSET leveraged
    allocation sleeves (HFEA-style stock/bond/gold blends), each with 2-3
    leveraged legs, and the brake is applied once per SLEEVE (a
    weight-blended trend score across its legs' underlyings), not once per
    leg.

min_lookback_required=504 and minimum_universe_size=4 (per spec) are kept
as-is (4 sleeves spanning 6 underlying leveraged tickers). No
universe_filter (fixed basket, per INFERRED_UNIVERSE_FILTER=null).
"""
from __future__ import annotations

import sys
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal

__all__ = ['LeveragedPortfolioTrendBrakeTv2']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID      = 'S_leveraged_portfolio_trend_brake_tv2'

# Four multi-asset leveraged allocation sleeves (legs: {ticker: weight}).
SLEEVES = {
    'HFEA':             {'UPRO': 0.55, 'TMF': 0.45},
    'LEV_GROWTH':       {'TQQQ': 0.60, 'UPRO': 0.40},
    'LEV_BALANCED':     {'SSO': 0.50, 'TMF': 0.50},
    'LEV_DIVERSIFIED':  {'QLD': 0.40, 'UGL': 0.30, 'TMF': 0.30},
}

# Each leveraged leg's unleveraged underlying index, used as the trend
# reference instead of the leveraged ETP's own price.
UNDERLYING_MAP = {
    'UPRO': 'SPY', 'SSO': 'SPY',
    'TQQQ': 'QQQ', 'QLD': 'QQQ',
    'TMF':  'TLT',
    'UGL':  'GLD',
}

SMA_WINDOW   = 200   # long-run trend window, on the underlying index
SLEEVE_GROSS = 0.15  # per-sleeve base weight pre-regime-scale (4 x 0.15 = 60% max gross)
ATR_MULT     = 2.0


def _underlying_trend_score(underlying_close: pd.Series) -> float:
    """(price - SMA) / SMA on the UNLEVERAGED underlying's own close.

    Positive => underlying above its long-run trend; negative => below.
    Returns float('nan') if there isn't enough history yet.
    """
    if len(underlying_close) < SMA_WINDOW + 1:
        return float('nan')
    sma = underlying_close.rolling(SMA_WINDOW, min_periods=SMA_WINDOW).mean()
    last_sma = sma.iloc[-1]
    if pd.isna(last_sma) or last_sma <= 0:
        return float('nan')
    last_price = float(underlying_close.iloc[-1])
    return (last_price - float(last_sma)) / float(last_sma)


def _is_month_start(idx: pd.DatetimeIndex) -> bool:
    """True iff the last timestamp in idx is the first trading day of its
    calendar month within idx (monthly re-evaluation gate)."""
    if len(idx) == 0:
        return False
    last = idx[-1]
    month_mask = (idx.year == last.year) & (idx.month == last.month)
    month_days = idx[month_mask]
    return len(month_days) > 0 and month_days[0] == last


class LeveragedPortfolioTrendBrakeTv2(BaseStrategy):
    """Brakes four multi-asset leveraged sleeves (HFEA / LEV_GROWTH /
    LEV_BALANCED / LEV_DIVERSIFIED) to cash whenever the weight-blended
    200-day trend score of each sleeve's UNLEVERAGED underlyings turns
    negative, re-evaluated once per calendar month (variant 2/2)."""

    id                = STRATEGY_ID
    name              = 'LeveragedPortfolioTrendBrakeTv2'
    description       = (
        'Monthly, sleeve-level 200-day underlying-index trend brake '
        '(HFEA/LEV_GROWTH/LEV_BALANCED/LEV_DIVERSIFIED) switches each '
        'multi-asset leveraged sleeve to cash when its blended underlying '
        'trend score turns negative, re-evaluated on the first trading '
        'day of each month.'
    )
    tier              = 2
    signal_frequency  = 'monthly'
    min_lookback      = 504
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    instrument_class  = INSTRUMENT_CLASS
    MAX_SIGNALS       = sum(len(legs) for legs in SLEEVES.values())

    def default_parameters(self) -> dict:
        return {
            'sma_window':   SMA_WINDOW,
            'sleeve_gross': SLEEVE_GROSS,
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

        idx = prices.index
        if not isinstance(idx, pd.DatetimeIndex):
            idx = pd.to_datetime(idx)
        if not _is_month_start(idx):
            # Not a re-evaluation day this month -- hold current allocation.
            print('[debug] signals=0', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)
        signals: List[Signal] = []

        for sleeve_name, legs in SLEEVES.items():
            # Blended trend score across this sleeve's underlyings.
            weighted_score = 0.0
            weight_covered = 0.0
            legs_ok = True
            for ticker, weight in legs.items():
                underlying = UNDERLYING_MAP.get(ticker)
                if ticker not in prices.columns or underlying not in prices.columns:
                    legs_ok = False
                    break
                leg_close = prices[ticker].dropna()
                under_close = prices[underlying].dropna()
                if len(leg_close) < self.min_lookback or len(under_close) < self.min_lookback:
                    legs_ok = False
                    break
                score = _underlying_trend_score(under_close)
                if pd.isna(score):
                    legs_ok = False
                    break
                weighted_score += weight * score
                weight_covered += weight

            if not legs_ok or weight_covered <= 0:
                continue

            direction = 'LONG' if weighted_score > 0 else 'FLAT'

            for ticker, weight in legs.items():
                leg_close = prices[ticker].dropna()
                current_price = float(leg_close.iloc[-1])

                if direction == 'LONG':
                    leg_weight = round(SLEEVE_GROSS * weight * scale, 4)
                    stops = self.compute_stops_and_targets(
                        leg_close, direction='LONG', current_price=current_price,
                        atr_multiplier=ATR_MULT, regime_state=regime_state,
                    )
                    signals.append(Signal(
                        ticker            = ticker,
                        direction         = 'LONG',
                        entry_price       = current_price,
                        stop_loss         = float(stops['stop']),
                        target_1          = float(stops['t1']),
                        target_2          = float(stops['t2']),
                        target_3          = float(stops['t3']),
                        position_size_pct = leg_weight,
                        confidence        = 'MED',
                        signal_params     = {
                            'sleeve':            sleeve_name,
                            'sleeve_weight':      weight,
                            'sma_window':         SMA_WINDOW,
                            'trend_score':        round(weighted_score, 6),
                            'trigger':            'brake_released',
                            'reeval_cadence':     'monthly',
                        },
                    ))
                else:
                    signals.append(Signal(
                        ticker            = ticker,
                        direction         = 'FLAT',
                        entry_price       = current_price,
                        stop_loss         = current_price,
                        target_1          = current_price,
                        target_2          = current_price,
                        target_3          = current_price,
                        position_size_pct = 0.0,
                        confidence        = 'HIGH',
                        signal_params     = {
                            'sleeve':            sleeve_name,
                            'sleeve_weight':      weight,
                            'sma_window':         SMA_WINDOW,
                            'trend_score':        round(weighted_score, 6),
                            'trigger':            'brake_engaged',
                            'reeval_cadence':     'monthly',
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

        month_starts = set()
        for (y, m), grp in wide.groupby([wide.index.year, wide.index.month]):
            month_starts.add(grp.index.min())

        rows = []
        for sleeve_name, legs in SLEEVES.items():
            required = set(legs.keys()) | {UNDERLYING_MAP[t] for t in legs}
            if not required.issubset(set(wide.columns)):
                continue

            # Precompute underlying trend scores for every bar.
            under_scores = {}
            for t in legs:
                u = UNDERLYING_MAP[t]
                u_close = wide[u].dropna()
                u_sma = u_close.rolling(SMA_WINDOW, min_periods=SMA_WINDOW).mean()
                under_scores[t] = (u_close - u_sma) / u_sma

            state = None  # True = levered, False = cash
            dates = wide.index
            for i in range(SMA_WINDOW, len(dates) - 1):
                today = dates[i]
                if today not in month_starts:
                    if state is None:
                        continue
                else:
                    weighted = 0.0
                    weight_covered = 0.0
                    ok = True
                    for t, w in legs.items():
                        s = under_scores[t].get(today, float('nan'))
                        if pd.isna(s):
                            ok = False
                            break
                        weighted += w * s
                        weight_covered += w
                    if ok and weight_covered > 0:
                        state = weighted > 0

                if state is None:
                    continue

                next_date = dates[i + 1]
                if state:
                    pnl = sum(
                        w * float(wide[t].pct_change().loc[next_date])
                        for t, w in legs.items()
                        if t in wide.columns and not pd.isna(wide[t].pct_change().loc[next_date])
                    )
                else:
                    pnl = 0.0
                rows.append({
                    'strategy_id':  STRATEGY_ID,
                    'signal_date':  str(next_date.date()),
                    'regime_state': regime_map.get(next_date, 'LOW_VOL'),
                    'pnl':          pnl,
                    'r_multiple':   pnl / 0.02 if pnl else 0.0,
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
