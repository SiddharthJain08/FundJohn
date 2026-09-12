# S_iv_spread_skew_regime_dependent — IV Spread + Risk-Neutral Skew, regime-partitioned
# Source: http://arxiv.org/abs/2608.26115v1 (Li & Wang 2026, "Option-Implied
# Signals and Crash Risk"). Cremers-Weinbaum (2010) IV spread (ATM call IV -
# ATM put IV) and Bakshi, Kapadia & Madan (2003) model-free risk-neutral
# skewness (rn_skew_30d, precomputed in options_surface.py) remain robust
# cross-sectional predictors of next-month equity returns across regimes,
# unlike the classic Xing/Zhang/Zhao (2010) smirk which decays and flips
# sign post-2023 (deliberately excluded here). Cross-sectional z-score rank,
# LONG the bottom decile (low spread/skew), SHORT the top decile, monthly
# rebalance. Clean-room implementation from the abstract's pseudocode — no
# reference code ported. ML/XGBoost overlay (paper's AI/mega-cap regime
# extension) is out of scope — this is the core linear cross-section only.
from __future__ import annotations
import sys
from typing import List
import pandas as pd
from strategies.base import BaseStrategy, Signal
from src.strategies.universe_default import options_eligible_only as universe_filter

INSTRUMENT_CLASS = 'equity'

MIN_UNIVERSE  = 30     # need enough names for a meaningful decile split
DECILE_FRAC   = 0.10
MAX_PER_SIDE  = 15
EXTREME_FRAC  = 0.02   # top/bottom 2% of the cross-section => HIGH confidence


def _mean_std(vals: list[float]) -> tuple[float, float]:
    n = len(vals)
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / n
    return mean, var ** 0.5


class IVSpreadSkewRegimeDependent(BaseStrategy):
    id                = 'S_iv_spread_skew_regime_dependent'
    name              = 'IV Spread + Skew Regime-Dependent'
    description       = ('Cross-sectional rank on Cremers-Weinbaum IV spread + Bakshi '
                          'risk-neutral skew (rn_skew_30d); LONG bottom decile, SHORT '
                          'top decile, monthly rebalance.')
    tier              = 2
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    def default_parameters(self) -> dict:
        return {
            'min_universe': MIN_UNIVERSE,
            'max_per_side': MAX_PER_SIDE,
            'gross_frac':   0.30,
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

        regime = regime or {}
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Monthly rebalance: only fire on the first bar of a new calendar
        # month, or on a regime-flip day (cadence_reset).
        idx = prices.index
        if len(prices) < 2:
            print('[debug] signals=0', file=sys.stderr)
            return []
        same_month = (getattr(idx[-1], 'month', None) == getattr(idx[-2], 'month', None)
                      and getattr(idx[-1], 'year', None) == getattr(idx[-2], 'year', None))
        if same_month and not self.cadence_reset(regime):
            print('[debug] signals=0', file=sys.stderr)
            return []

        options = (aux_data or {}).get('options', {}) or {}
        elig = set(universe) if universe else set(prices.columns)

        rows = []  # (ticker, iv_spread, rn_skew, series)
        for ticker in elig:
            if ticker not in prices.columns:
                continue
            opts = options.get(ticker)
            if opts is None:
                continue
            iv_spread = opts.get('iv_spread')
            rn_skew   = opts.get('rn_skew_30d')
            if iv_spread is None or rn_skew is None:
                continue
            series = prices[ticker].dropna()
            if len(series) < 14:
                continue
            try:
                current_price = float(series.iloc[-1])
            except (ValueError, IndexError):
                continue
            if current_price <= 0:
                continue
            rows.append((ticker, float(iv_spread), float(rn_skew), series))

        min_universe = int(self.parameters.get('min_universe', MIN_UNIVERSE))
        if len(rows) < min_universe:
            print(f'[debug] signals=0 ({len(rows)} names < {min_universe})', file=sys.stderr)
            return []

        iv_mean, iv_std     = _mean_std([r[1] for r in rows])
        skew_mean, skew_std = _mean_std([r[2] for r in rows])
        if iv_std <= 0 or skew_std <= 0:
            print('[debug] signals=0', file=sys.stderr)
            return []

        composite = []
        for ticker, iv_spread, rn_skew, series in rows:
            z_iv   = (iv_spread - iv_mean) / iv_std
            z_skew = (rn_skew - skew_mean) / skew_std
            composite.append((z_iv + z_skew, ticker, iv_spread, rn_skew, series))
        composite.sort(key=lambda x: x[0])

        n = len(composite)
        max_per_side = int(self.parameters.get('max_per_side', MAX_PER_SIDE))
        decile_n  = min(max(1, round(n * DECILE_FRAC)), max_per_side)
        extreme_n = max(1, round(n * EXTREME_FRAC))

        longs  = [(s, t, iv, sk, se, 'LONG')  for s, t, iv, sk, se in composite[:decile_n]]
        shorts = [(s, t, iv, sk, se, 'SHORT') for s, t, iv, sk, se in composite[-decile_n:]]
        long_extreme_cut  = composite[extreme_n - 1][0]
        short_extreme_cut = composite[-extreme_n][0]

        scale      = self.position_scale(regime_state)
        gross_frac = float(self.parameters.get('gross_frac', 0.30))
        per_name   = min(gross_frac * scale / max(1, 2 * decile_n), 0.05)

        signals: List[Signal] = []
        for score, ticker, iv_spread, rn_skew, series, direction in longs + shorts:
            current_price = float(series.iloc[-1])
            stops = self.compute_stops_and_targets(
                series, direction=direction, current_price=current_price,
                atr_multiplier=2.0, regime_state=regime_state,
            )
            extreme = (score <= long_extreme_cut) if direction == 'LONG' else (score >= short_extreme_cut)
            confidence = 'HIGH' if extreme else 'MED'

            signals.append(Signal(
                ticker            = ticker,
                direction         = direction,
                entry_price       = current_price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = round(per_name, 4),
                confidence        = confidence,
                signal_params     = {
                    'iv_spread':       round(iv_spread, 4),
                    'rn_skew_30d':     round(rn_skew, 4),
                    'composite_score': round(score, 4),
                    'decile':          'bottom' if direction == 'LONG' else 'top',
                    'regime':          regime_state,
                    'universe_size':   n,
                    'rebalance':       True,
                },
                features          = {
                    'iv_spread':   round(iv_spread, 4),
                    'rn_skew_30d': round(rn_skew, 4),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
