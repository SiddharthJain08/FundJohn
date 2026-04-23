"""
S-HV13: Call-Put IV Spread (Put-Call Parity Deviation)
=======================================================

Academic source
---------------
Cremers, M. and Weinbaum, D. (2010).
"Deviations from Put-Call Parity and Stock Return Predictability."
Journal of Financial and Quantitative Analysis, 45(2), 335-367.

Corroborated by Bali, T.G. and Hovakimian, A. (2009),
"Volatility Spreads and Expected Stock Returns," Management Science 55(11).

Edge mechanism
--------------
Informed traders prefer options for leverage and anonymity.  When they have
positive information they buy *calls*; their demand pushes call IV above the
matched-strike, matched-expiry put IV.  Theoretical put-call parity says these
two IVs should be equal — any deviation is informational.

Cremers & Weinbaum's headline result: the long-short quintile portfolio formed
on `iv_spread = iv_call_atm - iv_put_atm` earns ~50 bps/week.  Effect persists
1-4 weeks; strongest on small-cap optionable names with high option volume.

Signal logic (production code below)
-----------------------------------
1. For each ticker, isolate near-ATM call/put pairs (|delta| 0.40-0.60) at the
   nearest expiry within [10, 45] DTE.
2. Match call/put pairs at identical strike + expiry.
3. Compute weighted (by open_interest) average iv_spread.
4. Rank cross-sectionally; top decile by spread → LONG, bottom decile → SHORT.
5. Optional regime gate: in HIGH_VOL widen the |spread| threshold to control
   noise.

Data dependencies
-----------------
opts_map[ticker] must have:
    - iv_spread_atm_oi_weighted (pre-computed in engine.py aux loader)
    - last_price
    - iv_rank (for confidence scaling)
    - dte_used (the nominal DTE used for the iv_spread computation)

prices_df (in market_data['prices']) is used to compute 21-day realised vol
for stop-loss sizing.

Risk controls
-------------
* Effect partially decays after 4 weeks → re-rebalance weekly.
* Slippage on illiquid options can erode edge → require min OI > 100 in
  engine.py aux loader before reporting iv_spread.
* In CRISIS regime: skip — informed-trader signal noisier when market-wide
  fear dominates.
"""

from __future__ import annotations

from typing import List

from src.strategies.base import Signal
from src.strategies.cohort_base import CohortBaseStrategy
class CallPutIVSpread(CohortBaseStrategy):
    id = 'S_HV13_call_put_iv_spread_cohort2026'
    version = '2.1.0'
    regime_filter = ['HIGH_VOL', 'NEUTRAL', 'LOW_VOL']

    # Thresholds
    SPREAD_LONG_MIN: float = 0.025          # +2.5 IV pts → LONG
    SPREAD_SHORT_MAX: float = -0.025        # -2.5 IV pts → SHORT
    SPREAD_HIGH_VOL_BUFFER: float = 0.015   # widen by +1.5 IV pts in HIGH_VOL
    HOLD_DAYS: int = 7                      # ~1 week — re-rebalance weekly
    TOP_N: int = 10                         # 5 long + 5 short

    def _generate_signals_cohort(self, market_data: dict, opts_map: dict) -> List[Signal]:
        regime = (market_data or {}).get('regime', {}).get('label', 'NEUTRAL')
        spread_long = self.SPREAD_LONG_MIN
        spread_short = self.SPREAD_SHORT_MAX
        if regime == 'HIGH_VOL':
            spread_long += self.SPREAD_HIGH_VOL_BUFFER
            spread_short -= self.SPREAD_HIGH_VOL_BUFFER

        candidates: list[tuple] = []

        for ticker, opts in opts_map.items():
            spread = opts.get('iv_spread_atm_oi_weighted')
            if spread is None:
                # Fallback to plain iv_spread if aux loader hasn't been updated yet
                spread = opts.get('iv_spread')
            if spread is None:
                continue

            price = opts.get('last_price')
            if price is None or price <= 0:
                continue

            iv_rank = float(opts.get('iv_rank') or 50.0)

            direction = None
            score = 0.0
            if spread >= spread_long:
                direction = 'LONG'
                score = spread * (1.0 + iv_rank / 100.0)
            elif spread <= spread_short:
                direction = 'SHORT'
                score = abs(spread) * (1.0 + iv_rank / 100.0)

            if direction:
                candidates.append((score, ticker, direction, spread, iv_rank,
                                   price, opts))

        candidates.sort(key=lambda x: x[0], reverse=True)

        signals: List[Signal] = []
        long_count, short_count = 0, 0
        per_side = self.TOP_N // 2

        for score, ticker, direction, spread, iv_rank, price, opts in candidates:
            if direction == 'LONG' and long_count >= per_side:
                continue
            if direction == 'SHORT' and short_count >= per_side:
                continue

            # Position size: scale by spread magnitude up to a cap
            scale = min(abs(spread) / 0.04, 2.0)
            size = min(0.012 + 0.010 * scale, 0.04)

            confidence = 'HIGH' if abs(spread) >= 0.05 and iv_rank >= 60 else 'MED'

            # Stops sized to recent realised vol if available, else 7%
            rv20 = opts.get('rv20')
            if rv20 is not None and rv20 > 0:
                stop_pct = min(max(0.5 * rv20 / (252 ** 0.5) * (self.HOLD_DAYS ** 0.5),
                                   0.03), 0.10)
            else:
                stop_pct = 0.07

            if direction == 'LONG':
                stop = round(price * (1 - stop_pct), 2)
                t1 = round(price * (1 + 0.04), 2)
                t2 = round(price * (1 + 0.07), 2)
                t3 = round(price * (1 + 0.12), 2)
            else:
                stop = round(price * (1 + stop_pct), 2)
                t1 = round(price * (1 - 0.04), 2)
                t2 = round(price * (1 - 0.07), 2)
                t3 = round(price * (1 - 0.12), 2)

            signals.append(Signal(
                ticker=ticker,
                direction=direction,
                entry_price=price,
                stop_loss=stop,
                target_1=t1, target_2=t2, target_3=t3,
                position_size_pct=round(size, 4),
                confidence=confidence,
                signal_params={
                    'strategy_id': self.id,
                    'iv_spread': round(spread, 4),
                    'iv_rank': round(iv_rank, 2),
                    'regime_at_entry': regime,
                    'spread_threshold_used': spread_long if direction == 'LONG'
                                              else spread_short,
                    'hold_days': self.HOLD_DAYS,
                    'score': round(score, 4),
                },
            ))
            if direction == 'LONG':
                long_count += 1
            else:
                short_count += 1

        return signals
