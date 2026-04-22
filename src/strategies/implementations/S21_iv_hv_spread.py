"""S21: IV/HV Spread — sell expensive implied vol, buy cheap implied vol.

Variance risk premium: IV consistently trades at a premium to realized vol.
Cross-sectional rank by iv/rv ratio within universe; trade extremes.

Data: aux_data['options'] per-ticker: iv30 (30d IV), rv_20 (20d realized vol).
Zero LLM tokens.
"""
from __future__ import annotations
from typing import List
import pandas as pd
from src.strategies.base import BaseStrategy, Signal

SELL_VOL_THRESH = 1.35   # iv30/rv20 > 1.35 → vol expensive → SHORT
BUY_VOL_THRESH  = 0.75   # iv30/rv20 < 0.75 → vol cheap → LONG
MIN_RV          = 0.05   # skip tickers with rv_20 < 5% (data quality)
TOP_N           = 5      # max signals per direction


class IVHVSpread(BaseStrategy):
    id                = 'S21_iv_hv_spread'
    name              = 'IV/HV Spread Vol Arb'
    tier              = 2
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []

        opts_map = (aux_data or {}).get('options', {})
        if not opts_map:
            return []

        scale = self.position_scale(regime_state)

        # Compute HV20 from prices DataFrame (rv_20 is None in aux_data — no hv20 col in options_eod)
        hv20_map: dict = {}
        for col in prices.columns:
            s = prices[col].dropna()
            if len(s) >= 22:
                rv = float(s.pct_change().iloc[-20:].std() * (252 ** 0.5))
                if rv > 0:
                    hv20_map[col] = rv

        candidates = []

        for ticker in universe:
            opts = opts_map.get(ticker)
            if opts is None:
                continue

            iv30 = opts.get('iv30')
            if iv30 is None:
                continue

            rv20 = hv20_map.get(ticker)
            if rv20 is None or rv20 < MIN_RV:
                continue

            ratio = iv30 / rv20
            candidates.append((ticker, ratio, iv30, rv20))

        if not candidates:
            return []

        signals: List[Signal] = []

        # Sell vol: IV expensive relative to realized
        sell_cands = sorted(
            [(t, r, iv, rv) for t, r, iv, rv in candidates if r > SELL_VOL_THRESH],
            key=lambda x: x[1], reverse=True
        )[:TOP_N]

        for ticker, ratio, iv30, rv20 in sell_cands:
            if ticker not in prices.columns:
                continue
            ts = prices[ticker].dropna()
            if len(ts) < 2:
                continue
            current_price = float(ts.iloc[-1])
            if current_price <= 0:
                continue

            stops = self.compute_stops_and_targets(ts, 'SHORT', current_price, atr_multiplier=2.0)
            conf_val = min((ratio - SELL_VOL_THRESH) / 0.50, 1.0)
            confidence = 'HIGH' if conf_val >= 0.6 else ('MED' if conf_val >= 0.3 else 'LOW')
            size = min(0.02 * scale * (1.0 + conf_val), 0.06)

            signals.append(Signal(
                ticker            = ticker,
                direction         = 'SHORT',
                entry_price       = current_price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = round(size, 4),
                confidence        = confidence,
                signal_params     = {
                    'iv30':        round(iv30, 4),
                    'rv20':        round(rv20, 4),
                    'iv_hv_ratio': round(ratio, 3),
                    'threshold':   SELL_VOL_THRESH,
                    'vol_signal':  'SELL_VOL',
                },
            ))

        # Buy vol: IV cheap relative to realized
        buy_cands = sorted(
            [(t, r, iv, rv) for t, r, iv, rv in candidates if r < BUY_VOL_THRESH],
            key=lambda x: x[1]
        )[:TOP_N]

        for ticker, ratio, iv30, rv20 in buy_cands:
            if ticker not in prices.columns:
                continue
            ts = prices[ticker].dropna()
            if len(ts) < 2:
                continue
            current_price = float(ts.iloc[-1])
            if current_price <= 0:
                continue

            stops = self.compute_stops_and_targets(ts, 'LONG', current_price, atr_multiplier=2.0)
            conf_val = min((BUY_VOL_THRESH - ratio) / 0.25, 1.0)
            confidence = 'HIGH' if conf_val >= 0.6 else ('MED' if conf_val >= 0.3 else 'LOW')
            size = min(0.02 * scale * (1.0 + conf_val), 0.06)

            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = current_price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = round(size, 4),
                confidence        = confidence,
                signal_params     = {
                    'iv30':        round(iv30, 4),
                    'rv20':        round(rv20, 4),
                    'iv_hv_ratio': round(ratio, 3),
                    'threshold':   BUY_VOL_THRESH,
                    'vol_signal':  'BUY_VOL',
                },
            ))

        return signals[:self.MAX_SIGNALS]
