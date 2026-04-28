from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

__all__ = ['PairsCointegrationCopula']


class PairsCointegrationCopula(BaseStrategy):
    """Statistical arbitrage via cointegration-ranked equity pairs with z-score entry/exit."""

    id                = 'S_pairs_cointegration_copula'
    name              = 'PairsCointegrationCopula'
    description       = 'Statistical arbitrage in US equity pairs via cointegration z-score entry/exit.'
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = 504
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 20

    FORMATION_DAYS = 252
    Z_ENTRY        = 2.0
    Z_EXIT         = 0.5
    TOP_N_PAIRS    = 5
    MAX_TICKERS    = 40

    def generate_signals(
        self,
        prices: pd.DataFrame,
        regime: dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print('[debug] signals=0 (empty prices)', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0 (regime filtered)', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)

        if len(prices) < self.FORMATION_DAYS + 2:
            print(f'[debug] signals=0 (insufficient rows: {len(prices)})', file=sys.stderr)
            return []

        # Resolve close prices — handle both flat and MultiIndex columns
        if isinstance(prices.columns, pd.MultiIndex):
            if 'close' in prices.columns.get_level_values(0):
                close_df = prices['close']
            else:
                close_df = prices.iloc[:, prices.columns.get_level_values(0) == prices.columns.get_level_values(0)[0]]
        else:
            close_df = prices

        tickers = [t for t in universe if t in close_df.columns][:self.MAX_TICKERS]
        if len(tickers) < 10:
            print(f'[debug] signals=0 (universe too small: {len(tickers)})', file=sys.stderr)
            return []

        close = close_df[tickers].ffill().dropna(axis=1, thresh=self.FORMATION_DAYS)
        tickers = list(close.columns)
        if len(tickers) < 10:
            print('[debug] signals=0 (after dropna)', file=sys.stderr)
            return []

        log_prices = np.log(close.clip(lower=1e-6))
        formation = log_prices.iloc[-(self.FORMATION_DAYS + 1):-1]
        current_log = log_prices.iloc[-1]
        current_price = close.iloc[-1]

        # Rank pairs by Engle-Granger cointegration p-value
        try:
            from statsmodels.tsa.stattools import coint
        except ImportError:
            print('[debug] signals=0 (statsmodels unavailable)', file=sys.stderr)
            return []

        pair_scores = []
        n = len(tickers)
        for i in range(n):
            for j in range(i + 1, n):
                ti, tj = tickers[i], tickers[j]
                try:
                    t_stat, pval, _ = coint(formation[ti], formation[tj])
                    if pval < 0.05:
                        pair_scores.append((t_stat, ti, tj))
                except Exception:
                    continue

        if not pair_scores:
            print('[debug] signals=0 (no cointegrated pairs)', file=sys.stderr)
            return []

        pair_scores.sort(key=lambda x: x[0])           # most negative t-stat first
        top_pairs = pair_scores[:self.TOP_N_PAIRS]

        signals: List[Signal] = []
        base_size = min(0.04 * scale, 0.08)

        for t_stat, ti, tj in top_pairs:
            try:
                yi = formation[ti].values
                xj = formation[tj].values
                beta = float(np.cov(yi, xj)[0, 1] / (np.var(xj) + 1e-12))

                spread = yi - beta * xj
                mu_s   = float(spread.mean())
                sig_s  = float(spread.std())
                if sig_s < 1e-8:
                    continue

                cur_spread = float(current_log[ti]) - beta * float(current_log[tj])
                z = (cur_spread - mu_s) / sig_s

                if abs(z) < self.Z_ENTRY:
                    continue

                direction_i = 'LONG'  if z < -self.Z_ENTRY else 'SHORT'
                direction_j = 'SHORT' if z < -self.Z_ENTRY else 'LONG'
                confidence  = 'HIGH'  if abs(z) > 3.0 else 'MED'

                pi = float(current_price[ti])
                pj = float(current_price[tj])

                st_i = self.compute_stops_and_targets(close[ti], direction_i, pi, regime_state=regime_state)
                st_j = self.compute_stops_and_targets(close[tj], direction_j, pj, regime_state=regime_state)

                params_i = {'pair': tj, 'z_score': round(z, 4), 'beta': round(beta, 6)}
                params_j = {'pair': ti, 'z_score': round(-z, 4), 'beta': round(beta, 6)}

                signals.append(Signal(
                    ticker=ti, direction=direction_i,
                    entry_price=pi,
                    stop_loss=float(st_i['stop_loss']),
                    target_1=float(st_i['target_1']),
                    target_2=float(st_i['target_2']),
                    target_3=float(st_i['target_3']),
                    position_size_pct=base_size,
                    confidence=confidence,
                    signal_params=params_i,
                ))
                signals.append(Signal(
                    ticker=tj, direction=direction_j,
                    entry_price=pj,
                    stop_loss=float(st_j['stop_loss']),
                    target_1=float(st_j['target_1']),
                    target_2=float(st_j['target_2']),
                    target_3=float(st_j['target_3']),
                    position_size_pct=base_size,
                    confidence=confidence,
                    signal_params=params_j,
                ))
            except Exception:
                continue

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
