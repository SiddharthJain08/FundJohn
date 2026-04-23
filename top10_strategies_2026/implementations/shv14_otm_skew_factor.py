"""
S-HV14: OTM Put Skew Factor
============================

Academic source
---------------
Xing, Y., Zhang, X., and Zhao, R. (2010).
"What Does the Individual Option Volatility Smirk Tell Us About Future
Equity Returns?" Journal of Financial and Quantitative Analysis, 45(3),
641-662.

Edge mechanism
--------------
Define `smirk = iv(OTM put, |delta|≈0.20) − iv(ATM call, |delta|≈0.50)`.
A wide smirk signals informed traders are paying up for downside protection;
underlying tends to underperform over the next 1-4 weeks.

Headline result: long-short quintile portfolio sorted on smirk earns ~18 bps/week
(≈10%/yr gross).  Persists ~6 weeks, strongest on optionable mid-caps.

Signal logic
-----------
1. For each ticker compute smirk on the nearest expiry in [20, 60] DTE.
   - Put leg: |delta| in [0.18, 0.22]
   - Call leg: |delta| in [0.45, 0.55]
2. Cross-sectional rank.  Top decile (wide smirk) → SHORT, bottom decile → LONG.
3. Confidence boosted in HIGH_VOL (skew has more information when fear elevated)
   but capped to avoid right-tail tracking error.
4. Hold 10 trading days, with rebalance every 5 days.

Data dependencies
-----------------
opts_map[ticker] must have:
    - smirk_otmput_atmcall (computed in engine.py aux loader)
    - last_price
    - iv_rank
    - rv20 (optional, for stop sizing)

Risk controls
-------------
* Skip CRISIS regime (extreme smirks dominated by hedging demand, not info).
* Position cap 3% per name.
* Smirk must come from options with min OI ≥ 100 on each leg (enforced upstream).
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


class OTMSkewFactor(BaseStrategy):
    id = 'S_HV14_otm_skew_factor'
    version = '2.0.0'
    regime_filter = ['HIGH_VOL', 'NEUTRAL', 'LOW_VOL']

    SMIRK_SHORT_MIN: float = 0.05    # +5 IV pts → SHORT (informed bearish)
    SMIRK_LONG_MAX: float = 0.005    # ≤+0.5 IV pts → LONG (under-priced downside)
    HOLD_DAYS: int = 10
    TOP_N: int = 10                  # 5L + 5S

    def generate_signals(self, market_data: dict, opts_map: dict) -> List[Signal]:
        regime = (market_data or {}).get('regime', {}).get('label', 'NEUTRAL')
        if regime == 'CRISIS':
            return []

        candidates = []
        for ticker, opts in opts_map.items():
            smirk = opts.get('smirk_otmput_atmcall')
            if smirk is None:
                continue
            price = opts.get('last_price')
            if price is None or price <= 0:
                continue
            iv_rank = float(opts.get('iv_rank') or 50.0)

            direction = None
            if smirk >= self.SMIRK_SHORT_MIN:
                direction = 'SHORT'
                score = smirk * (1.0 + iv_rank / 200.0)
            elif smirk <= self.SMIRK_LONG_MAX:
                direction = 'LONG'
                # Wider negative smirk = stronger signal
                score = (self.SMIRK_LONG_MAX - smirk + 0.005) * (1.0 + iv_rank / 200.0)
            else:
                continue

            candidates.append((score, ticker, direction, smirk, iv_rank, price, opts))

        candidates.sort(key=lambda x: x[0], reverse=True)
        per_side = self.TOP_N // 2
        long_n, short_n = 0, 0
        signals: List[Signal] = []

        for score, ticker, direction, smirk, iv_rank, price, opts in candidates:
            if direction == 'LONG' and long_n >= per_side:
                continue
            if direction == 'SHORT' and short_n >= per_side:
                continue

            scale = min(abs(smirk) / 0.08, 2.0)
            size = min(0.010 + 0.008 * scale, 0.03)
            confidence = 'HIGH' if abs(smirk) >= 0.08 and iv_rank >= 60 else 'MED'

            rv20 = opts.get('rv20')
            if rv20 is not None and rv20 > 0:
                stop_pct = min(max(0.5 * rv20 / (252 ** 0.5) * (self.HOLD_DAYS ** 0.5),
                                   0.04), 0.10)
            else:
                stop_pct = 0.07

            if direction == 'LONG':
                stop = round(price * (1 - stop_pct), 2)
                t1 = round(price * 1.04, 2)
                t2 = round(price * 1.07, 2)
                t3 = round(price * 1.12, 2)
            else:
                stop = round(price * (1 + stop_pct), 2)
                t1 = round(price * 0.96, 2)
                t2 = round(price * 0.93, 2)
                t3 = round(price * 0.88, 2)

            signals.append(Signal(
                ticker=ticker, direction=direction,
                entry_price=price, stop_loss=stop,
                target_1=t1, target_2=t2, target_3=t3,
                position_size_pct=round(size, 4),
                confidence=confidence,
                signal_params={
                    'strategy_id': self.id,
                    'smirk': round(smirk, 4),
                    'iv_rank': round(iv_rank, 2),
                    'regime_at_entry': regime,
                    'hold_days': self.HOLD_DAYS,
                    'score': round(score, 4),
                },
            ))
            if direction == 'LONG':
                long_n += 1
            else:
                short_n += 1
        return signals
