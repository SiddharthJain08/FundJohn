"""
Adaptive Hedge Ratio Ensemble — estimator-consensus beta anomaly.

Source: Chan & Chu (2026), "Objective Diversification of Dynamic Hedging"
(arXiv:2611.10333). The paper combines 5 objective-specific rolling
hedge-ratio (beta) estimators — minimum-variance, LAD, downside-semivariance,
lower-tail-quantile, and Expected-Shortfall-minimizing — showing that each
carries complementary information about the true market exposure and that
even equal-weight combination beats any single objective-matched estimator.

Interpretation (variant 1/2): rather than trading the *level* of the
combined hedge ratio directly (the obvious betting-against-beta read) or the
down-minus-up asymmetry (already covered by S_downside_beta_premium), this
variant trades on ESTIMATOR CONSENSUS as a reliability filter. For each name
we compute the 5 betas over a single long window, take their cross-estimator
dispersion (std) as a proxy for how trustworthy the combined hedge ratio is,
and only trade names in the low-dispersion (high-agreement) half of the
universe. Within that reliable subset we apply the classic low-beta anomaly:
LONG the low ensemble-beta quintile, SHORT the high ensemble-beta quintile.
Names where the 5 objective-specific estimators disagree are skipped
entirely — the paper's "diversification across objectives" is used here as
a confidence gate rather than as a direct re-weighting signal.

Data: close panel + SPY benchmark column only.
"""
from __future__ import annotations

import sys
from typing import List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal

try:
    from strategies.implementations._extra_panels import liquid_pool
except ImportError:  # direct-file import fallback (validate harness)
    from _extra_panels import liquid_pool

__all__ = ['AdaptiveHedgeRatioEnsemble']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_adaptive_hedge_ratio_ensemble'


class AdaptiveHedgeRatioEnsemble(BaseStrategy):
    """LONG low ensemble-beta / SHORT high ensemble-beta, gated on cross-estimator agreement."""

    id                = STRATEGY_ID
    name              = 'Adaptive Hedge Ratio Ensemble'
    description       = ('5-estimator hedge-ratio ensemble (min-var, robust/LAD-proxy, downside-semivariance, '
                         '20% tail, 5% ES-tail beta); trade only names where the estimators agree, LONG the low-beta '
                         'half SHORT the high-beta half (Chan & Chu 2026, variant 1/2).')
    tier              = 2
    signal_frequency  = 'monthly'
    min_lookback      = 506            # 504d spec window + 2d buffer
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = 24

    BETA_WINDOW      = 504
    MIN_OBS_FULL      = 300
    MIN_OBS_SEMI      = 100
    MIN_OBS_TAIL20    = 60
    MIN_OBS_ES05      = 15
    LEG_COUNT         = 12
    BASE_SIZE_LONG    = 0.015
    BASE_SIZE_SHORT   = 0.012
    MARKET_TICKER     = 'SPY'
    DISPERSION_KEEP   = 0.50           # keep the lowest-disagreement half

    def default_parameters(self) -> dict:
        return {'beta_window': self.BETA_WINDOW, 'leg_count': self.LEG_COUNT, 'pool_size': 500}

    def _month_boundary(self, prices: pd.DataFrame) -> bool:
        if len(prices) < 2:
            return False
        d1 = pd.Timestamp(prices.index[-1])
        d0 = pd.Timestamp(prices.index[-2])
        return d1.month != d0.month

    @staticmethod
    def _masked_beta(r: pd.DataFrame, s: pd.Series, mask: pd.Series, min_obs: int) -> pd.Series:
        """cov(r,s)/var(s) over rows where mask is True, pairwise-valid, vectorized."""
        if r.empty or s.empty or not mask.any():
            return pd.Series(dtype='float64')
        rm = r[mask.values]
        sm = s[mask.values]
        valid = rm.notna()
        n = valid.sum()
        sbc = pd.DataFrame(np.broadcast_to(sm.to_numpy(dtype='float64')[:, None], rm.shape),
                            index=rm.index, columns=rm.columns).where(valid)
        r_mean = rm.mean()
        s_mean = sbc.mean()
        cov = (rm * sbc).mean() - r_mean * s_mean
        var = (sbc * sbc).mean() - s_mean * s_mean
        beta = cov / var.where(var > 1e-10)
        return beta.where(n >= min_obs)

    @staticmethod
    def _robust_beta(r: pd.DataFrame, s: pd.Series, min_obs: int) -> pd.Series:
        """Spearman-rank correlation scaled by MAD ratio — robust, LAD-like proxy slope."""
        valid = r.notna()
        n = valid.sum()
        rr = r.rank(axis=0)
        sr = s.rank()
        sbc = pd.DataFrame(np.broadcast_to(sr.to_numpy(dtype='float64')[:, None], rr.shape),
                            index=rr.index, columns=rr.columns).where(valid)
        rm = rr.mean()
        sm = sbc.mean()
        cov = (rr * sbc).mean() - rm * sm
        rvar = (rr * rr).mean() - rm * rm
        svar = (sbc * sbc).mean() - sm * sm
        corr = cov / np.sqrt(rvar * svar).where(np.sqrt(rvar * svar) > 1e-10)
        r_mad = (r - r.median()).abs().median()
        s_mad = (s - s.median()).abs().median()
        scale = r_mad / max(float(s_mad), 1e-10)
        return (corr * scale).where(n >= min_obs)

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty or len(prices) < self.min_lookback:
            print('[debug] signals=0', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []

        if not (self._month_boundary(prices) or self.cadence_reset(regime)):
            print('[debug] signals=0', file=sys.stderr)
            return []

        mkt = self.MARKET_TICKER
        if mkt not in prices.columns:
            print('[debug] signals=0', file=sys.stderr)
            return []

        bwin = int(self.parameters.get('beta_window', self.BETA_WINDOW))
        pool = liquid_pool(prices, max_names=int(self.parameters.get('pool_size', 500)))
        pool = [t for t in pool if t in universe] or pool
        pool = [t for t in pool if t in prices.columns and t != mkt]
        if len(pool) < 100:
            print('[debug] signals=0', file=sys.stderr)
            return []

        eq = prices[pool].dropna(how='all')
        if len(eq) < bwin + 2:
            print('[debug] signals=0', file=sys.stderr)
            return []

        c = eq.iloc[-(bwin + 1):].astype('float64')
        spy = prices[mkt].reindex(c.index).astype('float64')
        rets = c.pct_change().iloc[1:]
        sret = spy.pct_change().iloc[1:]
        if sret.notna().sum() < bwin * 0.8:
            print('[debug] signals=0', file=sys.stderr)
            return []

        full_mask = pd.Series(True, index=sret.index)
        down_mask = sret < 0
        q20 = sret.quantile(0.20)
        q05 = sret.quantile(0.05)
        tail20_mask = sret <= q20
        es05_mask = sret <= q05

        b_ols    = self._masked_beta(rets, sret, full_mask, self.MIN_OBS_FULL)
        b_robust = self._robust_beta(rets, sret, self.MIN_OBS_FULL)
        b_semi   = self._masked_beta(rets, sret, down_mask, self.MIN_OBS_SEMI)
        b_tail20 = self._masked_beta(rets, sret, tail20_mask, self.MIN_OBS_TAIL20)
        b_es05   = self._masked_beta(rets, sret, es05_mask, self.MIN_OBS_ES05)

        ensemble = pd.concat(
            {'ols': b_ols, 'robust': b_robust, 'semi': b_semi, 'tail20': b_tail20, 'es05': b_es05},
            axis=1,
        ).dropna(how='any')
        if len(ensemble) < 30:
            print('[debug] signals=0', file=sys.stderr)
            return []

        ensemble_mean = ensemble.mean(axis=1)
        dispersion = ensemble.std(axis=1, ddof=1)

        disp_cut = dispersion.quantile(self.DISPERSION_KEEP)
        consensus = ensemble_mean[dispersion <= disp_cut]
        if len(consensus) < 20:
            print('[debug] signals=0', file=sys.stderr)
            return []

        rank_pct = consensus.rank(pct=True)
        n_leg = min(int(self.parameters.get('leg_count', self.LEG_COUNT)),
                    max(1, int(len(consensus) * 0.20)))
        longs  = rank_pct[rank_pct <= 0.20].sort_values(ascending=True).head(n_leg)
        shorts = rank_pct[rank_pct >= 0.80].sort_values(ascending=False).head(n_leg)

        scale = self.position_scale(regime_state)
        current = c.iloc[-1]

        def _conf(ext: float) -> str:
            if ext >= 0.95:
                return 'HIGH'
            if ext >= 0.88:
                return 'MED'
            return 'LOW'

        signals: List[Signal] = []
        for leg, direction, base in ((longs, 'LONG', self.BASE_SIZE_LONG),
                                     (shorts, 'SHORT', self.BASE_SIZE_SHORT)):
            for ticker, rp in leg.items():
                if len(signals) >= self.MAX_SIGNALS:
                    break
                raw = current.get(ticker)
                if raw is None or not np.isfinite(raw) or raw <= 0:
                    continue
                price = float(raw)
                series = prices[ticker].dropna()
                stops = self.compute_stops_and_targets(series, direction, price,
                                                       regime_state=regime_state)
                extremity = 1.0 - float(rp) if direction == 'LONG' else float(rp)
                signals.append(Signal(
                    ticker=ticker,
                    direction=direction,
                    entry_price=price,
                    stop_loss=stops['stop'],
                    target_1=stops['t1'],
                    target_2=stops['t2'],
                    target_3=stops['t3'],
                    position_size_pct=round(base * scale, 6),
                    confidence=_conf(extremity),
                    signal_params={
                        'beta_ensemble_mean': round(float(ensemble_mean[ticker]), 4),
                        'beta_dispersion':    round(float(dispersion[ticker]), 4),
                        'beta_ols':           round(float(ensemble.loc[ticker, 'ols']), 4),
                        'beta_es05':          round(float(ensemble.loc[ticker, 'es05']), 4),
                        'rank_pct':           round(float(rp), 4),
                    },
                ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
