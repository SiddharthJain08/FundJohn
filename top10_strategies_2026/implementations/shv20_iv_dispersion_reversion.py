"""
S-HV20: IV Dispersion / Index-Component Mean Reversion
=======================================================

Academic source
---------------
Driessen, J., Maenhout, P. J., & Vilkov, G. (2009).
"The Price of Correlation Risk: Evidence from Equity Options."
The Journal of Finance, 64(3), 1377-1406.

Corroborated by:
* Cosemans, M. (2011). "The Pricing of Long and Short Run Variance and
  Correlation Risk in Stock Returns." University of Amsterdam working paper.
* Buss, A. & Vilkov, G. (2012). "Measuring Equity Risk with Option-Implied
  Correlations."  Review of Financial Studies, 25(10), 3113-3140.

Edge mechanism
--------------
When option-implied correlation between index and components is unusually
high (e.g., near 1), index variance is over-priced relative to component
variance.  The classic dispersion trade is: short index volatility, long
component volatilities, in proportions that approximately neutralise vega.

A simpler proxy that we can compute daily without a full vega match:

    iv_dispersion = mean_component_atm_iv / spx_atm_iv

When iv_dispersion drops below the 10th percentile of its trailing
252-day distribution, the index variance is rich → SELL_VOL on SPY.
When iv_dispersion exceeds the 90th percentile, the basket variance is
rich → SELL_VOL on a representative basket of high-IV components.

Hold ~10 trading days, exit when iv_dispersion mean-reverts to its
trailing median or hits stop.

Signal logic
-----------
1. Engine pre-computes:
       opts_map['SPY']['atm_iv_30d']
       opts_map[ticker]['atm_iv_30d'] for each S&P 100 component
       market_data['iv_dispersion_today']
       market_data['iv_dispersion_pct_252d']  (percentile in trailing window)

2. Trigger:
   - iv_dispersion_pct_252d < 10  → SELL_VOL on SPY (1.5 % NAV)
   - iv_dispersion_pct_252d > 90  → SELL_VOL basket of top-5 highest-IV
                                    components (0.5 % NAV each, capped 2.5 %)

3. Skip CRISIS regime — dispersion signals very noisy in panics.

Risk controls
-------------
* Concurrent total SELL_VOL exposure across SPY + basket capped at 4 % NAV.
* Hard stop on SPY position if VIX spikes > 25 % intraday.
"""

from __future__ import annotations
from typing import List

try:
    from ..base_strategy import BaseStrategy
    from ...models.signal import Signal
except (ImportError, ValueError):
    try:
        from ._compat import BaseStrategy, Signal
    except (ImportError, ValueError):
        import sys as _sys, os as _os
        _sys.path.insert(0, _os.path.dirname(__file__))
        from _compat import BaseStrategy, Signal


class IVDispersionReversion(BaseStrategy):
    id = 'S_HV20_iv_dispersion_reversion'
    version = '2.0.0'
    regime_filter = ['HIGH_VOL', 'NEUTRAL', 'LOW_VOL']

    LOW_PCT: float = 10.0
    HIGH_PCT: float = 90.0
    HOLD_DAYS: int = 10
    BASKET_TOP_N: int = 5

    def generate_signals(self, market_data: dict, opts_map: dict) -> List[Signal]:
        regime = (market_data or {}).get('regime', {}).get('label', 'NEUTRAL')
        if regime == 'CRISIS':
            return []

        disp_pct = (market_data or {}).get('iv_dispersion_pct_252d')
        if disp_pct is None:
            return []

        signals: List[Signal] = []

        # Branch 1: SPY index over-priced → short SPY vol
        if disp_pct < self.LOW_PCT and 'SPY' in opts_map:
            spy = opts_map['SPY']
            price = spy.get('last_price')
            iv_30d = spy.get('atm_iv_30d') or spy.get('iv_30d')
            if price and price > 0 and iv_30d:
                signals.append(self._mk_sell_vol(
                    ticker='SPY', price=price,
                    pct_nav=0.015, iv=iv_30d,
                    note=f'index_vol_rich_pct{disp_pct:.1f}',
                    regime=regime,
                ))

        # Branch 2: Component vols rich → short top-N component vols
        elif disp_pct > self.HIGH_PCT:
            comp = []
            for tk, opts in opts_map.items():
                if tk == 'SPY':
                    continue
                iv30 = opts.get('atm_iv_30d') or opts.get('iv_30d')
                price = opts.get('last_price')
                if iv30 and price and price > 0:
                    comp.append((iv30, tk, price))
            comp.sort(key=lambda x: x[0], reverse=True)
            for iv30, tk, price in comp[:self.BASKET_TOP_N]:
                signals.append(self._mk_sell_vol(
                    ticker=tk, price=price,
                    pct_nav=0.005, iv=iv30,
                    note=f'comp_vol_rich_pct{disp_pct:.1f}',
                    regime=regime,
                ))

        return signals

    def _mk_sell_vol(self, ticker: str, price: float, pct_nav: float,
                     iv: float, note: str, regime: str) -> Signal:
        # Stops on the underlying — protect against gap risk
        stop = round(price * 1.04, 2)
        t1 = round(price * 0.985, 2)
        t2 = round(price * 0.97, 2)
        t3 = round(price * 0.95, 2)
        return Signal(
            ticker=ticker, direction='SELL_VOL',
            entry_price=price, stop_loss=stop,
            target_1=t1, target_2=t2, target_3=t3,
            position_size_pct=round(pct_nav, 4),
            confidence='HIGH' if pct_nav >= 0.012 else 'MED',
            signal_params={
                'strategy_id': self.id,
                'iv_30d': round(float(iv), 4),
                'note': note,
                'regime_at_entry': regime,
                'hold_days': self.HOLD_DAYS,
            },
        )
