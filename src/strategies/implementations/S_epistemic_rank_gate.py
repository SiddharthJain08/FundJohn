from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

__all__ = ['EpistemicRankGate']


class EpistemicRankGate(BaseStrategy):
    """Rank by momentum-proxy scores; long top-decile, short bottom-decile; epistemic uncertainty gate."""

    id          = 'S_epistemic_rank_gate'
    name        = 'EpistemicRankGate'
    description = 'Rank by momentum-proxy LightGBM scores; long top-decile, short bottom-decile; epistemic uncertainty gate'
    tier        = 2
    # RISK_OFF maps to HIGH_VOL/CRISIS in this system's regime vocabulary
    active_in_regimes = ['TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    UNCERTAINTY_GATE  = 0.20   # minimum cross-sectional dispersion to enable trading
    UNCERTAINTY_CAP   = 0.40   # tail-cap: above this dampens position sizes
    DECILE_FRAC       = 0.10   # top/bottom 10% of ranked universe
    BASE_SIZE_PCT     = 0.015  # base per-position size before vol/regime scaling
    LOOKBACK_MOM      = 252    # 12-month momentum lookback
    SKIP_RECENT       = 21     # skip last 21 days (standard momentum skip)
    VOL_WINDOW        = 21     # realized vol window for vol-normalization

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

        # --- Filter universe to tickers present in prices ---
        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < 20:
            print(f'[debug] signals=0 (too few tickers: {len(tickers)})', file=sys.stderr)
            return []

        price_data = prices[tickers].ffill().dropna(how='all')
        min_rows = self.LOOKBACK_MOM + self.SKIP_RECENT + 5
        if len(price_data) < min_rows:
            print(f'[debug] signals=0 (need {min_rows} rows, got {len(price_data)})', file=sys.stderr)
            return []

        returns = price_data.pct_change()

        # --- Momentum scores: 12-1 month return (proxy for LightGBM rank scores) ---
        start_px = price_data.iloc[-(self.LOOKBACK_MOM + self.SKIP_RECENT)]
        end_px   = price_data.iloc[-self.SKIP_RECENT]
        momentum = (end_px / start_px - 1.0).dropna()
        valid    = momentum.index.tolist()
        if len(valid) < 20:
            print(f'[debug] signals=0 (insufficient valid momentum tickers)', file=sys.stderr)
            return []

        # --- Epistemic uncertainty gate: cross-sectional return dispersion ---
        recent_rets    = returns[valid].iloc[-self.SKIP_RECENT:]
        cross_std      = recent_rets.std(axis=1).mean()
        cross_abs_mean = recent_rets.abs().mean(axis=1).mean()
        uncertainty    = float(cross_std / cross_abs_mean) if cross_abs_mean > 0 else 0.0

        if uncertainty < self.UNCERTAINTY_GATE:
            print(f'[debug] signals=0 (uncertainty={uncertainty:.3f} < gate={self.UNCERTAINTY_GATE})', file=sys.stderr)
            return []

        # Position damping from uncertainty tail cap (high uncertainty → smaller sizes)
        tail_ratio       = min(uncertainty / self.UNCERTAINTY_CAP, 1.0)
        uncertainty_mult = 1.0 - tail_ratio * 0.5   # 50% max dampening at tail

        # --- Realized vol for vol-normalization ---
        vol     = returns[valid].iloc[-self.VOL_WINDOW:].std() * np.sqrt(252)
        vol     = vol.replace(0, np.nan).dropna()
        common  = [t for t in valid if t in vol.index]
        if len(common) < 10:
            print(f'[debug] signals=0 (insufficient vol-valid tickers)', file=sys.stderr)
            return []

        momentum = momentum[common]
        vol      = vol[common]
        latest   = price_data[common].iloc[-1]

        # --- Rank into top/bottom decile ---
        n_decile = max(1, int(len(common) * self.DECILE_FRAC))
        ranked   = momentum.rank(ascending=True)
        longs    = ranked[ranked >= (ranked.max() - n_decile + 1)].index.tolist()
        shorts   = ranked[ranked <= n_decile].index.tolist()

        scale   = self.position_scale(regime_state)
        signals: List[Signal] = []
        max_per_side = self.MAX_SIGNALS // 2

        for direction, candidates in [('LONG', longs[:max_per_side]), ('SHORT', shorts[:max_per_side])]:
            for ticker in candidates:
                price = float(latest.get(ticker, 0))
                if price <= 0:
                    continue
                ticker_vol = float(vol.get(ticker, 0.20))
                vol_norm   = 0.15 / ticker_vol if ticker_vol > 0 else 1.0
                size       = float(self.BASE_SIZE_PCT * vol_norm * scale * uncertainty_mult)
                size       = max(0.001, min(size, 0.05))

                st = self.compute_stops_and_targets(
                    price_data[ticker].dropna(), direction, price,
                    regime_state=regime_state,
                )
                confidence = 'HIGH' if uncertainty < 0.30 else 'MED'
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
                        'momentum':    round(float(momentum[ticker]), 4),
                        'uncertainty': round(uncertainty, 4),
                        'vol':         round(ticker_vol, 4),
                    },
                ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
