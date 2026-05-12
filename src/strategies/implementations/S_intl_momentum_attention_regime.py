"""
International 12-1 momentum filtered by market state and realized vol regime.
Source: Goyal, Jegadeesh, Subrahmanyam (2024), RoF — DOI: 10.1093/rof/rfae038
"""
from __future__ import annotations
import sys
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

__all__ = ['IntlMomentumAttentionRegime']


class IntlMomentumAttentionRegime(BaseStrategy):
    """12-1 month momentum LONG/SHORT top/bottom quintile in up-markets and low-vol regimes."""

    id                = 'S_intl_momentum_attention_regime'
    name              = 'IntlMomentumAttentionRegime'
    description       = '12-1m momentum top/bottom quintile filtered by up-market and low realized-vol'
    tier              = 2
    min_lookback      = 504
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

    def default_parameters(self) -> dict:
        return {
            'mom_lookback':    252,   # 12 months
            'skip_months':     21,    # ~1 month skip
            'quintile_n':      5,
            'vol_window':      63,    # realized vol rolling window
            'market_window':   63,    # trailing market return window
            'base_size':       0.02,  # 2% per position
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
        mom_lb   = p['mom_lookback']
        skip     = p['skip_months']
        q_n      = p['quintile_n']
        vol_win  = p['vol_window']
        mkt_win  = p['market_window']
        base_sz  = p['base_size']
        scale    = self.position_scale(regime_state)

        # Filter to universe tickers present in prices
        cols = [t for t in universe if t in prices.columns]
        if not cols:
            print('[debug] signals=0', file=sys.stderr)
            return []

        px = prices[cols].dropna(how='all')
        if len(px) < mom_lb + skip + 5:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # ── Market-state filter ──────────────────────────────────────────────
        # Use median return across universe as a proxy for market return
        rets = px.pct_change()
        mkt_return = rets.mean(axis=1).rolling(mkt_win).sum().iloc[-1]
        market_up = bool(mkt_return > 0)

        if not market_up:
            # Strategy is FLAT in down markets
            print('[debug] signals=0', file=sys.stderr)
            return []

        # ── Realized-vol filter ──────────────────────────────────────────────
        # Cross-sectional median of realized vol
        rv_latest = rets.rolling(vol_win).std().iloc[-1]
        rv_median = float(rv_latest.median())

        # ── Momentum score (12m return, skip last 1m) ────────────────────────
        end_px   = px.iloc[-skip - 1]
        start_px = px.iloc[-(mom_lb + skip) - 1] if len(px) >= mom_lb + skip + 1 else px.iloc[0]
        mom_score = (end_px / start_px - 1.0).dropna()

        if mom_score.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        ranked = mom_score.rank(pct=True)
        n_per_q = max(1, len(ranked) // q_n)

        # Top and bottom quintile
        top_tickers    = ranked[ranked >= (1 - 1 / q_n)].index.tolist()
        bottom_tickers = ranked[ranked <= (1 / q_n)].index.tolist()

        signals: List[Signal] = []

        for ticker in top_tickers[:self.MAX_SIGNALS // 2]:
            if ticker not in px.columns:
                continue
            rv_t = float(rv_latest.get(ticker, rv_median))
            if rv_t > rv_median:
                continue  # Skip high-vol names even in top quintile

            price_series = px[ticker].dropna()
            if price_series.empty:
                continue
            current_price = float(price_series.iloc[-1])
            st = self.compute_stops_and_targets(
                price_series, 'LONG', current_price, regime_state=regime_state
            )
            confidence = 'HIGH' if ranked[ticker] >= 0.95 else 'MED'
            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = current_price,
                stop_loss         = st['stop'],
                target_1          = st['t1'],
                target_2          = st['t2'],
                target_3          = st['t3'],
                position_size_pct = round(base_sz * scale, 4),
                confidence        = confidence,
                signal_params     = {
                    'mom_12_1':   round(float(mom_score[ticker]), 4),
                    'rank_pct':   round(float(ranked[ticker]), 4),
                    'rv_ratio':   round(rv_t / rv_median, 4),
                },
            ))

        for ticker in bottom_tickers[:self.MAX_SIGNALS // 2]:
            if ticker not in px.columns:
                continue
            rv_t = float(rv_latest.get(ticker, rv_median))
            if rv_t > rv_median:
                continue

            price_series = px[ticker].dropna()
            if price_series.empty:
                continue
            current_price = float(price_series.iloc[-1])
            st = self.compute_stops_and_targets(
                price_series, 'SHORT', current_price, regime_state=regime_state
            )
            confidence = 'HIGH' if ranked[ticker] <= 0.05 else 'MED'
            signals.append(Signal(
                ticker            = ticker,
                direction         = 'SHORT',
                entry_price       = current_price,
                stop_loss         = st['stop'],
                target_1          = st['t1'],
                target_2          = st['t2'],
                target_3          = st['t3'],
                position_size_pct = round(base_sz * scale, 4),
                confidence        = confidence,
                signal_params     = {
                    'mom_12_1':   round(float(mom_score[ticker]), 4),
                    'rank_pct':   round(float(ranked[ticker]), 4),
                    'rv_ratio':   round(rv_t / rv_median, 4),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
