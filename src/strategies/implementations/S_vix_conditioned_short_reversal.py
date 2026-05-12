"""
S_vix_conditioned_short_reversal — VIX-Conditioned Short-Term Reversal
Nagel (2012) "Evaporating Liquidity": short-term reversal returns are
compensation for liquidity provision; VIX-scaled aggression captures the
elevated risk premium when constrained intermediaries withdraw supply.
"""
from __future__ import annotations
import sys
import pandas as pd
import numpy as np
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['VixConditionedShortReversal']


class VixConditionedShortReversal(BaseStrategy):
    """Buy 1-week losers / short 1-week winners; scale position by VIX vs. median."""

    id          = 'S_vix_conditioned_short_reversal'
    name        = 'VIX-Conditioned Short-Term Reversal'
    description = (
        'Short-term reversal scaled by VIX/median-VIX — liquidity-provision risk '
        'premium is elevated when market makers are constrained (Nagel 2012)'
    )
    tier        = 2
    min_lookback = 252
    active_in_regimes = ['TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    def default_parameters(self) -> dict:
        return {
            'ret_window':        5,     # 1-week return window (trading days)
            'vix_high_threshold': 20.0, # VIX must exceed this to activate signals
            'vix_low_threshold':  15.0, # VIX below this → no signals
            'vix_med_window':    252,   # rolling window for VIX median
            'quintile_n':        5,     # quintile split (5 = 20% each end)
            'base_size_pct':     0.02,  # base position size per signal
            'max_size_pct':      0.06,  # per-signal cap
            'min_price':         5.0,   # skip penny stocks
            'min_universe':      20,    # need enough stocks for quintile split
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

        regime_state = (regime or {}).get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []

        p = self.parameters
        macro_df = (aux_data or {}).get('macro')

        # ── 1. VIX scalar ────────────────────────────────────────────────────
        vix_series = self._get_vix(macro_df)
        if vix_series is None or len(vix_series) < p['ret_window'] + 1:
            print('[debug] signals=0 no_vix', file=sys.stderr)
            return []

        current_vix = float(vix_series.iloc[-1])
        if current_vix < p['vix_low_threshold']:
            print(f'[debug] signals=0 vix={current_vix:.1f}<{p["vix_low_threshold"]}',
                  file=sys.stderr)
            return []

        # VIX scalar: 1.0 at median, >1.0 when elevated, <1.0 when subdued
        med_window = min(p['vix_med_window'], len(vix_series))
        vix_median = float(vix_series.iloc[-med_window:].median())
        vix_scalar = current_vix / max(vix_median, 1.0)

        # ── 2. Universe + 1-week returns ─────────────────────────────────────
        candidates = [t for t in universe if t in prices.columns]
        if len(candidates) < p['min_universe']:
            print(f'[debug] signals=0 universe_too_small={len(candidates)}',
                  file=sys.stderr)
            return []

        ret_map: dict[str, float] = {}
        price_map: dict[str, float] = {}
        win = p['ret_window']

        for ticker in candidates:
            ts = prices[ticker].dropna()
            if len(ts) < win + 2:
                continue
            cur = float(ts.iloc[-1])
            past = float(ts.iloc[-(win + 1)])
            if past <= 0 or cur < p['min_price']:
                continue
            ret_map[ticker] = (cur - past) / past
            price_map[ticker] = cur

        if len(ret_map) < p['min_universe']:
            print(f'[debug] signals=0 after_filter={len(ret_map)}', file=sys.stderr)
            return []

        # ── 3. Quintile split ────────────────────────────────────────────────
        sorted_tickers = sorted(ret_map, key=lambda t: ret_map[t])
        n = len(sorted_tickers)
        q_size = max(1, n // p['quintile_n'])

        long_tickers  = sorted_tickers[:q_size]           # bottom quintile (losers)
        short_tickers = sorted_tickers[n - q_size:]       # top quintile (winners)

        # ── 4. Gate on VIX threshold ─────────────────────────────────────────
        # Activate fully above high_threshold; partial above low_threshold
        if current_vix < p['vix_high_threshold']:
            vix_gate = (current_vix - p['vix_low_threshold']) / (
                p['vix_high_threshold'] - p['vix_low_threshold']
            )
        else:
            vix_gate = 1.0

        scale = self.position_scale(regime_state)
        conf = 'HIGH' if current_vix >= 25.0 else ('MED' if current_vix >= p['vix_high_threshold'] else 'LOW')
        signals: List[Signal] = []

        for ticker, direction in (
            [(t, 'LONG') for t in long_tickers] +
            [(t, 'SHORT') for t in short_tickers]
        ):
            cur = price_map[ticker]
            ts  = prices[ticker].dropna()
            stops = self.compute_stops_and_targets(
                ts, direction, cur, regime_state=regime_state
            )
            size = min(
                p['base_size_pct'] * scale * vix_scalar * vix_gate,
                p['max_size_pct']
            )
            signals.append(Signal(
                ticker=ticker,
                direction=direction,
                entry_price=round(cur, 4),
                stop_loss=round(stops['stop'], 4),
                target_1=round(stops['t1'], 4),
                target_2=round(stops['t2'], 4),
                target_3=round(stops['t3'], 4),
                position_size_pct=round(max(size, 0.001), 4),
                confidence=conf,
                signal_params={
                    'ret_1w':      round(ret_map[ticker], 5),
                    'vix':         round(current_vix, 2),
                    'vix_scalar':  round(vix_scalar, 3),
                    'vix_gate':    round(vix_gate, 3),
                    'regime':      regime_state,
                },
            ))
            if len(signals) >= self.MAX_SIGNALS:
                break

        print(f'[debug] signals={len(signals)} vix={current_vix:.1f} scalar={vix_scalar:.2f}',
              file=sys.stderr)
        return signals

    # ── helpers ───────────────────────────────────────────────────────────────

    def _get_vix(self, macro_df) -> 'pd.Series | None':
        """Extract VIX as a date-indexed Series from the long-format macro DataFrame."""
        if macro_df is None or not isinstance(macro_df, pd.DataFrame):
            return None
        if macro_df.empty or 'series' not in macro_df.columns:
            return None
        vix_rows = macro_df[macro_df['series'] == 'VIX'].copy()
        if vix_rows.empty:
            return None
        vix_rows['date'] = pd.to_datetime(vix_rows['date'])
        vix_rows = vix_rows.sort_values('date').drop_duplicates('date')
        return vix_rows.set_index('date')['value'].astype(float)
