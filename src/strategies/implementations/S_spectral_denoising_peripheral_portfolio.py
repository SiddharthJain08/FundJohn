"""Spectral Denoising Peripheral Portfolio (Ansari, Jain & Iyer 2026, arxiv:2607.10297).
Marchenko-Pastur eigenvalue filtering denoises the return correlation matrix;
Markov-chain coreness identifies peripheral (low-coreness) assets that capture
diversification benefit hidden by noise-dominated eigenmodes.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE
from strategies.universe_default import sp500 as universe_filter

__all__ = ['SpectralDenoisingPeripheralPortfolio']

STRATEGY_ID      = 'S_spectral_denoising_peripheral_portfolio'
INSTRUMENT_CLASS = 'equity'
LOOKBACK         = 500   # trading-day rolling window [§3]
REBALANCE_DAYS   = 125   # hold period; rebalance every ~6 months [§4.2]
TOP_M            = 40    # peripheral assets to select [§4.2]
MIN_TICKERS      = 50    # minimum cross-section to compute meaningful coreness


class SpectralDenoisingPeripheralPortfolio(BaseStrategy):
    """MP spectral denoising of return correlation matrix → LONG top-40 peripheral (low-coreness) assets."""

    id          = STRATEGY_ID
    name        = 'SpectralDenoisingPeripheralPortfolio'
    description = ('Marchenko-Pastur spectral denoising reveals core-periphery network; '
                   'long top-40 peripheral (low-coreness) assets equal-weighted (Ansari 2026).')
    tier               = 2
    min_lookback       = LOOKBACK
    active_in_regimes  = ['LOW_VOL', 'TRANSITIONING']

    def generate_signals(
        self, prices: pd.DataFrame, regime: dict,
        universe: List[str], aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []

        scale = self.position_scale(regime_state)

        # --- Data preparation ---
        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < MIN_TICKERS:
            print(f'[debug] signals=0 (universe too small: {len(tickers)})', file=sys.stderr)
            return []

        px = prices[tickers].tail(LOOKBACK + 1).dropna(axis=1, thresh=LOOKBACK // 2)
        if len(px) < LOOKBACK // 2 or px.shape[1] < MIN_TICKERS:
            print(f'[debug] signals=0 (insufficient data: rows={len(px)} cols={px.shape[1]})',
                  file=sys.stderr)
            return []

        # Rebalance gate: emit signals only on the first day of each 125-day window
        # Determine position of last date in the full price history
        all_px_len = len(prices.dropna(subset=[tickers[0]], how='all')) if tickers else len(prices)
        if all_px_len % REBALANCE_DAYS != 0 and all_px_len > REBALANCE_DAYS:
            # Not a rebalance day — suppress output (pipeline holds existing positions)
            print(f'[debug] signals=0 (not rebalance day: pos={all_px_len})', file=sys.stderr)
            return []

        rets = px.pct_change().dropna()
        if len(rets) < LOOKBACK // 4:
            print(f'[debug] signals=0 (insufficient returns: {len(rets)})', file=sys.stderr)
            return []

        avail = list(rets.columns)
        N = len(avail)
        T = len(rets)

        # --- Step 1: Correlation matrix ---
        R = rets.corr().values.astype(float)
        np.fill_diagonal(R, 1.0)

        # --- Step 2: Marchenko-Pastur spectral denoising [§2, §4.1] ---
        lambda_plus = (1.0 + np.sqrt(N / T)) ** 2
        eigenvalues, eigenvectors = np.linalg.eigh(R)  # ascending order

        structured_idx = [i for i in range(N) if eigenvalues[i] > lambda_plus]
        if not structured_idx:
            structured_idx = [N - 1]  # fallback: market mode only

        R_denoised = np.zeros((N, N))
        for i in structured_idx:
            v = eigenvectors[:, i]
            R_denoised += eigenvalues[i] * np.outer(v, v)

        # Re-normalise diagonal to 1 (correlation scale)
        d = np.sqrt(np.maximum(np.diag(R_denoised), 1e-12))
        R_denoised = R_denoised / np.outer(d, d)
        np.fill_diagonal(R_denoised, 1.0)

        # --- Step 3: Build weighted financial network ---
        A = np.abs(R_denoised)
        np.fill_diagonal(A, 0.0)
        A[A < 0.05] = 0.0  # prune near-zero edges

        # --- Step 4: Markov-chain coreness via power iteration [§4.2] ---
        row_sums = A.sum(axis=1, keepdims=True)
        row_sums = np.where(row_sums == 0, 1.0, row_sums)
        T_mat = A / row_sums  # row-stochastic transition matrix

        pi = np.ones(N) / N
        for _ in range(200):
            pi_new = pi @ T_mat
            s = pi_new.sum()
            if s > 0:
                pi_new /= s
            if np.max(np.abs(pi_new - pi)) < 1e-9:
                break
            pi = pi_new
        coreness = pi_new  # higher = core, lower = peripheral

        # --- Step 5: Select top-M peripheral assets (ascending coreness) ---
        sorted_idx = np.argsort(coreness)  # most peripheral first
        n_select = min(TOP_M, N)
        peripheral_idx = sorted_idx[:n_select]
        selected = [avail[i] for i in peripheral_idx]

        if not selected:
            print('[debug] signals=0', file=sys.stderr)
            return []

        pos_size = round(scale / len(selected), 4)
        pos_size = max(pos_size, 0.001)

        last_prices = px.iloc[-1]
        signals: List[Signal] = []
        for rank, ticker in enumerate(selected):
            price = float(last_prices.get(ticker, np.nan))
            if not np.isfinite(price) or price <= 0:
                continue
            st = self.compute_stops_and_targets(
                px[ticker].dropna(), 'LONG', price, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=price,
                stop_loss=float(st['stop']),
                target_1=float(st['t1']),
                target_2=float(st['t2']),
                target_3=float(st['t3']),
                position_size_pct=pos_size,
                confidence='MED',
                signal_params={
                    'periphery_rank': rank,
                    'coreness': round(float(coreness[peripheral_idx[rank]]), 6),
                    'n_structured_modes': len(structured_idx),
                    'lambda_plus': round(float(lambda_plus), 4),
                    'hold_days': REBALANCE_DAYS,
                },
            ))
            if len(signals) >= self.MAX_SIGNALS:
                break

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
