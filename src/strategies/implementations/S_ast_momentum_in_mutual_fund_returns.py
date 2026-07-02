"""
Momentum in Mutual Fund Returns — Quantpedia clean-room port.

Universe: ~850 no-load equity mutual funds proxied by a broad ETF basket
covering large/mid/small cap, growth/value, sector, international, and
factor ETFs. Quarterly rebalance at months 3/6/9/12; rank by 6-month
(126-day) return; equally weight top-decile ETFs for the next quarter.

Source: https://quantpedia.com/strategies/momentum-in-mutual-fund-returns/
"""
from __future__ import annotations

import sys
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['MomentumInMutualFundReturns']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID      = 'S_ast_momentum_in_mutual_fund_returns'

# Broad ETF proxy for the ~850 no-load mutual fund universe.
# Covers equity styles, sectors, geographies, factors — most are in prices.parquet.
BASKET = (
    'SPY', 'IVV', 'QQQ', 'DIA',            # large cap broad
    'MDY', 'IJH', 'IWM', 'IJR',            # mid/small cap
    'VTV', 'VUG', 'IWD', 'IWF',            # value / growth
    'EFA', 'EEM', 'VEA', 'VWO',            # international
    'XLK', 'XLF', 'XLV', 'XLE',            # sector ETFs
    'XLI', 'XLB', 'XLY', 'XLP', 'XLU',    # sector ETFs continued
    'VNQ', 'XLRE',                          # real estate
    'GLD', 'SLV', 'USO',                    # commodities
    'TLT', 'IEF', 'AGG', 'HYG', 'LQD',    # fixed income
    'MTUM', 'VLUE', 'QUAL', 'USMV',        # factor ETFs
)

LOOKBACK  = 126   # 6 months of trading days
QUANTILE  = 10    # top decile
STALE_DAYS = 3    # exclude if last price > 3 calendar days old


class MomentumInMutualFundReturns(BaseStrategy):
    """Quarterly ETF-proxy rotation on 6-month momentum, top-decile equal-weight.

    Source: https://quantpedia.com/strategies/momentum-in-mutual-fund-returns/
    """

    id                = STRATEGY_ID
    name              = 'Momentum in Mutual Fund Returns'
    description       = (
        'No-load equity mutual funds with the highest 6-month NAV return continue '
        'to outperform over the next quarter; proxied via a broad ETF basket with '
        'quarterly top-decile equally weighted rotation.'
    )
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = LOOKBACK
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 20

    def default_parameters(self) -> dict:
        return {
            'lookback':    LOOKBACK,
            'quantile':    QUANTILE,
            'stale_days':  STALE_DAYS,
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

        if len(prices) < 2:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Quarterly trigger: fire only on the first trading day of a quarter
        # (months 3, 6, 9, 12). Mirrors the QC reference implementation.
        idx = prices.index
        if not hasattr(idx[-1], 'month') or not hasattr(idx[-2], 'month'):
            print('[debug] signals=0', file=sys.stderr)
            return []

        current_month = idx[-1].month
        prior_month   = idx[-2].month

        # Month boundary crossed and we are in a quarter-end month
        if current_month == prior_month:
            print('[debug] signals=0', file=sys.stderr)
            return []
        if current_month not in (3, 6, 9, 12):
            print('[debug] signals=0', file=sys.stderr)
            return []

        lookback   = int(self.parameters.get('lookback',   LOOKBACK))
        quantile   = int(self.parameters.get('quantile',   QUANTILE))
        stale_days = int(self.parameters.get('stale_days', STALE_DAYS))

        if len(prices) < lookback:
            print('[debug] signals=0', file=sys.stderr)
            return []

        last_date = idx[-1]

        # Compute 6-month return for each basket member present in the panel.
        performance: dict[str, float] = {}
        for ticker in BASKET:
            if ticker not in prices.columns:
                continue
            series = prices[ticker].dropna()
            if len(series) < lookback:
                continue

            # Staleness filter: last valid price must be within stale_days calendar days.
            try:
                last_valid_idx = series.index[-1]
                delta = (last_date - last_valid_idx).days
            except Exception:
                continue
            if delta > stale_days:
                continue

            try:
                ret = float(series.iloc[-1]) / float(series.iloc[-lookback]) - 1.0
            except (ZeroDivisionError, ValueError):
                continue
            performance[ticker] = ret

        if len(performance) < quantile:
            # Not enough instruments to form a meaningful decile
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Select top decile (round down as reference does)
        decile_size = max(1, int(len(performance) / quantile))
        sorted_perf = sorted(performance.items(), key=lambda x: x[1], reverse=True)
        top_long    = sorted_perf[:decile_size]

        scale    = self.position_scale(regime_state)
        pos_frac = round(1.0 / len(top_long), 6)

        signals: List[Signal] = []
        for rank_idx, (ticker, ret) in enumerate(top_long):
            series = prices[ticker].dropna()
            if len(series) < 14:
                continue
            current_price = float(series.iloc[-1])

            stops = self.compute_stops_and_targets(
                series,
                direction='LONG',
                current_price=current_price,
                regime_state=regime_state,
            )

            if ret > 0.20:
                confidence = 'HIGH'
            elif ret > 0.08:
                confidence = 'MED'
            else:
                confidence = 'LOW'

            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = current_price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = round(pos_frac * scale, 4),
                confidence        = confidence,
                signal_params     = {
                    'ret_6m':       round(ret, 4),
                    'regime':       regime_state,
                    'scale':        scale,
                    'rank':         rank_idx + 1,
                    'decile_size':  decile_size,
                    'n_evaluated':  len(performance),
                    'rebalance':    True,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
