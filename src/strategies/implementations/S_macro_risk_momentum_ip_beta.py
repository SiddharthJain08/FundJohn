from __future__ import annotations
import sys
import pandas as pd
import numpy as np
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

__all__ = ['MacroRiskMomentumIPBeta']


class MacroRiskMomentumIPBeta(BaseStrategy):
    """Momentum long/short where winners have higher IP-growth beta than losers (Liu & Zhang 2008)."""

    id                 = 'S_macro_risk_momentum_ip_beta'
    name               = 'MacroRiskMomentumIPBeta'
    description        = 'Momentum winners carry higher IP-growth beta; long top decile, short bottom decile'
    tier               = 2
    signal_frequency   = 'monthly'
    min_lookback       = 756          # ~3 years daily (spec: 756)
    active_in_regimes  = ['LOW_VOL', 'TRANSITIONING']

    def default_parameters(self) -> dict:
        return {
            'momentum_window': 252,    # ~12 months daily
            'skip_window':     21,     # skip last month
            'beta_window':     504,    # ~24 months daily
            'top_pct':         0.10,
            'bottom_pct':      0.10,
            'max_signals':     40,
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

        scale = self.position_scale(regime_state)
        p     = self.parameters

        # ── 1. Intersect universe with available price columns ──────────────
        available = [t for t in universe if t in prices.columns]
        if len(available) < 20:
            print('[debug] signals=0', file=sys.stderr)
            return []

        prices_sub = prices[available].copy()

        # ── 2. Extract IP growth from macro aux_data ─────────────────────────
        ip_growth = self._get_ip_growth(aux_data, len(prices_sub))

        # ── 3. Compute daily returns ─────────────────────────────────────────
        rets = prices_sub.pct_change().fillna(0.0)

        # ── 4. Momentum score: cumret(t-12m, t-1m) ─────────────────────────
        mom_window = p['momentum_window']
        skip       = p['skip_window']
        if len(rets) < mom_window + skip:
            print('[debug] signals=0', file=sys.stderr)
            return []

        end_idx   = len(rets) - skip
        start_idx = end_idx - mom_window
        if start_idx < 0:
            print('[debug] signals=0', file=sys.stderr)
            return []

        momentum_rets = rets.iloc[start_idx:end_idx]
        momentum_score = (1 + momentum_rets).prod() - 1

        # ── 5. IP-growth beta: 2-yr rolling OLS ─────────────────────────────
        beta_window = p['beta_window']
        beta_start  = max(0, end_idx - beta_window)
        rets_beta   = rets.iloc[beta_start:end_idx]

        if ip_growth is not None and len(ip_growth) >= len(rets_beta):
            ip_slice = ip_growth.iloc[-len(rets_beta):].values
            betas = self._compute_betas(rets_beta, ip_slice)
        else:
            # Fallback: uniform betas (momentum-only ranking)
            betas = pd.Series(0.0, index=available)

        # ── 6. Rank by momentum, filter by IP beta direction ─────────────────
        ranked = momentum_score.rank(pct=True)
        top_pct    = p['top_pct']
        bottom_pct = p['bottom_pct']

        long_mask  = ranked >= (1 - top_pct)
        short_mask = ranked <= bottom_pct

        long_tickers  = ranked[long_mask].index.tolist()
        short_tickers = ranked[short_mask].index.tolist()

        # Prefer winners with highest IP beta, losers with lowest IP beta
        if betas is not None and not betas.empty:
            long_tickers  = sorted(long_tickers,  key=lambda t: betas.get(t, 0), reverse=True)
            short_tickers = sorted(short_tickers, key=lambda t: betas.get(t, 0))

        max_per_side = p['max_signals'] // 2
        long_tickers  = long_tickers[:max_per_side]
        short_tickers = short_tickers[:max_per_side]

        # ── 7. Build signals ──────────────────────────────────────────────────
        last_prices = prices_sub.iloc[-1]
        n_longs  = len(long_tickers)
        n_shorts = len(short_tickers)
        size_per = scale * 0.04  # 4% per name before regime scale

        signals: List[Signal] = []

        for ticker in long_tickers:
            px = float(last_prices.get(ticker, 0.0))
            if px <= 0:
                continue
            stops = self.compute_stops_and_targets(
                prices_sub[ticker].dropna(), 'LONG', px, regime_state=regime_state
            )
            mom_val = float(momentum_score.get(ticker, 0.0))
            conf = 'HIGH' if mom_val > 0.15 else ('MED' if mom_val > 0.05 else 'LOW')
            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = px,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = round(size_per, 4),
                confidence        = conf,
                signal_params     = {
                    'momentum_score': round(mom_val, 4),
                    'ip_beta':        round(float(betas.get(ticker, 0.0)), 4),
                    'regime':         regime_state,
                },
            ))

        for ticker in short_tickers:
            px = float(last_prices.get(ticker, 0.0))
            if px <= 0:
                continue
            stops = self.compute_stops_and_targets(
                prices_sub[ticker].dropna(), 'SHORT', px, regime_state=regime_state
            )
            mom_val = float(momentum_score.get(ticker, 0.0))
            conf = 'HIGH' if mom_val < -0.10 else ('MED' if mom_val < -0.03 else 'LOW')
            signals.append(Signal(
                ticker            = ticker,
                direction         = 'SHORT',
                entry_price       = px,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = round(size_per, 4),
                confidence        = conf,
                signal_params     = {
                    'momentum_score': round(mom_val, 4),
                    'ip_beta':        round(float(betas.get(ticker, 0.0)), 4),
                    'regime':         regime_state,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals

    # ── helpers ────────────────────────────────────────────────────────────────

    def _get_ip_growth(self, aux_data: dict, n_rows: int) -> pd.Series | None:
        """Extract INDPRO monthly pct-change, reindex to daily length."""
        if aux_data is None:
            return None
        macro = aux_data.get('macro')
        if macro is None or macro.empty:
            return None
        col = None
        for c in ['INDPRO', 'indpro', 'industrial_production', 'ip']:
            if c in macro.columns:
                col = c
                break
        if col is None:
            return None
        ip_monthly = macro[col].dropna().pct_change().fillna(0.0)
        # Forward-fill monthly values to daily length
        ip_daily = ip_monthly.reindex(
            range(n_rows), method='ffill'
        ).fillna(0.0)
        return ip_daily

    def _compute_betas(self, rets: pd.DataFrame, ip: np.ndarray) -> pd.Series:
        """OLS beta of each stock's returns on IP-growth series."""
        ip_arr = ip[-len(rets):] if len(ip) >= len(rets) else ip
        if len(ip_arr) < 10:
            return pd.Series(0.0, index=rets.columns)
        ip_dm = ip_arr - ip_arr.mean()
        ip_var = float(np.dot(ip_dm, ip_dm))
        if ip_var == 0:
            return pd.Series(0.0, index=rets.columns)
        betas = {}
        for ticker in rets.columns:
            r = rets[ticker].values[-len(ip_arr):]
            r_dm = r - r.mean()
            betas[ticker] = float(np.dot(r_dm, ip_dm) / ip_var)
        return pd.Series(betas)
