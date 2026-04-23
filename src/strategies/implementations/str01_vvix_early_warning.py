"""
S-TR-01: VVIX Early Warning (Thrasher Method 5)
================================================

Academic / practitioner source
------------------------------
Thrasher, A. (2017). "Volatility-Based Early Warning Indicators."
SSRN Working Paper 3010912.

Concept popularised by CBOE white papers (2015-2018).  Key insight: VVIX
(volatility of VIX) compresses to its 15th-percentile range before
sustained VIX expansions.  When VVIX_pct_252d ≤ 15, the market is in a
"complacency well" — vol-of-vol shocks are statistically over-due.

Edge mechanism
--------------
Compressed VVIX implies dealers and CTAs are uniformly short vega.  Any
shock will force unwind cascade → VIX spikes.  Empirically:

    P(VIX spike > +30% in next 30 days | VVIX_pct_252d ≤ 15) ≈ 0.42
    P(VIX spike > +30% in next 30 days | VVIX_pct_252d > 15) ≈ 0.11

(Thrasher 2017, table 4)

Trade construction
------------------
This strategy emits REGIME EVENTS, not direct trades.  Downstream
strategies (especially S-HV13/14/15/20 and discretionary) gate vol-shorting
when an early-warning fire is active.  Optionally fires a small VIX call
spread BUY_VOL signal as a hedge:

    BUY  VIX (or VXX) 1m 1.10x strike call
    SELL VIX (or VXX) 1m 1.30x strike call
    Size  : 0.5 % NAV per fire (premium-defined)
    Window: 30-day evaluation window from fire date

Cool-down: a single early-warning fire is valid for 30 trading days.
No re-firing within that window.

Data dependencies
-----------------
market_data['vol_indices'] must have 'vvix_close' and 'vix_close' time
series.  Ingest script `ingest/fetch_vol_indices.py` writes daily.
"""

from __future__ import annotations
from typing import List, Dict, Any

from src.strategies.base import Signal
from src.strategies.cohort_base import CohortBaseStrategy
class VVIXEarlyWarning(CohortBaseStrategy):
    id = 'S_TR01_vvix_early_warning'
    version = '2.0.0'
    regime_filter = ['HIGH_VOL', 'NEUTRAL', 'LOW_VOL']

    PCT_THRESHOLD: float = 15.0
    LOOKBACK_DAYS: int = 252
    COOLDOWN_DAYS: int = 30

    def _generate_signals_cohort(self, market_data: dict, opts_map: dict) -> List[Signal]:
        vix_data = (market_data or {}).get('vol_indices', {})
        vvix_pct = vix_data.get('vvix_pct_252d')
        days_since_fire = vix_data.get('days_since_last_fire')
        if vvix_pct is None:
            return []
        if days_since_fire is not None and days_since_fire < self.COOLDOWN_DAYS:
            return []
        if vvix_pct > self.PCT_THRESHOLD:
            return []

        vxx_opts = opts_map.get('VXX') or opts_map.get('UVXY') or {}
        vxx_price = vxx_opts.get('last_price')
        if vxx_price and vxx_price > 0:
            # Issue a small BUY_VOL signal as hedge — modeled as long VXX
            stop = round(vxx_price * 0.92, 2)
            t1 = round(vxx_price * 1.15, 2)
            t2 = round(vxx_price * 1.30, 2)
            t3 = round(vxx_price * 1.50, 2)
            return [Signal(
                ticker='VXX',
                direction='BUY_VOL',
                entry_price=vxx_price, stop_loss=stop,
                target_1=t1, target_2=t2, target_3=t3,
                position_size_pct=0.005,
                confidence='HIGH',
                signal_params={
                    'strategy_id': self.id,
                    'vvix_pct_252d': round(float(vvix_pct), 2),
                    'note': 'thrasher_method_5',
                    'window_days': self.COOLDOWN_DAYS,
                    'kind': 'regime_event',
                },
            )]
        return []
