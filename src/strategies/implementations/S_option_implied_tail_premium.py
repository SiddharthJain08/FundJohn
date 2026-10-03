"""Taming the Option Factor Zoo: A High-Dimensional Analysis — Walter, Zimmer
& Ulrich (2026). Source: http://arxiv.org/abs/2609.31263v1

Double-selection LASSO screening of option-implied characteristics against a
160-factor equity zoo finds equity factors explain ~91% of option-factor
variance in-sample, but jump-tail, risk-neutral kurtosis and IV-curve
convexity characteristics retain non-zero residual SDF loadings. LONG top
decile / SHORT bottom decile on the orthogonalized composite.

Implementation note: the full 160-factor LASSO screen is out of scope for a
single strategy module (StrategyCoder import allowlist + no covariance-based
optimization). This is a tractable reduction: three option-implied
characteristics proxy the paper's surviving factors from aux_data['options']
(canonical type 'options_eod') —
  jump_tail  <- skew_20d        (OTM put IV - ATM call IV; crash/tail premium)
  kurtosis   <- surface_premium (vega-weighted IV - ATM IV; wing pricing)
  convexity  <- ts_ratio - 1    (near/far IV term-structure curvature)
each is orthogonalized via cross-sectional OLS against two liquid equity
factor proxies (12-1 momentum, 21d realized vol) computed from `prices` —
standing in for the paper's 160-factor zoo. Residuals are z-scored and
summed into a composite; rank cross-sectionally, LONG top decile / SHORT
bottom decile, monthly rebalance.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal
from strategies.universe_default import options_eligible_only as universe_filter

__all__ = ['OptionImpliedTailPremium']

STRATEGY_ID       = 'S_option_implied_tail_premium'
INSTRUMENT_CLASS  = 'equity'


class OptionImpliedTailPremium(BaseStrategy):
    """Orthogonalized option-implied tail/kurtosis/convexity characteristic -> LONG/SHORT decile, monthly."""

    id                = STRATEGY_ID
    name              = 'OptionImpliedTailPremium'
    description       = ("Cross-sectional composite of option-implied jump-tail, kurtosis and IV-curve "
                          "convexity proxies, orthogonalized against momentum/vol via OLS; LONG top decile "
                          "/ SHORT bottom decile, monthly rebalance.")
    tier              = 2
    min_lookback      = 290
    signal_frequency  = 'monthly'
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    _DECILE_PCT = 0.10
    _BASE_SIZE  = 0.025
    _REBAL_STEP = 21   # ~1 trading month
    _MIN_NAMES  = 30   # minimum tickers with usable option characteristics

    def _option_chars(self, opts_map: dict, tickers: list) -> pd.DataFrame | None:
        """Raw option-implied characteristic frame, one row per ticker with
        >=2 of the 3 characteristics present. Missing cells filled with the
        cross-sectional median so the OLS step below stays well-posed."""
        rows = {}
        for t in tickers:
            opts = opts_map.get(t)
            if not opts:
                continue
            jump_tail = opts.get('skew_20d')
            kurt      = opts.get('surface_premium')
            ts_ratio  = opts.get('ts_ratio')
            convexity = (ts_ratio - 1.0) if ts_ratio is not None else None
            present = sum(v is not None for v in (jump_tail, kurt, convexity))
            if present < 2:
                continue
            rows[t] = {'jump_tail': jump_tail, 'kurtosis': kurt, 'convexity': convexity}
        if len(rows) < self._MIN_NAMES:
            return None
        df = pd.DataFrame(rows).T
        for col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
            df[col] = df[col].fillna(df[col].median())
        return df.dropna()

    def _equity_factors(self, psub: pd.DataFrame) -> pd.DataFrame:
        """mom_12_1 and 21d realized vol per ticker — the orthogonalization
        target standing in for the paper's 160-factor equity zoo."""
        safe = lambda s: s.replace(0, np.nan)
        mom  = psub.iloc[-22] / safe(psub.iloc[-253]) - 1
        vol  = psub.pct_change().iloc[-21:].std()
        out  = pd.DataFrame({'mom_12_1': mom, 'vol_21d': vol})
        return out.replace([np.inf, -np.inf], np.nan)

    def _orthogonalize(self, chars: pd.DataFrame, factors: pd.DataFrame) -> pd.Series | None:
        """Cross-sectional OLS of each characteristic on [1, mom_12_1, vol_21d];
        residuals z-scored and summed -> composite score per ticker."""
        panel = chars.join(factors, how='inner').dropna()
        if len(panel) < self._MIN_NAMES:
            return None
        X = np.column_stack([np.ones(len(panel)), panel['mom_12_1'].values, panel['vol_21d'].values])
        composite = pd.Series(0.0, index=panel.index)
        for col in ('jump_tail', 'kurtosis', 'convexity'):
            y = panel[col].values
            try:
                beta, *_ = np.linalg.lstsq(X, y, rcond=None)
            except Exception:
                continue
            resid = y - X @ beta
            std = resid.std()
            if std < 1e-12:
                continue
            composite = composite.add(pd.Series((resid - resid.mean()) / std, index=panel.index), fill_value=0.0)
        return composite.dropna()

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
            print('[debug] signals=0 (regime inactive)', file=sys.stderr)
            return []
        if len(prices) % self._REBAL_STEP != 0 and not self.cadence_reset(regime):
            print('[debug] signals=0 (off-cycle)', file=sys.stderr)
            return []
        if len(prices) < self.min_lookback:
            print(f'[debug] signals=0 (lookback too short: {len(prices)})', file=sys.stderr)
            return []

        opts_map = (aux_data or {}).get('options') or {}
        if not opts_map:
            print('[debug] signals=0 (no options data)', file=sys.stderr)
            return []

        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < self._MIN_NAMES:
            print(f'[debug] signals=0 (universe too small: {len(tickers)})', file=sys.stderr)
            return []

        psub  = prices[tickers]
        chars = self._option_chars(opts_map, tickers)
        if chars is None:
            print('[debug] signals=0 (insufficient option characteristics)', file=sys.stderr)
            return []

        factors = self._equity_factors(psub)
        score   = self._orthogonalize(chars, factors)
        if score is None or len(score) < self._MIN_NAMES:
            print('[debug] signals=0 (orthogonalization failed)', file=sys.stderr)
            return []

        scale    = self.position_scale(regime_state)
        n_dec    = max(1, min(int(len(score) * self._DECILE_PCT), self.MAX_SIGNALS // 2))
        longs    = score.nlargest(n_dec).index.tolist()
        shorts   = score.nsmallest(n_dec).index.tolist()
        pos_size = min(self._BASE_SIZE * scale, 0.08)
        latest   = psub.iloc[-1]
        signals: List[Signal] = []

        for ticker, direction in (*[(t, 'LONG') for t in longs], *[(t, 'SHORT') for t in shorts]):
            price = float(latest.get(ticker, np.nan))
            if np.isnan(price) or price <= 0:
                continue
            st = self.compute_stops_and_targets(
                psub[ticker].dropna(), direction, price, regime_state=regime_state)
            signals.append(Signal(
                ticker=ticker, direction=direction,
                entry_price=round(price, 4),
                stop_loss=st['stop'], target_1=st['t1'], target_2=st['t2'], target_3=st['t3'],
                position_size_pct=pos_size, confidence='MED',
                signal_params={'factor': 'option_implied_tail_premium',
                               'composite_score': round(float(score[ticker]), 6)},
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
