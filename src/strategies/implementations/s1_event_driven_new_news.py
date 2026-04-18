"""S1: Event-Driven New News — IV Term Structure + Post-Event Momentum.

Identify scheduled catalyst events (earnings, macro releases) in the SP500 universe.
Pre-event: IV30/IV7 ratio > 1.30 (far vol elevated vs near) → BUY_VOL straddle 2 days before.
Post-event: price move > 1.5σ vs ATM implied move → directional momentum (LONG/SHORT).
Regime: LOW_VOL / TRANSITIONING only (VIX < 35 equivalent).
"""
from __future__ import annotations
import math
import pandas as pd
from typing import List
from ..base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['EventDrivenNewNews']

IV_TERM_RATIO_MIN  = 1.30   # IV30/IV7 threshold: far vol elevated vs near
EARNINGS_PRE_MIN   = 1      # days before event: open entry window
EARNINGS_PRE_MAX   = 5      # days before event: close entry window
MOMENTUM_SIGMA_MIN = 1.5    # post-event move threshold (× ATM implied 1-day sigma)
MIN_NEAR_IV        = 0.15   # filter illiquid names
TOP_N              = 6      # max signals per cycle
AVOID_REGIMES      = {'HIGH_VOL', 'CRISIS'}


class EventDrivenNewNews(BaseStrategy):
    id              = 'S1_event_driven_new_news'
    name            = 'Event-Driven New News'
    description     = 'IV term structure crush + post-event momentum on SP500 catalyst events'
    tier            = 2
    signal_frequency = 'daily'
    min_lookback    = 21
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or (isinstance(prices, pd.DataFrame) and prices.empty):
            return []
        if not aux_data:
            return []

        regime_state = regime.get('state', 'LOW_VOL') if isinstance(regime, dict) else 'LOW_VOL'
        if regime_state in AVOID_REGIMES:
            return []

        pos_scale    = REGIME_POSITION_SCALE.get(regime_state, 0.55)
        options_data = aux_data.get('options', {})
        if not options_data:
            return []

        pre_cands:  list = []
        post_cands: list = []

        for ticker in universe:
            opts = options_data.get(ticker)
            if not opts:
                continue

            price = opts.get('last_price') or opts.get('close')
            if not price or price <= 0:
                continue

            near_iv      = opts.get('near_iv') or opts.get('iv_7d')
            far_iv       = opts.get('far_iv')  or opts.get('iv_30d')
            iv_rank      = float(opts.get('iv_rank') or 50.0)
            earnings_dte = opts.get('earnings_dte')

            if near_iv is None or far_iv is None or near_iv <= 0 or far_iv <= 0:
                continue
            if near_iv < MIN_NEAR_IV:
                continue

            iv_term_ratio = float(far_iv) / float(near_iv)
            prices_series = self._get_prices_series(prices, ticker)

            # --- PRE-EVENT: straddle entry ---
            if earnings_dte is not None and EARNINGS_PRE_MIN <= earnings_dte <= EARNINGS_PRE_MAX:
                if iv_term_ratio >= IV_TERM_RATIO_MIN:
                    score = iv_term_ratio * (iv_rank / 100.0)
                    pre_cands.append((score, ticker, float(price), float(near_iv),
                                      float(far_iv), iv_term_ratio, iv_rank,
                                      int(earnings_dte), prices_series))

            # --- POST-EVENT: directional momentum ---
            elif earnings_dte is not None and -3 <= earnings_dte <= 0:
                prev_price = opts.get('prev_close') or opts.get('prev_price')
                if not prev_price or prev_price <= 0:
                    continue
                implied_1d   = float(near_iv) * math.sqrt(1.0 / 252.0)
                realized_ret = abs(float(price) - float(prev_price)) / float(prev_price)
                sigma_move   = realized_ret / implied_1d if implied_1d > 0 else 0.0
                if sigma_move >= MOMENTUM_SIGMA_MIN:
                    direction = 'LONG' if float(price) > float(prev_price) else 'SHORT'
                    post_cands.append((sigma_move, ticker, float(price), direction,
                                       float(near_iv), iv_rank, prices_series))

        pre_cands.sort(key=lambda x: x[0], reverse=True)
        post_cands.sort(key=lambda x: x[0], reverse=True)

        signals: List[Signal] = []
        pre_limit  = (TOP_N + 1) // 2
        post_limit = TOP_N // 2

        # Pre-event BUY_VOL signals
        for score, ticker, price, near_iv, far_iv, ratio, iv_rank, dte, prices_series in pre_cands[:pre_limit]:
            days     = max(dte, 1)
            straddle = 2.0 * near_iv * math.sqrt(days / 252.0) * price
            base_sz  = 0.015 + 0.010 * min((ratio - IV_TERM_RATIO_MIN) / 0.30, 1.0)
            signals.append(Signal(
                ticker            = ticker,
                direction         = 'BUY_VOL',
                entry_price       = price,
                stop_loss         = round(price - straddle * 0.60, 2),
                target_1          = round(price + straddle * 1.00, 2),
                target_2          = round(price + straddle * 1.50, 2),
                target_3          = round(price + straddle * 2.00, 2),
                position_size_pct = round(base_sz * pos_scale, 4),
                confidence        = 'HIGH' if ratio >= 1.50 and iv_rank >= 60 else 'MED',
                signal_params     = {
                    'strategy_id':    self.id,
                    'phase':          'pre_event',
                    'earnings_dte':   dte,
                    'iv_term_ratio':  round(ratio, 4),
                    'near_iv':        round(near_iv, 4),
                    'far_iv':         round(far_iv, 4),
                    'iv_rank':        round(iv_rank, 2),
                },
            ))

        # Post-event directional signals
        for sigma, ticker, price, direction, near_iv, iv_rank, prices_series in post_cands[:post_limit]:
            if prices_series is not None and len(prices_series) >= 5:
                stops = self.compute_stops_and_targets(
                    prices_series=prices_series,
                    direction=direction,
                    current_price=price,
                    regime_state=regime_state,
                )
            else:
                pct = 0.06
                stops = {
                    'stop': round(price * (1 - pct) if direction == 'LONG' else price * (1 + pct), 2),
                    't1':   round(price * (1.05 if direction == 'LONG' else 0.95), 2),
                    't2':   round(price * (1.10 if direction == 'LONG' else 0.90), 2),
                    't3':   round(price * (1.14 if direction == 'LONG' else 0.86), 2),
                }
            size = round(0.020 * min(sigma / MOMENTUM_SIGMA_MIN, 2.0) * pos_scale, 4)
            signals.append(Signal(
                ticker            = ticker,
                direction         = direction,
                entry_price       = price,
                stop_loss         = float(stops['stop']),
                target_1          = float(stops['t1']),
                target_2          = float(stops['t2']),
                target_3          = float(stops['t3']),
                position_size_pct = size,
                confidence        = 'HIGH' if sigma >= 2.5 else 'MED',
                signal_params     = {
                    'strategy_id': self.id,
                    'phase':       'post_event',
                    'sigma_move':  round(sigma, 3),
                    'near_iv':     round(near_iv, 4),
                    'iv_rank':     round(iv_rank, 2),
                },
            ))

        return signals

    # ------------------------------------------------------------------ helpers
    def _get_prices_series(self, prices: pd.DataFrame, ticker: str):
        """Extract a price series for ATR computation. Returns None if unavailable."""
        try:
            if isinstance(prices, pd.DataFrame) and not prices.empty:
                if ticker in prices.columns:
                    return prices[ticker].dropna()
                if 'ticker' in prices.columns and 'close' in prices.columns:
                    px = prices[prices['ticker'] == ticker].sort_values('date')
                    if not px.empty:
                        return px['close'].reset_index(drop=True)
        except Exception:
            pass
        return None
