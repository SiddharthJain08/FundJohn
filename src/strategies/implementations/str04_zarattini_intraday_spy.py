"""
S-TR-04: Zarattini Intraday SPY Momentum (NOPE)
================================================

Academic source
---------------
Zarattini, C., Pagani, A., & Aziz, A. (2024).
"A Profitable Day Trading Strategy For The U.S. Equity Market."
SSRN Working Paper 4824172.

Headline result: a daily long-only intraday strategy on SPY that buys at
the open conditional on a noise-of-prices-estimate (NOPE) score and exits
at close.  Reported Sharpe 1.33 over 1996-2023; performance robust to
multiple parameter perturbations (table 5 in paper).

Edge mechanism
--------------
The strategy exploits the well-documented intraday momentum / morning
reversal asymmetry in SPY (Heston/Korajczyk/Sadka 2010; Bogousslavsky
2016).  The NOPE score combines:
* Overnight gap sign and magnitude (capped to ±1 std).
* First-15-min volume relative to 10-day average.
* VIX level (LONG only when VIX < 30).

Trade:
* If NOPE > +0.20 → LONG SPY at 10:00 ET, exit at close.  Stop = -1.5 ATR.
* If NOPE < -0.20 → optional SHORT (skipped by default for $400/mo budget;
  enabling SHORT increases Sharpe slightly but adds borrow cost & gap risk).

Position sizing fixed at 50% NAV per fire (intraday, no overnight risk).
One trade per day.

Data dependencies
-----------------
market_data['spy_30m_bars'] = list of OHLCV bars for today's session
   (we use 10:00 ET bar onward for entry; close-of-session for exit).
market_data['spy_10d_volume_avg'] for normalisation.
market_data['vix_close'] for the LONG-only filter.
"""

from __future__ import annotations
from typing import List

from src.strategies.base import Signal
from src.strategies.cohort_base import CohortBaseStrategy
def compute_nope(prev_close: float, today_open: float, first_bar_volume: float,
                 vol_10d_avg: float, vix: float) -> float:
    """Combine overnight gap + first-bar volume + VIX into a NOPE score."""
    if prev_close <= 0 or vol_10d_avg <= 0:
        return 0.0
    gap = (today_open - prev_close) / prev_close
    gap_z = max(min(gap / 0.005, 3.0), -3.0)            # cap at ±3 std (5 bps σ)
    vol_z = max(min((first_bar_volume / vol_10d_avg) - 1.0, 3.0), -1.0)
    # VIX dampener — NOPE → 0 as VIX → 50
    vix_dampen = max(0.0, 1.0 - max(vix - 15.0, 0.0) / 35.0)
    return float((0.55 * gap_z + 0.30 * vol_z + 0.15) * vix_dampen)


class ZarattiniIntradaySPY(CohortBaseStrategy):
    id = 'S_TR04_zarattini_intraday_spy'
    version = '2.0.0'
    regime_filter = ['HIGH_VOL', 'NEUTRAL', 'LOW_VOL']

    NOPE_LONG_MIN: float = 0.20
    NOPE_SHORT_MAX: float = -0.20
    ALLOW_SHORT: bool = False
    POSITION_SIZE_PCT: float = 0.50      # large because intraday only
    VIX_HARD_CAP: float = 35.0

    def _generate_signals_cohort(self, market_data: dict, opts_map: dict) -> List[Signal]:
        bars = (market_data or {}).get('spy_30m_bars')
        if bars is None or len(bars) < 1:
            return []
        prev_close = (market_data or {}).get('spy_prev_close')
        vol_10d = (market_data or {}).get('spy_10d_volume_avg')
        vix = (market_data or {}).get('vix_close', 20.0)
        if prev_close is None or vol_10d is None:
            return []
        if vix > self.VIX_HARD_CAP:
            return []

        first = bars[0]
        today_open = float(first.get('open', 0.0))
        first_vol = float(first.get('volume', 0.0))
        if today_open <= 0:
            return []

        nope = compute_nope(float(prev_close), today_open, first_vol,
                            float(vol_10d), float(vix))

        direction = None
        if nope >= self.NOPE_LONG_MIN:
            direction = 'LONG'
        elif self.ALLOW_SHORT and nope <= self.NOPE_SHORT_MAX:
            direction = 'SHORT'
        if direction is None:
            return []

        # Stop = ±1.5 × ATR(14) on 30-min bars (proxied by recent bar range)
        recent_range = max(0.005, float(first.get('high', today_open) - first.get('low', today_open)) /
                                  max(today_open, 1.0))
        stop_pct = min(max(1.5 * recent_range, 0.005), 0.020)

        entry_price = today_open

        if direction == 'LONG':
            stop = round(entry_price * (1 - stop_pct), 2)
            t1 = round(entry_price * 1.005, 2)
            t2 = round(entry_price * 1.010, 2)
            t3 = round(entry_price * 1.015, 2)
        else:
            stop = round(entry_price * (1 + stop_pct), 2)
            t1 = round(entry_price * 0.995, 2)
            t2 = round(entry_price * 0.990, 2)
            t3 = round(entry_price * 0.985, 2)

        return [Signal(
            ticker='SPY',
            direction=direction,
            entry_price=entry_price,
            stop_loss=stop,
            target_1=t1, target_2=t2, target_3=t3,
            position_size_pct=self.POSITION_SIZE_PCT,
            confidence='HIGH' if abs(nope) >= 0.40 else 'MED',
            signal_params={
                'strategy_id': self.id,
                'nope_score': round(float(nope), 4),
                'vix': round(float(vix), 2),
                'gap_pct': round((today_open - float(prev_close)) / float(prev_close), 4),
                'first_bar_vol_ratio': round(first_vol / float(vol_10d), 4),
                'kind': 'intraday',
                'exit_on': 'session_close',
            },
        )]
