from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal

__all__ = ['EODCrossSectionalReversal']


# Zarattini & Staley (2023) ROD3 cross-sectional reversal.
# Original: rank return from prior-close to 15:00 ET intraday; short top, long bottom.
# EOD version: use full-day open-to-close return as ROD3 proxy (existing daily data).
# Signal: cross-sectional bottom decile → LONG (reversal next open), top → SHORT.

DECILE_FRAC      = 0.10   # top/bottom 10%
MIN_UNIVERSE     = 50     # need enough tickers for cross-section to be meaningful
BASE_SIZE_PCT    = 0.010  # smaller per-position: high diversification strategy
LOOKBACK_VOL     = 21     # vol-normalize position sizes
MIN_LOOKBACK_DAYS = 22


class EODCrossSectionalReversal(BaseStrategy):
    """
    EOD cross-sectional reversal (Zarattini & Staley 2023 ROD3 proxy).

    Daily return cross-section:
    - Bottom decile (largest daily losers)  → LONG (expect next-open reversal)
    - Top decile    (largest daily winners) → SHORT (expect next-open reversal)

    Uses existing daily prices.parquet (454 tickers). No new data source required.
    Vol-normalizes position sizes. Active TRANSITIONING only.
    """

    id                = 'S_tr_06_eod_reversal'
    name              = 'EODCrossSectionalReversal'
    description       = 'Cross-sectional EOD return reversal — short top decile, long bottom decile (ROD3 proxy, Zarattini 2023)'
    tier              = 1
    active_in_regimes = ['TRANSITIONING']
    min_lookback      = MIN_LOOKBACK_DAYS

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

        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < MIN_UNIVERSE:
            print(f'[debug] {self.id}: signals=0 (need {MIN_UNIVERSE} tickers, got {len(tickers)})', file=sys.stderr)
            return []

        price_data = prices[tickers].ffill().dropna(how='all')
        if len(price_data) < self.min_lookback:
            print(f'[debug] {self.id}: signals=0 (need {self.min_lookback} rows)', file=sys.stderr)
            return []

        returns    = price_data.pct_change()
        today_ret  = returns.iloc[-1].dropna()
        latest_px  = price_data.iloc[-1]

        if len(today_ret) < MIN_UNIVERSE:
            return []

        # Cross-sectional rank of today's return (proxy for ROD3)
        ranked     = today_ret.rank(ascending=True)
        n          = len(ranked)
        n_decile   = max(1, int(n * DECILE_FRAC))
        longs      = ranked[ranked <= n_decile].index.tolist()       # bottom decile
        shorts     = ranked[ranked >= (n - n_decile + 1)].index.tolist()  # top decile

        # Vol for position sizing
        vol = returns[today_ret.index].iloc[-LOOKBACK_VOL:].std() * np.sqrt(252)
        vol = vol.replace(0, np.nan)

        scale        = self.position_scale(regime_state)
        signals: List[Signal] = []
        max_per_side = self.MAX_SIGNALS // 2

        for direction, candidates in [('LONG', longs[:max_per_side]), ('SHORT', shorts[:max_per_side])]:
            for ticker in candidates:
                price = float(latest_px.get(ticker, 0))
                if price <= 0:
                    continue
                ticker_vol = float(vol.get(ticker, 0.25))
                if np.isnan(ticker_vol) or ticker_vol <= 0:
                    ticker_vol = 0.25
                # Target 15% annualised vol per position
                size = float(BASE_SIZE_PCT * (0.15 / ticker_vol) * scale)
                size = max(0.001, min(size, 0.03))

                ret_today  = float(today_ret.get(ticker, 0))
                rank_pct   = float(ranked.get(ticker, 0)) / n
                confidence = 'HIGH' if abs(ret_today) > 0.03 else ('MED' if abs(ret_today) > 0.015 else 'LOW')

                st = self.compute_stops_and_targets(
                    price_data[ticker].dropna(), direction, price, regime_state=regime_state,
                )
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
                        'ret_today':  round(ret_today, 6),
                        'rank_pct':   round(rank_pct, 3),
                        'vol_annual': round(ticker_vol, 4),
                    },
                ))

        print(f'[debug] {self.id}: signals={len(signals)} '
              f'(long={len(longs)}, short={len(shorts)})', file=sys.stderr)
        return signals
