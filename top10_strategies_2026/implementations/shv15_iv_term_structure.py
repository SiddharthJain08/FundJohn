"""
S-HV15: IV Term-Structure Inversion / Slope Trade
==================================================

Academic source
---------------
Johnson, T. (2017). "Risk Premia and the VIX Term Structure."
Journal of Financial and Quantitative Analysis, 52(6), 2461-2490.

Corroborated by:
* Dew-Becker, I. et al. (2017). "The Price of Variance Risk."
  Journal of Financial Economics, 123(2), 225-250.
* Egloff, D., Leippold, M., & Wu, L. (2010). "The Term Structure of
  Variance Swap Rates and Optimal Variance Swap Investments."
  Journal of Financial and Quantitative Analysis, 45(5), 1279-1310.

Edge mechanism
--------------
A normal options term structure is upward sloping (longer-dated IV ≥ short-dated).
When the curve inverts (front-month IV > deferred-month IV), short-dated fear has
spiked relative to background uncertainty.  Two well-documented effects:

  (a) When curve INVERTS (ts_ratio = iv_30d / iv_90d > 1.05): markets
      typically mean-revert in 5-15 trading days as front-month IV
      decays.  The signal is BUY_VOL_SHORT_DATED only if the inversion
      is mild (< 1.10) and confirmed by VIX < 30; otherwise the
      inversion is "real" (genuine crisis) and we sit out.

  (b) When curve is steeply CONTANGO (ts_ratio < 0.85): variance risk
      premium is rich, so SELL_VOL via short-vol position on
      single-name underlying.  Cremers et al. (2021) show carry trade
      earns 8-12 % annualised gross of slippage.

Signal logic
-----------
For each ticker:
1. iv_30d  = mean ATM IV at expiry closest to 30 DTE (filter |delta| 0.40-0.60).
2. iv_90d  = mean ATM IV at expiry closest to 90 DTE.
3. ts_ratio = iv_30d / iv_90d.

Trade rules:
* ts_ratio > 1.05  AND VIX < 30  AND regime != CRISIS → LONG underlying
  (front-month panic decays → spot rallies).
* ts_ratio < 0.85  AND iv_rank > 60                  → SHORT vol via SELL_VOL
  (high carry, low realised vol → harvest VRP).

Hold 7 trading days.  Cap to top 6 longs / 6 shorts to control concentration.

Data dependencies
-----------------
opts_map[ticker] must have:
    - ts_ratio (computed in engine.py aux loader)
    - iv_30d, iv_90d
    - last_price
    - iv_rank
market_data['vix_index'] must have last close.
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


class IVTermStructureSlope(BaseStrategy):
    id = 'S_HV15_iv_term_structure'
    version = '2.0.0'
    regime_filter = ['HIGH_VOL', 'NEUTRAL', 'LOW_VOL']

    TS_INVERTED_MIN: float = 1.05
    TS_INVERTED_MAX: float = 1.15      # above this → likely real crisis, skip
    TS_CONTANGO_MAX: float = 0.85
    VIX_INVERSION_CAP: float = 30.0
    HOLD_DAYS: int = 7
    TOP_N_PER_SIDE: int = 6

    def generate_signals(self, market_data: dict, opts_map: dict) -> List[Signal]:
        regime = (market_data or {}).get('regime', {}).get('label', 'NEUTRAL')
        if regime == 'CRISIS':
            return []
        vix = float((market_data or {}).get('vix_index', {}).get('close', 20.0))

        long_cands, short_cands = [], []
        for ticker, opts in opts_map.items():
            ts = opts.get('ts_ratio')
            price = opts.get('last_price')
            if ts is None or price is None or price <= 0:
                continue
            iv_rank = float(opts.get('iv_rank') or 50.0)
            iv_30d = opts.get('iv_30d')

            if (self.TS_INVERTED_MIN <= ts <= self.TS_INVERTED_MAX
                and vix < self.VIX_INVERSION_CAP):
                # Mean reversion long
                long_cands.append((
                    (ts - 1.0) * (1.0 + iv_rank / 200.0),
                    ticker, 'LONG', ts, iv_30d, iv_rank, price, opts,
                ))
            elif ts <= self.TS_CONTANGO_MAX and iv_rank >= 60:
                # Vol-rich → SELL_VOL on the underlying via short-vol
                short_cands.append((
                    (1.0 - ts) * (1.0 + iv_rank / 200.0),
                    ticker, 'SELL_VOL', ts, iv_30d, iv_rank, price, opts,
                ))

        long_cands.sort(key=lambda x: x[0], reverse=True)
        short_cands.sort(key=lambda x: x[0], reverse=True)
        signals: List[Signal] = []

        for cand_list in (long_cands[:self.TOP_N_PER_SIDE],
                          short_cands[:self.TOP_N_PER_SIDE]):
            for score, ticker, direction, ts, iv_30d, iv_rank, price, opts in cand_list:
                size = min(0.012 + 0.012 * min(abs(ts - 1.0) / 0.10, 1.5), 0.035)
                confidence = 'HIGH' if (abs(ts - 1.0) >= 0.10 and iv_rank >= 70) else 'MED'

                if direction == 'LONG':
                    stop = round(price * 0.94, 2)
                    t1 = round(price * 1.03, 2)
                    t2 = round(price * 1.05, 2)
                    t3 = round(price * 1.08, 2)
                else:  # SELL_VOL — modeled as short underlying for backtest
                    stop = round(price * 1.06, 2)
                    t1 = round(price * 0.97, 2)
                    t2 = round(price * 0.95, 2)
                    t3 = round(price * 0.92, 2)

                signals.append(Signal(
                    ticker=ticker, direction=direction,
                    entry_price=price, stop_loss=stop,
                    target_1=t1, target_2=t2, target_3=t3,
                    position_size_pct=round(size, 4),
                    confidence=confidence,
                    signal_params={
                        'strategy_id': self.id,
                        'ts_ratio': round(float(ts), 4),
                        'iv_30d': round(float(iv_30d) if iv_30d is not None else 0.0, 4),
                        'iv_rank': round(iv_rank, 2),
                        'vix': round(vix, 2),
                        'regime_at_entry': regime,
                        'hold_days': self.HOLD_DAYS,
                    },
                ))
        return signals
