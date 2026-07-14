"""
Aligned Economic Index (AEI) market timing under yield-curve-slope state-switching.

Based on: Aarab (2025) — "Switching between states and the COVID-19 turbulence"
arXiv:2512.20477  OOS R²=4.12%, state-specific OLS on Welch & Goyal predictors.

Since the canonical Welch & Goyal predictors (div yield, earnings yield, T-bill,
term spread, default spread, NEI) are not available in-ledger, we proxy them with
price-derived equivalents:
  - Equity premium  → SPY 12m cumulative return
  - Short momentum  → SPY 1m return
  - Term spread     → VIX3M − VIX9D spread (normal > 0)
  - Default spread  → VIX level z-score (fear proxy)
  - Realized vol    → 63-day rolling σ_annual
  - Mean reversion  → price / 200d MA − 1

State variable (yield-curve slope proxy):
  - VIX9D < VIX3M  → upward-sloping fear surface → "expansion"
  - else            → "recession/contraction"
  Fallback when VIX term data absent: SPY 252d return sign.

Signal: LONG SPY when mean-variance weight > threshold, FLAT otherwise.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

__all__ = ['AlignedEconomicIndexRegimeTiming']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID = 'S_aligned_economic_index_regime_timing'


class AlignedEconomicIndexRegimeTiming(BaseStrategy):
    """Market timing via Aligned Economic Index under yield-curve state-switching (Aarab 2025)."""

    id                 = STRATEGY_ID
    name               = 'AlignedEconomicIndexRegimeTiming'
    description        = 'Mean-variance SPY timing via rolling-OLS AEI under yield-curve state-switch'
    tier               = 2
    signal_frequency   = 'monthly'
    min_lookback       = 756
    active_in_regimes  = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    def default_parameters(self) -> dict:
        return {
            'gamma':            3.0,   # mean-variance risk aversion
            'ols_window':       756,   # rolling OLS window (trading days)
            'min_obs_state':    120,   # min state-specific rows for OLS
            'weight_threshold': 0.05,  # min MV-weight to emit LONG
            'vol_window':       63,    # realized vol estimation window
            'max_position':     0.20,  # max position size (fraction of NAV)
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

        p = self.parameters
        gamma            = float(p.get('gamma', 3.0))
        ols_window       = int(p.get('ols_window', 756))
        min_obs_state    = int(p.get('min_obs_state', 120))
        weight_threshold = float(p.get('weight_threshold', 0.05))
        vol_window       = int(p.get('vol_window', 63))
        max_pos          = float(p.get('max_position', 0.20))

        scale = self.position_scale(regime_state)

        # ── 1. SPY price series ───────────────────────────────────────────────
        if 'SPY' not in prices.columns:
            print('[debug] signals=0 (SPY not in prices)', file=sys.stderr)
            return []
        spy_prices = prices['SPY'].dropna()
        if len(spy_prices) < min_obs_state + vol_window:
            print(f'[debug] signals=0 (SPY rows={len(spy_prices)})', file=sys.stderr)
            return []

        # ── 2. Macro series from aux_data (dict[str, pd.Series]) ─────────────
        macro = (aux_data or {}).get('macro') or {}
        vix9d  = self._get_macro(macro, 'VIX9D',  spy_prices.index)
        vix3m  = self._get_macro(macro, 'VIX3M',  spy_prices.index)
        vix_lv = self._get_macro(macro, 'VIX',    spy_prices.index)

        # ── 3. Feature engineering ────────────────────────────────────────────
        spy_ret    = spy_prices.pct_change()
        eq_prem    = spy_prices.pct_change(252)           # 12m return proxy
        mom_1m     = spy_prices.pct_change(21)            # 1m return
        rvol       = spy_ret.rolling(vol_window).std() * np.sqrt(252)
        ma_200     = spy_prices.rolling(200).mean()
        price_ma   = (spy_prices / ma_200 - 1.0).clip(-0.30, 0.30)

        # State: 1 = expansion (VIX term structure normal), 0 = recession
        if vix9d is not None and vix3m is not None:
            state = (vix9d < vix3m).astype(int)           # upward slope → expansion
        elif vix_lv is not None:
            state = (vix_lv < 20.0).astype(int)           # low VIX → expansion
        else:
            state = (eq_prem > 0).astype(int)             # positive trend → expansion

        # Default spread proxy: VIX level z-score; 0 if VIX unavailable
        if vix_lv is not None:
            vix_roll_mean = vix_lv.rolling(252, min_periods=63).mean()
            vix_roll_std  = vix_lv.rolling(252, min_periods=63).std().clip(lower=1e-6)
            def_spread = ((vix_lv - vix_roll_mean) / vix_roll_std)
        else:
            def_spread = pd.Series(0.0, index=spy_prices.index)

        # Term spread proxy: VIX3M − VIX9D; 0 if absent
        if vix3m is not None and vix9d is not None:
            term_spread = (vix3m - vix9d)
        else:
            term_spread = pd.Series(0.0, index=spy_prices.index)

        # ── 4. Build aligned feature matrix for TRAINING (needs fwd_ret) ─────
        feat_cols = ['eq_prem', 'mom_1m', 'rvol', 'price_ma', 'def_spread', 'term_spread']
        feat = pd.DataFrame({
            'eq_prem':    eq_prem,
            'mom_1m':     mom_1m,
            'rvol':       rvol,
            'price_ma':   price_ma,
            'def_spread': def_spread,
            'term_spread':term_spread,
            'state':      state,
        }, index=spy_prices.index)

        # One-month forward SPY return (prediction target); shift -21 → last 21 rows are NaN
        feat['fwd_ret'] = spy_ret.rolling(21).sum().shift(-21)

        # CURRENT BAR values — read BEFORE dropna() so we get today's features
        cur_state   = int(feat['state'].iloc[-1]) if not feat['state'].isna().iloc[-1] else 1
        cur_rvol    = float(feat['rvol'].iloc[-1])
        cur_price   = float(spy_prices.iloc[-1])
        cur_x_vals  = feat[feat_cols].iloc[-1].values

        # Training set: drop rows with any NaN (incl. last 21 rows missing fwd_ret)
        train = feat.dropna()
        if len(train) < min_obs_state + 21:
            print(f'[debug] signals=0 (train rows={len(train)})', file=sys.stderr)
            return []

        # Validate current features
        if not np.isfinite(cur_rvol) or cur_rvol <= 0:
            cur_rvol = 0.15
        if not np.all(np.isfinite(cur_x_vals)):
            print('[debug] signals=0 (non-finite current features)', file=sys.stderr)
            return []
        if not np.isfinite(cur_price) or cur_price <= 0:
            print('[debug] signals=0 (bad SPY price)', file=sys.stderr)
            return []

        # ── 5. State-specific rolling OLS ─────────────────────────────────────
        same_state = train[train['state'] == cur_state].tail(ols_window)
        if len(same_state) < min_obs_state:
            same_state = train.tail(ols_window)           # fallback: all states
        if len(same_state) < 30:
            print(f'[debug] signals=0 (state rows={len(same_state)})', file=sys.stderr)
            return []

        X_raw = same_state[feat_cols].values
        y_raw = same_state['fwd_ret'].values
        X_aug = np.column_stack([np.ones(len(X_raw)), X_raw])

        try:
            XtX    = X_aug.T @ X_aug
            Xty    = X_aug.T @ y_raw
            ridge  = np.eye(XtX.shape[0]) * 1e-8
            coeffs = np.linalg.solve(XtX + ridge, Xty)
        except np.linalg.LinAlgError:
            print('[debug] signals=0 (OLS failed)', file=sys.stderr)
            return []

        cur_x_aug     = np.concatenate([[1.0], cur_x_vals])
        predicted_ret = float(cur_x_aug @ coeffs)

        # ── 6. Mean-variance weight ───────────────────────────────────────────
        mv_weight = predicted_ret / (gamma * cur_rvol ** 2)
        mv_weight = float(np.clip(mv_weight, 0.0, 1.0))

        if mv_weight < weight_threshold:
            print(f'[debug] signals=0 (mv_weight={mv_weight:.3f})', file=sys.stderr)
            return []

        # ── 7. Size and emit ──────────────────────────────────────────────────
        position_size = float(np.clip(mv_weight * scale * max_pos, 0.01, max_pos))
        confidence    = 'HIGH' if mv_weight > 0.60 else ('MED' if mv_weight > 0.30 else 'LOW')

        st = self.compute_stops_and_targets(
            spy_prices, 'LONG', cur_price, regime_state=regime_state
        )

        signals = [Signal(
            ticker            = 'SPY',
            direction         = 'LONG',
            entry_price       = round(cur_price, 4),
            stop_loss         = round(st['stop'], 4),
            target_1          = round(st['t1'], 4),
            target_2          = round(st['t2'], 4),
            target_3          = round(st['t3'], 4),
            position_size_pct = round(position_size, 4),
            confidence        = confidence,
            signal_params     = {
                'mv_weight':        round(mv_weight, 4),
                'predicted_return': round(predicted_ret, 4),
                'realized_vol':     round(cur_rvol, 4),
                'state':            'expansion' if cur_state == 1 else 'recession',
                'gamma':            gamma,
                'n_state_obs':      len(same_state),
            },
        )]
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _get_macro(
        macro: dict,
        series_name: str,
        date_index: pd.DatetimeIndex,
    ) -> 'pd.Series | None':
        """Extract a named series from macro dict (dict[str, pd.Series]) and align
        to date_index via forward-fill. Returns None if series is absent/empty."""
        if not isinstance(macro, dict):
            return None
        s = macro.get(series_name)
        if not isinstance(s, pd.Series) or s.empty:
            return None
        s = s.dropna().sort_index()
        if s.empty:
            return None
        return s.reindex(date_index, method='ffill')
