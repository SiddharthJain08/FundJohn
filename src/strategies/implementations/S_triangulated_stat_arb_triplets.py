from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE
from src.strategies.universe_default import sp500 as universe_filter

INSTRUMENT_CLASS = "equity"

__all__ = ['TriangulatedStatArbTriplets']


class TriangulatedStatArbTriplets(BaseStrategy):
    """Triangulated stat arb: OLS spread across stock triplets — trade z-score mean reversion.

    Source: Longmore, K. (2026) — robotwealth.com/resourcing-a-triangulated-stat-arb-operation-as-a-solo-trader/
    Hypothesis: 3-stock triangulated spread (log_A - beta_B*log_B - beta_C*log_C) is cleaner
    and more capital-efficient than bilateral pairs; signal = rolling 60-day z-score crossing ±2.0.
    """

    id          = 'S_triangulated_stat_arb_triplets'
    name        = 'TriangulatedStatArbTriplets'
    description = 'Triangulated stat arb across stock triplets — OLS hedge ratio z-score mean reversion.'
    tier        = 2
    min_lookback = 504
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

    ENTRY_Z   = 2.0
    Z_WINDOW  = 60
    BASE_SIZE = 0.008   # ~0.8% per leg; capped at 3%
    MAX_TICKERS = 50    # pre-screen cap for correlation speed
    MAX_TRIPLETS = 20   # triplets to attempt before MAX_SIGNALS guard

    def generate_signals(self, prices: pd.DataFrame, regime: dict,
                         universe: List[str], aux_data: dict = None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        scale = self.position_scale(regime_state)

        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < 10:
            print(f'[debug] signals=0 (universe<10: {len(tickers)})', file=sys.stderr)
            return []
        if len(prices) < self.min_lookback:
            print(f'[debug] signals=0 (history<{self.min_lookback}: {len(prices)})', file=sys.stderr)
            return []

        # Forward-fill gaps and restrict to lookback window
        raw = prices[tickers].ffill().tail(self.min_lookback)
        log_px = np.log(raw.dropna(how='all'))
        if len(log_px) < 252:
            print(f'[debug] signals=0 (post-dropna<252)', file=sys.stderr)
            return []

        # Keep only tickers with >=90% coverage then cap for speed
        valid = log_px.dropna(axis=1, thresh=int(len(log_px) * 0.9)).columns.tolist()
        if len(valid) < 10:
            print(f'[debug] signals=0 (valid<10)', file=sys.stderr)
            return []
        valid = valid[:self.MAX_TICKERS]

        # Formation window (first half), trade window (second half)
        mid = len(log_px) // 2
        form_px = log_px[valid].iloc[:mid]
        trade_px = log_px[valid].iloc[mid:]

        # Pairwise return correlation on formation window
        rets_form = form_px.diff().dropna()
        if len(rets_form) < 30:
            print(f'[debug] signals=0 (rets_form<30)', file=sys.stderr)
            return []

        corr_mat = rets_form.corr()

        # Anchor-based triplets: each ticker A → top-2 correlated peers B, C
        # O(n) triplets — avoids intractable O(n^3) enumeration
        triplets = []
        for a in valid:
            row = corr_mat[a].drop(labels=[a]).abs()
            top2 = row.nlargest(2).index.tolist()
            if len(top2) == 2:
                triplets.append((a, top2[0], top2[1]))

        if not triplets:
            print(f'[debug] signals=0 (no triplets formed)', file=sys.stderr)
            return []

        signals: List[Signal] = []
        attempted = 0

        for (a, b, c) in triplets:
            if attempted >= self.MAX_TRIPLETS or len(signals) >= self.MAX_SIGNALS:
                break
            attempted += 1

            # OLS: log_A = beta_B*log_B + beta_C*log_C + intercept (on formation window)
            Xf = np.column_stack([form_px[b].values, form_px[c].values,
                                   np.ones(len(form_px))])
            yf = form_px[a].values
            try:
                coefs, _, _, _ = np.linalg.lstsq(Xf, yf, rcond=None)
            except Exception:
                continue
            beta_b, beta_c, intercept = float(coefs[0]), float(coefs[1]), float(coefs[2])

            # Spread residual on trade window
            Xt = np.column_stack([trade_px[b].values, trade_px[c].values,
                                   np.ones(len(trade_px))])
            spr = trade_px[a].values - Xt @ coefs

            if len(spr) < self.Z_WINDOW:
                continue
            window = spr[-self.Z_WINDOW:]
            mu  = float(np.mean(window))
            sig = float(np.std(window))
            if sig < 1e-8:
                continue
            zscore = float((spr[-1] - mu) / sig)

            if abs(zscore) < self.ENTRY_Z:
                continue

            # Inverse-vol sizing: smaller size when spread is noisier
            spr_std_20 = float(np.std(spr[-20:])) if len(spr) >= 20 else sig
            if spr_std_20 < 1e-8:
                spr_std_20 = sig
            size = min(float(self.BASE_SIZE * scale / spr_std_20), 0.03)

            dir_a = 'SHORT' if zscore > 0 else 'LONG'
            dir_b = 'LONG'  if zscore > 0 else 'SHORT'
            dir_c = 'LONG'  if zscore > 0 else 'SHORT'
            conf  = 'HIGH' if abs(zscore) > 3.0 else ('MED' if abs(zscore) > 2.5 else 'LOW')
            params = {
                'triplet': f'{a}_{b}_{c}',
                'zscore': round(zscore, 3),
                'beta_b': round(beta_b, 4),
                'beta_c': round(beta_c, 4),
            }

            for ticker, direction in [(a, dir_a), (b, dir_b), (c, dir_c)]:
                px_val = float(np.exp(log_px[ticker].iloc[-1]))
                stops = self.compute_stops_and_targets(
                    prices[ticker].dropna().tail(20), direction, px_val,
                    regime_state=regime_state)
                signals.append(Signal(
                    ticker=ticker,
                    direction=direction,
                    entry_price=px_val,
                    stop_loss=float(stops['stop']),
                    target_1=float(stops['t1']),
                    target_2=float(stops['t2']),
                    target_3=float(stops['t3']),
                    position_size_pct=size,
                    confidence=conf,
                    signal_params=params,
                ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
