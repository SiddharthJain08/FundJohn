"""measurement twin — never register; emits the parent's signals

`_cached` twin of S_tr_02_hurst_regime_flip (six-repo D2d). The per-bar
ffill().pct_change() over the growing prefix is replaced by BaseStrategy.cached()
slices; the Hurst gate, sizing and prints are the parent's. Used solely by scripts/measure_indicator_cache.py; registering it would double the
parent's signals live. The ONLY sanctioned measurement command is `scripts/measure_indicator_cache.py --strategy <id>_cached --compare-to <id>`: `unified_backtest --strategy-file` derives the sid from the file stem, so no manifest tier cap applies and the run is not comparable to the parent (full static universe, heavy on the 8 GB box).
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import Signal
import strategies.implementations.S_tr_02_hurst_regime_flip as _orig

_hurst_rs = _orig._hurst_rs

__all__ = ['HurstRegimeFlip']


class HurstRegimeFlip(_orig.HurstRegimeFlip):
    """Measurement twin: parent's Hurst regime-flip signals via cached returns."""

    id = 'S_tr_02_hurst_regime_flip_cached'

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

        tickers = [t for t in universe if t in prices.columns]
        if not tickers:
            return []

        price_data = prices[tickers].ffill()
        if len(price_data) < self.min_lookback:
            print(f'[debug] {self.id}: signals=0 (need {self.min_lookback} rows, got {len(price_data)})',
                  file=sys.stderr)
            return []

        latest   = self.cached('ffill', lambda p: p.ffill(), prices)[tickers].iloc[-1]
        rets_all = self.cached('ffill_pct', lambda p: p.ffill().pct_change(), prices)[tickers]
        vol      = rets_all.iloc[-self.VOL_WINDOW:].dropna(how='all').std() * np.sqrt(252)

        momentum_cands: list = []  # (ticker, H)
        mean_rev_cands: list = []

        for ticker in tickers:
            series = rets_all[ticker].dropna().values
            if len(series) < 50:
                continue
            arr = series[-self.RS_LOOKBACK:]
            H   = _hurst_rs(arr)
            if np.isnan(H):
                continue
            if H > self.H_MOMENTUM_THRESH:
                momentum_cands.append((ticker, H))
            elif H < self.H_MEAN_REV_THRESH:
                mean_rev_cands.append((ticker, H))

        if not momentum_cands and not mean_rev_cands:
            print(f'[debug] {self.id}: signals=0 (no tickers passed H gate)', file=sys.stderr)
            return []

        # Sort by strongest signal
        momentum_cands.sort(key=lambda x: x[1], reverse=True)
        mean_rev_cands.sort(key=lambda x: x[1])

        scale        = self.position_scale(regime_state)
        signals: List[Signal] = []
        max_per_side = self.MAX_SIGNALS // 2

        for direction, candidates in [
            ('LONG',  momentum_cands[:max_per_side]),
            ('SHORT', mean_rev_cands[:max_per_side]),
        ]:
            for ticker, H in candidates:
                price = float(latest.get(ticker, 0))
                if price <= 0:
                    continue
                ticker_vol = max(float(vol.get(ticker, 0.20)), 1e-4)
                size = float(self.BASE_SIZE_PCT * (0.15 / ticker_vol) * scale)
                size = max(0.001, min(size, 0.05))

                deviation  = abs(H - 0.50)
                confidence = 'HIGH' if deviation > 0.08 else ('MED' if deviation > 0.05 else 'LOW')

                st = self.compute_stops_and_targets(
                    price_data[ticker].dropna(), direction, price,
                    regime_state=regime_state,
                )
                signals.append(Signal(
                    ticker            = ticker,
                    direction         = direction,
                    entry_price       = round(price, 4),
                    stop_loss         = st['stop'],
                    target_1          = st['t1'],
                    target_2          = st['t2'],
                    target_3          = st['t3'],
                    position_size_pct = size,
                    confidence        = confidence,
                    signal_params     = {
                        'hurst':      round(H, 4),
                        'deviation':  round(deviation, 4),
                        'vol_annual': round(ticker_vol, 4),
                    },
                ))

        print(f'[debug] {self.id}: signals={len(signals)} '
              f'(momentum={len(momentum_cands)}, mean_rev={len(mean_rev_cands)})', file=sys.stderr)
        return signals
