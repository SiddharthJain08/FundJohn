from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE
from src.strategies.universe_default import no_adr as universe_filter

INSTRUMENT_CLASS = "equity"
STRATEGY_ID = "S_ast_residual_momentum_factor"

__all__ = ['AstResidualMomentumFactor']


class AstResidualMomentumFactor(BaseStrategy):
    """Market-adjusted residual momentum: OLS-orthogonalized 12-month return (1M skip) long/short decile.

    Source: https://quantpedia.com/strategies/residual-momentum-factor/
    Clean-room port from QuantConnect reference (Blitz, Huij & Martens 2011).
    Uses SPY monthly returns as the single market factor proxy in place of the
    Fama-French Mkt-RF factor.  SMB/HML are omitted because we lack reliable
    cross-sectional B/M history in prices.parquet; the single-factor residual
    still strips market beta and preserves the idiosyncratic momentum signal.

    Signal: rank stocks on mean(resid[-13:-1]) / std(resid[-13:-1]).
    LONG top decile, SHORT bottom decile.  Rebalance monthly.
    """

    id               = 'S_ast_residual_momentum_factor'
    name             = 'AstResidualMomentumFactor'
    description      = 'Residual momentum: market-orthogonalized 12-1M score → monthly long/short top-bottom decile'
    tier             = 2
    signal_frequency = 'monthly'
    min_lookback     = 800        # ~38 months of daily bars
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

    MONTHS_LOOKBACK = 36      # OLS estimation window (months)
    MIN_MONTHS_BARS = 38      # monthly bars required after resample
    MIN_VALID_OBS   = 24      # minimum valid obs per ticker for OLS
    DECILE_FRAC     = 0.10
    BASE_SIZE_LONG  = 0.012
    BASE_SIZE_SHORT = 0.010
    MIN_RANKED      = 30      # minimum qualifying tickers to emit signals

    def _is_month_start(self, prices: pd.DataFrame) -> bool:
        """True if last date in prices is the first trading day of its month."""
        if prices.index.empty:
            return False
        last_date = prices.index[-1]
        prev_dates = prices.index[prices.index < last_date]
        if len(prev_dates) == 0:
            return True
        return prev_dates[-1].month != last_date.month

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

        # Monthly rebalance only — skip all other trading days
        if not (self._is_month_start(prices) or self.cadence_reset(regime)):
            print('[debug] signals=0', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)

        if 'SPY' not in prices.columns:
            print('[debug] signals=0', file=sys.stderr)
            return []

        tickers = [t for t in universe if t in prices.columns and t != 'SPY']
        if len(tickers) < self.MIN_RANKED:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Resample daily → month-end closes
        all_cols = tickers + ['SPY']
        try:
            monthly = prices[all_cols].resample('ME').last()
        except Exception:
            try:
                monthly = prices[all_cols].resample('M').last()
            except Exception:
                print('[debug] signals=0', file=sys.stderr)
                return []

        if len(monthly) < self.MIN_MONTHS_BARS:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Last MONTHS_LOOKBACK+1 bars → MONTHS_LOOKBACK monthly returns
        monthly_slice = monthly.iloc[-(self.MONTHS_LOOKBACK + 1):]
        monthly_rets = monthly_slice.pct_change().iloc[-self.MONTHS_LOOKBACK:]

        if len(monthly_rets) < self.MONTHS_LOOKBACK:
            print('[debug] signals=0', file=sys.stderr)
            return []

        spy_rets = monthly_rets['SPY'].values.astype(float)
        spy_finite = np.isfinite(spy_rets)

        scores: dict[str, float] = {}
        for ticker in tickers:
            if ticker not in monthly_rets.columns:
                continue
            y_full = monthly_rets[ticker].values.astype(float)
            valid = spy_finite & np.isfinite(y_full)
            if valid.sum() < self.MIN_VALID_OBS:
                continue

            x_v = spy_rets[valid]
            y_v = y_full[valid]
            X_mat = np.column_stack([np.ones(len(x_v)), x_v])
            try:
                coeffs, _, _, _ = np.linalg.lstsq(X_mat, y_v, rcond=None)
            except Exception:
                continue
            resids = y_v - X_mat @ coeffs

            # Score: mean / std of residuals in months t-13 to t-2 (skip t-1)
            if len(resids) < 14:
                continue
            window = resids[-13:-1]      # 12 months, skip the most recent residual
            w_std = float(window.std())
            if w_std < 1e-10:
                continue
            scores[ticker] = float(window.mean()) / w_std

        if len(scores) < self.MIN_RANKED:
            print('[debug] signals=0', file=sys.stderr)
            return []

        score_s = pd.Series(scores).sort_values()
        k = max(1, int(len(score_s) * self.DECILE_FRAC))
        longs  = score_s.iloc[-k:]   # highest residual momentum → LONG
        shorts = score_s.iloc[:k]    # lowest residual momentum  → SHORT

        current_prices = prices.iloc[-1]
        size_long  = round(self.BASE_SIZE_LONG  * scale, 6)
        size_short = round(self.BASE_SIZE_SHORT * scale, 6)
        signals: List[Signal] = []

        for ticker, sc in longs.items():
            if len(signals) >= self.MAX_SIGNALS // 2:
                break
            raw_p = current_prices.get(ticker)
            if raw_p is None or not np.isfinite(float(raw_p)) or float(raw_p) <= 0:
                continue
            price = float(raw_p)
            stops = self.compute_stops_and_targets(
                prices[ticker].dropna(), 'LONG', price, regime_state=regime_state
            )
            conf = 'HIGH' if sc > 1.5 else ('MED' if sc > 0.5 else 'LOW')
            signals.append(Signal(
                ticker=ticker, direction='LONG',
                entry_price=price,
                stop_loss=stops['stop'],
                target_1=stops['t1'], target_2=stops['t2'], target_3=stops['t3'],
                position_size_pct=size_long,
                confidence=conf,
                signal_params={'resid_mom_score': round(sc, 4)},
            ))

        for ticker, sc in shorts.items():
            if len(signals) >= self.MAX_SIGNALS:
                break
            raw_p = current_prices.get(ticker)
            if raw_p is None or not np.isfinite(float(raw_p)) or float(raw_p) <= 0:
                continue
            price = float(raw_p)
            stops = self.compute_stops_and_targets(
                prices[ticker].dropna(), 'SHORT', price, regime_state=regime_state
            )
            conf = 'HIGH' if sc < -1.5 else ('MED' if sc < -0.5 else 'LOW')
            signals.append(Signal(
                ticker=ticker, direction='SHORT',
                entry_price=price,
                stop_loss=stops['stop'],
                target_1=stops['t1'], target_2=stops['t2'], target_3=stops['t3'],
                position_size_pct=size_short,
                confidence=conf,
                signal_params={'resid_mom_score': round(sc, 4)},
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
