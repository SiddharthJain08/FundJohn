"""
S_graham_number_value_screen_tv2 — Benjamin Graham's defensive-investor
valuation screen (Luca, R. 2026, "The Graham Number: Formula and How to Use
It"). Interpretation variant 2 of 2.

graham_number    = sqrt(22.5 * trailing_eps * book_value_per_share)
margin_of_safety = (graham_number - price) / graham_number
LONG names trading below the Graham Number within the large-cap universe.

Deliberately different defensible reading vs. variant 1:
  - financials.parquet has no total_assets/total_liabilities fields (only
    revenue, net_income, eps, market_cap, roe, roic, debt_equity_ratio,
    pe_ratio, ...). book_value_per_share is derived from fields that
    actually exist: shares_out = market_cap / price (consistent with how
    market_cap itself is defined), book_equity = net_income / roe (ROE is
    net_income / equity by definition), bvps = book_equity / shares_out.
    This is a different derivation path than variant 1's
    (total_assets - total_liabilities) route.
  - Selection rule uses Graham's own absolute margin-of-safety floor
    (price <= 2/3 of the Graham Number, i.e. margin_of_safety >= 1/3) rather
    than a relative top-decile rank. Graham's defensive-investor method was
    an absolute screen, not a cross-sectional ranking — this is the more
    literal reading of the source.
  - Position sizing is equal-weighted across every name that clears the
    1/3 floor (capped at MAX_SIGNALS, highest margin-of-safety first), not
    tilted toward the cheapest names — Graham's defensive investor
    diversifies across everything that qualifies rather than concentrating.
  - Eligible regimes are LOW_VOL, TRANSITIONING, CRISIS (deep-value names
    that already clear a 1/3 margin of safety are the ones that snap back
    hardest in a post-crash recovery) and exclude HIGH_VOL, where
    quality/growth flight-to-safety typically dominates over statistically
    cheap names — the opposite regime carve-out from variant 1.
"""
from __future__ import annotations
import sys
import math
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal
from src.strategies.universe_default import large_cap as universe_filter

INSTRUMENT_CLASS = 'equity'

__all__ = ['GrahamNumberValueScreenTv2']


class GrahamNumberValueScreenTv2(BaseStrategy):
    """LONG every large-cap name clearing Graham's 1/3 margin-of-safety floor."""

    id          = 'S_graham_number_value_screen_tv2'
    name        = 'GrahamNumberValueScreenTv2'
    description = ('Graham Number defensive value screen (variant 2): LONG large-caps '
                   'priced at or below 2/3 of sqrt(22.5 * trailing EPS * book value/share)')
    tier        = 2
    signal_frequency  = 'quarterly'
    min_lookback      = 1260
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'CRISIS']

    def default_parameters(self):
        return {
            'margin_floor':  1.0 / 3.0,   # Graham's own 2/3-of-fair-value rule
            'min_universe':  40,
            'base_size':     0.04,
        }

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print('[debug] signals=0 (empty prices)', file=sys.stderr)
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print(f'[debug] signals=0 (regime={regime_state} not active)', file=sys.stderr)
            return []
        scale = self.position_scale(regime_state)
        p = self.parameters

        financials = (aux_data or {}).get('financials', {}) or {}
        if not financials:
            print('[debug] signals=0 (no financials)', file=sys.stderr)
            return []

        candidates = []
        for ticker in universe:
            if ticker not in prices.columns:
                continue
            series = prices[ticker].dropna()
            if len(series) < 20:
                continue
            price = float(series.iloc[-1])
            if price <= 0:
                continue
            fin = financials.get(ticker)
            if not fin:
                continue
            eps = fin.get('eps')
            net_income = fin.get('net_income', fin.get('netIncome'))
            market_cap = fin.get('market_cap', fin.get('marketCap'))
            roe = fin.get('roe')
            if eps is None or net_income is None or market_cap is None or roe is None:
                continue
            try:
                eps = float(eps)
                net_income = float(net_income)
                market_cap = float(market_cap)
                roe = float(roe)
            except (TypeError, ValueError):
                continue
            if eps <= 0 or net_income <= 0 or roe <= 0 or market_cap <= 0:
                continue  # Graham's defensive floor: no negative-earnings/equity names
            shares_out = market_cap / price
            if shares_out <= 0:
                continue
            book_equity = net_income / roe
            if book_equity <= 0:
                continue
            bvps = book_equity / shares_out
            if bvps <= 0:
                continue
            graham_number = math.sqrt(22.5 * eps * bvps)
            if graham_number <= 0:
                continue
            margin_of_safety = (graham_number - price) / graham_number
            if margin_of_safety < p['margin_floor']:
                continue  # hasn't cleared Graham's absolute 1/3 safety margin
            candidates.append((ticker, margin_of_safety, price))

        if len(candidates) < p['min_universe']:
            print(f'[debug] signals=0 (candidates={len(candidates)} < min_universe)', file=sys.stderr)
            return []

        candidates.sort(key=lambda x: x[1], reverse=True)
        selected = candidates[:self.MAX_SIGNALS]

        signals: List[Signal] = []
        for ticker, mos, price in selected:
            series = prices[ticker].dropna()
            stops = self.compute_stops_and_targets(series, 'LONG', price, regime_state=regime_state)
            size = min(round(p['base_size'] * scale, 4), 0.06)
            confidence = 'HIGH' if mos >= 0.50 else ('MED' if mos >= 0.40 else 'LOW')
            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=price,
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=size,
                confidence=confidence,
                signal_params={
                    'margin_of_safety': round(mos, 4),
                    'regime': regime_state,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
