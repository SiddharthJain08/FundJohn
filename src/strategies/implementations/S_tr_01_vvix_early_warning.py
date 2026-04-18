from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal

__all__ = ['VVIXEarlyWarning']

# SPY proxy: first available of these tickers used as the equity hedge target
_SPY_PROXIES = ['SPY', 'IVV', 'VOO']

# Volatility-of-VIX signal thresholds (Whaley 2009, Avellaneda & Cont 2010)
VVIX_SPIKE_THRESH   = 100.0   # VVIX > 100 → VIX spike imminent (high fear of fear)
VVIX_EXTREME_THRESH = 115.0   # VVIX > 115 → extreme / sell-vol on pullback
VIX_ELEVATED_THRESH = 20.0    # VIX must also be elevated for signal to fire
VIX_SPIKE_THRESH    = 25.0    # VIX > 25 on a spike day → post-spike mean reversion

# Z-score gate: VVIX z-score vs trailing 63-day window
VVIX_Z_THRESH  = 1.5
LOOKBACK_ZWIN  = 63


def _zscore(series: pd.Series, window: int, min_periods: int = 10) -> pd.Series:
    mu  = series.rolling(window, min_periods=min_periods).mean()
    sig = series.rolling(window, min_periods=min_periods).std()
    return (series - mu) / sig.replace(0, np.nan)


class VVIXEarlyWarning(BaseStrategy):
    """
    VVIX-conditioned VIX spike early warning.

    Signal logic (Whaley 2009 — "Investor Fear Gauge"):
    1. VVIX > 100 AND VVIX z-score (63d) > 1.5 → fear-of-fear spike imminent
       → SELL_VOL on SPY (vol short) + SHORT SPY equity hedge
    2. VVIX > 115 (extreme) → scale down to MED conviction (reversion likely)
       → SELL_VOL only, no equity SHORT
    3. VIX spike reversal (VIX > 25 → 1-day SELL_VOL contrarian):
       → if VIX today > 25 and VVIX starts mean-reverting (z-score declining)
         → BUY_VOL signal on post-spike bounce

    Data requirement: aux_data['macro']['VIX'] and aux_data['macro']['VVIX'].
    Falls back to FLAT if macro data unavailable.
    """

    id                = 'S_tr_01_vvix_early_warning'
    name              = 'VVIXEarlyWarning'
    description       = 'VVIX-conditioned VIX-spike early warning — SELL_VOL + hedge in TRANSITIONING regime'
    tier              = 1
    active_in_regimes = ['TRANSITIONING']
    min_lookback      = LOOKBACK_ZWIN + 5

    BASE_SIZE_PCT = 0.020

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []

        # Require macro data
        macro = (aux_data or {}).get('macro', {})
        vvix_series: pd.Series | None = macro.get('VVIX')
        vix_series:  pd.Series | None = macro.get('VIX')

        if vvix_series is None or len(vvix_series) < self.min_lookback:
            print(f'[debug] {self.id}: signals=0 (VVIX data unavailable or too short)', file=sys.stderr)
            return []

        # Find SPY proxy in universe
        spy_ticker = next((t for t in _SPY_PROXIES if t in universe and t in prices.columns), None)
        if spy_ticker is None:
            print(f'[debug] {self.id}: signals=0 (no SPY proxy in universe)', file=sys.stderr)
            return []

        # Align macro to price dates
        price_dates = prices.index
        vvix = vvix_series.reindex(price_dates, method='ffill').dropna()
        vix  = vix_series.reindex(price_dates, method='ffill').dropna() if vix_series is not None else None

        if len(vvix) < self.min_lookback:
            return []

        vvix_z    = _zscore(vvix, LOOKBACK_ZWIN)
        vvix_now  = float(vvix.iloc[-1])
        vvix_z_now = float(vvix_z.iloc[-1]) if not pd.isna(vvix_z.iloc[-1]) else 0.0
        vix_now   = float(vix.iloc[-1]) if vix is not None and len(vix) > 0 else 0.0

        spy_price = float(prices[spy_ticker].iloc[-1])
        if spy_price <= 0:
            return []

        scale  = self.position_scale(regime_state)
        signals: List[Signal] = []

        # ── Signal 1: VVIX spike warning → vol short + equity hedge ──────────
        if vvix_now > VVIX_SPIKE_THRESH and vvix_z_now > VVIX_Z_THRESH and vix_now > VIX_ELEVATED_THRESH:
            # SELL_VOL: sell implied vol (straddle/strangle short) on SPY
            size_vol = float(self.BASE_SIZE_PCT * scale)
            size_vol = max(0.005, min(size_vol, 0.06))

            confidence = 'HIGH' if vvix_now > VVIX_EXTREME_THRESH else 'MED'

            st = self.compute_stops_and_targets(
                prices[spy_ticker].dropna(), 'SHORT', spy_price, regime_state=regime_state,
            )
            # SELL_VOL: use SPY as underlying proxy, stop/target from equity move
            signals.append(Signal(
                ticker            = spy_ticker,
                direction         = 'SELL_VOL',
                entry_price       = round(spy_price, 4),
                stop_loss         = st['stop'],
                target_1          = st['t1'],
                target_2          = st['t2'],
                target_3          = st['t3'],
                position_size_pct = size_vol,
                confidence        = confidence,
                signal_params     = {
                    'vvix':        round(vvix_now, 2),
                    'vvix_z':      round(vvix_z_now, 3),
                    'vix':         round(vix_now, 2),
                    'signal_type': 'vvix_spike_warning',
                },
            ))

            # Equity SHORT hedge (only below extreme — at extreme, reversion likely)
            if vvix_now < VVIX_EXTREME_THRESH:
                size_eq = float(self.BASE_SIZE_PCT * 0.5 * scale)
                size_eq = max(0.003, min(size_eq, 0.03))
                signals.append(Signal(
                    ticker            = spy_ticker,
                    direction         = 'SHORT',
                    entry_price       = round(spy_price, 4),
                    stop_loss         = st['stop'],
                    target_1          = st['t1'],
                    target_2          = st['t2'],
                    target_3          = st['t3'],
                    position_size_pct = size_eq,
                    confidence        = 'LOW',
                    signal_params     = {
                        'vvix':        round(vvix_now, 2),
                        'vvix_z':      round(vvix_z_now, 3),
                        'signal_type': 'vvix_equity_hedge',
                    },
                ))

        # ── Signal 2: Post-spike BUY_VOL contrarian ───────────────────────────
        # VIX already elevated + VVIX rolling over (z-score falling from peak)
        elif (vix_now > VIX_SPIKE_THRESH
              and vvix_now > VVIX_SPIKE_THRESH
              and len(vvix_z) >= 3
              and float(vvix_z.iloc[-2]) > vvix_z_now > 0.5):
            size_bvol = float(self.BASE_SIZE_PCT * 0.75 * scale)
            size_bvol = max(0.005, min(size_bvol, 0.04))

            st_long = self.compute_stops_and_targets(
                prices[spy_ticker].dropna(), 'LONG', spy_price, regime_state=regime_state,
            )
            signals.append(Signal(
                ticker            = spy_ticker,
                direction         = 'BUY_VOL',
                entry_price       = round(spy_price, 4),
                stop_loss         = st_long['stop'],
                target_1          = st_long['t1'],
                target_2          = st_long['t2'],
                target_3          = st_long['t3'],
                position_size_pct = size_bvol,
                confidence        = 'MED',
                signal_params     = {
                    'vvix':        round(vvix_now, 2),
                    'vvix_z':      round(vvix_z_now, 3),
                    'vix':         round(vix_now, 2),
                    'signal_type': 'post_spike_reversal',
                },
            ))

        print(f'[debug] {self.id}: signals={len(signals)} '
              f'vvix={vvix_now:.1f} z={vvix_z_now:.2f} vix={vix_now:.1f}', file=sys.stderr)
        return signals
