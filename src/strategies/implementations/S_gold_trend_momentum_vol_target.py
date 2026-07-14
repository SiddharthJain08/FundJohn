"""
Gold Trend-Momentum with Volatility-Targeted Fractional Kelly.

Based on: "Forecast-to-Fill: Benchmark-Neutral Alpha and Billion-Dollar Capacity
in Gold Futures (2015-2025)" — Singha, Aguilera-Toste & Lahiri (arXiv 2511.08571).

Signal:
  smoothed_trend  = EMA(n_short) − EMA(n_long)
  momentum_score  = close / close[-lookback] − 1
  regime_score    = alpha * zscore(trend) + (1-alpha) * zscore(momentum)
  vol_scalar      = vol_target / realized_vol_20d
  kelly_f         = kelly_fraction * score / (1 + impact_gamma * |score|)
  position_size   = |kelly_f| * vol_scalar  if |score| > threshold else FLAT

Only emits LONG signals (gold is a flight-to-quality/trend asset; shorting GLD
via an ETP is asymmetric and out-of-scope for this implementation).
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['GoldTrendMomentumVolTarget']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID      = 'S_gold_trend_momentum_vol_target'

GOLD_TICKER      = 'GLD'
_ANNUAL_FACTOR   = 252 ** 0.5


class GoldTrendMomentumVolTarget(BaseStrategy):
    """Smoothed trend-momentum on GLD sized via volatility-targeted fractional Kelly."""

    id                = STRATEGY_ID
    name              = 'GoldTrendMomentumVolTarget'
    description       = (
        'Smoothed EMA-trend + momentum z-score on GLD, vol-targeted fractional '
        'Kelly sizing, ATR-anchored exits; benchmark-neutral gold alpha.'
    )
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = 504        # 2 years per paper §3
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 1

    def default_parameters(self) -> dict:
        return {
            'n_short':         20,    # short EMA span (days)
            'n_long':          60,    # long  EMA span (days)
            'lookback':        126,   # momentum lookback — 6 months
            'window':          63,    # rolling zscore window — 3 months
            'alpha':           0.60,  # weight on trend component
            'vol_target':      0.15,  # annualised volatility target
            'entry_threshold': 0.50,  # |zscore| must exceed to enter
            'kelly_fraction':  0.25,  # fractional Kelly scalar
            'impact_gamma':    0.02,  # market-impact dampening in Kelly
            'max_pos_size':    0.15,  # position size cap (pre-regime-scale)
        }

    # ------------------------------------------------------------------ helpers

    def _zscore(self, series: pd.Series, window: int) -> pd.Series:
        """Rolling z-score; NaN-safe (returns NaN when std ≈ 0)."""
        mu  = series.rolling(window, min_periods=max(window // 2, 2)).mean()
        sig = series.rolling(window, min_periods=max(window // 2, 2)).std(ddof=1)
        return (series - mu) / sig.replace(0.0, np.nan)

    def _kelly(self, score: float) -> float:
        """Impact-adjusted fractional Kelly: f = kf * s / (1 + gamma * |s|)."""
        kf    = self.parameters['kelly_fraction']
        gamma = self.parameters['impact_gamma']
        raw   = kf * score / (1.0 + gamma * abs(score))
        return float(np.clip(raw, -1.0, 1.0))

    # --------------------------------------------------------------- main logic

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
            print(f'[GoldTrendMomentumVolTarget] regime={regime_state} not active — skipping', file=sys.stderr)
            print('[debug] signals=0', file=sys.stderr)
            return []

        if GOLD_TICKER not in prices.columns:
            print(f'[GoldTrendMomentumVolTarget] {GOLD_TICKER} missing from price panel', file=sys.stderr)
            print('[debug] signals=0', file=sys.stderr)
            return []

        p      = self.parameters
        series = prices[GOLD_TICKER].dropna()

        if len(series) < self.min_lookback:
            print(
                f'[GoldTrendMomentumVolTarget] only {len(series)} rows < {self.min_lookback} required',
                file=sys.stderr,
            )
            print('[debug] signals=0', file=sys.stderr)
            return []

        # ── trend component ──────────────────────────────────────────────────
        ema_short    = series.ewm(span=p['n_short'], adjust=False).mean()
        ema_long     = series.ewm(span=p['n_long'],  adjust=False).mean()
        trend_z      = self._zscore(ema_short - ema_long, p['window'])

        # ── momentum component ───────────────────────────────────────────────
        mom_raw      = series / series.shift(p['lookback']) - 1.0
        mom_z        = self._zscore(mom_raw, p['window'])

        # ── blended regime score ─────────────────────────────────────────────
        score_series = p['alpha'] * trend_z + (1.0 - p['alpha']) * mom_z
        score_now    = float(score_series.iloc[-1]) if pd.notna(score_series.iloc[-1]) else 0.0

        # Only go LONG when score exceeds threshold (never short GLD ETP)
        if score_now < p['entry_threshold']:
            print(
                f'[GoldTrendMomentumVolTarget] score={score_now:.3f} < threshold={p["entry_threshold"]:.2f} — FLAT',
                file=sys.stderr,
            )
            print('[debug] signals=0', file=sys.stderr)
            return []

        # ── volatility targeting ─────────────────────────────────────────────
        daily_rets     = series.pct_change().dropna()
        rv_20d         = float(daily_rets.tail(20).std()) * _ANNUAL_FACTOR
        if rv_20d <= 0.0:
            rv_20d = 0.15   # fallback to vol_target itself → vol_scalar = 1

        vol_scalar = min(p['vol_target'] / rv_20d, 3.0)

        # ── position size ────────────────────────────────────────────────────
        kelly_abs  = abs(self._kelly(score_now))
        raw_size   = kelly_abs * vol_scalar
        capped     = min(raw_size, p['max_pos_size'])
        scale      = self.position_scale(regime_state)
        pos_size   = round(max(capped * scale, 0.005), 4)

        # ── entry / stops / targets ──────────────────────────────────────────
        current_price = float(series.iloc[-1])
        stops = self.compute_stops_and_targets(
            series,
            direction='LONG',
            current_price=current_price,
            regime_state=regime_state,
        )

        # ── confidence from z-score magnitude ────────────────────────────────
        if score_now >= 1.5:
            confidence = 'HIGH'
        elif score_now >= 0.8:
            confidence = 'MED'
        else:
            confidence = 'LOW'

        trend_z_now = float(trend_z.iloc[-1]) if pd.notna(trend_z.iloc[-1]) else 0.0
        mom_z_now   = float(mom_z.iloc[-1])   if pd.notna(mom_z.iloc[-1])   else 0.0

        sig = Signal(
            ticker            = GOLD_TICKER,
            direction         = 'LONG',
            entry_price       = current_price,
            stop_loss         = stops['stop'],
            target_1          = stops['t1'],
            target_2          = stops['t2'],
            target_3          = stops['t3'],
            position_size_pct = pos_size,
            confidence        = confidence,
            signal_params     = {
                'regime_score':    round(score_now, 4),
                'trend_zscore':    round(trend_z_now, 4),
                'mom_zscore':      round(mom_z_now, 4),
                'realized_vol_20d': round(rv_20d, 4),
                'vol_scalar':      round(vol_scalar, 4),
                'kelly_f':         round(kelly_abs, 4),
                'regime':          regime_state,
            },
        )

        print(
            f'[GoldTrendMomentumVolTarget] score={score_now:.3f} '
            f'trend_z={trend_z_now:.3f} mom_z={mom_z_now:.3f} '
            f'rv20={rv_20d:.3f} entry={current_price:.2f} '
            f'stop={stops["stop"]:.2f} pos={pos_size:.4f} regime={regime_state}',
            file=sys.stderr,
        )
        print('[debug] signals=1', file=sys.stderr)
        return [sig]
