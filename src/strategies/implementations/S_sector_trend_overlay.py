"""
Sector Trend Overlay — distilled trend-following replication, ported from
"Trend Following (4/4): The Poor Man's Trend Program" (Beyond Passive
Investing, 2026). Thesis: one representative instrument per sector captures
most of a 62-futures-market trend program's diversification benefit on top
of a risk-premia core, since trend persistence is a sector- not
instrument-level phenomenon.

Interpretation choices (paper is ambiguous on trend-signal construction and
regime scope — deliberately picked the MORE literal, long-only "overlay"
reading here, variant 2 of 2, vs. variant 1's TSMOM/bidirectional/
crisis-alpha reading):
  - Trend signal = price vs. 200d SMA (classic Faber-style filter), NOT
    12m-skip-1m TSMOM — the paper offers both ("OR"); price/SMA is the more
    literal "trend program" reading and skips the skip-month convention,
    which is really a stock-momentum artifact.
  - Long/flat only (LONG above SMA200, FLAT otherwise), not bidirectional —
    an overlay "layered on top of" a long core reads as a tilt that adds
    exposure in confirmed uptrends and steps aside otherwise, not one that
    shorts against a position the core already holds long.
  - Active in LOW_VOL/TRANSITIONING (calm-to-shifting trends), not
    HIGH_VOL/CRISIS — a slow 200d filter confirms trends that persist in
    orderly regimes; by HIGH_VOL/CRISIS such trends are usually whipsawing.
  - Inverse-vol sizing uses a 21d (monthly) window, not 63d (quarterly) —
    more responsive, matching a faster review cadence.
  - Weekly rebalance (first bar of a new ISO week), not monthly — a sleeve
    meant to track shifting trend strength promptly reviews faster than its
    slow (200d) signal updates.

Universe: 11 Select Sector SPDR ETFs (one representative instrument per
GICS sector) — XLK, XLF, XLE, XLV, XLI, XLY, XLP, XLU, XLB, XLRE, XLC.

Source: https://beyondpassive.substack.com/p/trend-following-44-the-poor-mans
"""
from __future__ import annotations

import sys
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['SectorTrendOverlay']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID      = 'S_sector_trend_overlay'

# One representative instrument per GICS sector.
BASKET = ('XLK', 'XLF', 'XLE', 'XLV', 'XLI', 'XLY', 'XLP', 'XLU', 'XLB', 'XLRE', 'XLC')

SMA_WINDOW    = 200   # classic long-term trend filter (trading days)
VOL_LOOKBACK  = 21    # monthly realized-vol window for inverse-vol sizing
MIN_HISTORY   = 504   # ~2 years — per strategy_spec min_lookback_required
MIN_UNIVERSE  = 8     # per strategy_spec minimum_universe_size


class SectorTrendOverlay(BaseStrategy):
    """Distilled sector-level trend overlay: price-vs-200d-SMA direction,
    long/flat only, inverse-realized-vol sizing, weekly rebalance.

    Source: https://beyondpassive.substack.com/p/trend-following-44-the-poor-mans
    """

    id                = STRATEGY_ID
    name              = 'Sector Trend Overlay'
    description       = (
        'One-instrument-per-sector trend overlay: price-vs-200-day-SMA sets '
        'a long/flat direction, inverse realized-vol sets size, layered on '
        'top of a risk-premia core.'
    )
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = MIN_HISTORY
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = len(BASKET)

    def default_parameters(self) -> dict:
        return {
            'sma_window':         SMA_WINDOW,
            'vol_lookback':       VOL_LOOKBACK,
            'overlay_gross_frac': 0.30,   # total sleeve gross before regime scale
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

        # Weekly rebalance: first bar of a new ISO week, or a regime-flip day.
        idx = prices.index
        if len(prices) < 2 or not hasattr(idx[-1], 'isocalendar'):
            print('[debug] signals=0', file=sys.stderr)
            return []
        same_week = idx[-1].isocalendar()[:2] == idx[-2].isocalendar()[:2]
        if same_week and not self.cadence_reset(regime):
            print('[debug] signals=0', file=sys.stderr)
            return []

        sma_window   = int(self.parameters.get('sma_window', SMA_WINDOW))
        vol_lookback = int(self.parameters.get('vol_lookback', VOL_LOOKBACK))

        available = [t for t in BASKET if t in prices.columns]
        if len(available) < MIN_UNIVERSE:
            print(f'[debug] signals=0 ({len(available)} tickers < {MIN_UNIVERSE})', file=sys.stderr)
            return []

        # Price-vs-SMA200 direction (long/flat) + inverse realized-vol weight.
        candidates: dict[str, dict] = {}
        for ticker in available:
            series = prices[ticker].dropna()
            if len(series) < max(MIN_HISTORY, sma_window + 1):
                continue
            try:
                current_price = float(series.iloc[-1])
                sma = float(series.iloc[-sma_window:].mean())
            except (ValueError, IndexError):
                continue
            if sma <= 0:
                continue
            trend_strength = current_price / sma - 1.0
            if trend_strength <= 0:
                continue  # long/flat only — no trend confirmation, no position
            rets = series.pct_change().dropna().iloc[-vol_lookback:]
            if len(rets) < vol_lookback:
                continue
            realized_vol = float(rets.std())
            if not (realized_vol > 0):
                continue
            candidates[ticker] = {
                'trend_strength': trend_strength,
                'inv_vol':        1.0 / realized_vol,
            }

        if not candidates:
            print('[debug] signals=0', file=sys.stderr)
            return []

        total_inv_vol = sum(c['inv_vol'] for c in candidates.values())
        if not (total_inv_vol > 0):
            print('[debug] signals=0', file=sys.stderr)
            return []

        scale      = self.position_scale(regime_state)
        gross_frac = float(self.parameters.get('overlay_gross_frac', 0.30))

        signals = []
        for ticker, c in candidates.items():
            series = prices[ticker].dropna()
            if len(series) < 14:
                continue
            current_price = float(series.iloc[-1])
            direction = 'LONG'
            stops = self.compute_stops_and_targets(
                series,
                direction=direction,
                current_price=current_price,
                regime_state=regime_state,
            )

            weight        = c['inv_vol'] / total_inv_vol
            pos_size_frac = round(weight * gross_frac * scale, 4)
            ts = c['trend_strength']
            if ts > 0.10:
                confidence = 'HIGH'
            elif ts > 0.03:
                confidence = 'MED'
            else:
                confidence = 'LOW'
            signals.append(Signal(
                ticker            = ticker,
                direction         = direction,
                entry_price       = current_price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = pos_size_frac,
                confidence        = confidence,
                signal_params     = {
                    'price_over_sma200_pct': round(ts, 4),
                    'inv_vol_weight':        round(weight, 4),
                    'regime':                regime_state,
                    'scale':                 scale,
                    'rebalance':             True,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
