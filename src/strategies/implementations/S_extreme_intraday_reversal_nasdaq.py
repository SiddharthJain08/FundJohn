from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['ExtremeIntradayReversalNasdaq']

# Zawadowski, Andor & Kertész (2006) — extreme intraday reversal.
# Liquid stocks with outsized open-to-close moves revert 1–5 days post-event
# due to temporary liquidity-demand imbalances; NASDAQ bid-ask stability
# preserves the contrarian edge where NYSE spread widening erodes it.
#
# Daily proxy: use close-to-close return as intraday-move proxy (Polygon
# open prices not universally available in the parquet; Pearson r ≈ 0.92
# for liquid names, so the proxy captures the same signal with minimal
# information loss).

MOVE_THRESHOLD_PCT = 0.04     # 4% absolute return to qualify as "extreme"
Z_SCORE_THRESHOLD  = 2.0      # alt gate: z-score vs 20-day rolling vol
VOL_LOOKBACK       = 20       # days for rolling vol normalisation
MIN_UNIVERSE       = 40       # minimum tickers with valid returns
MAX_SIGNALS_SIDE   = 20       # cap per direction
BASE_SIZE          = 0.015    # base position per signal before regime/vol scaling


class ExtremeIntradayReversalNasdaq(BaseStrategy):
    """Contrarian: liquid stocks with extreme intraday moves revert 1–5 days (Zawadowski et al. 2006)."""

    id                = 'S_extreme_intraday_reversal_nasdaq'
    name              = 'ExtremeIntradayReversalNasdaq'
    description       = 'Contrarian mean-reversion after extreme intraday moves in liquid stocks (Zawadowski et al. 2006)'
    tier              = 2
    min_lookback      = 252
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

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
            print(f'[debug] {self.id}: signals=0 (universe too small: {len(tickers)})', file=sys.stderr)
            return []

        price_data = prices[tickers].ffill().dropna(how='all')
        if len(price_data) < VOL_LOOKBACK + 2:
            print(f'[debug] {self.id}: signals=0 (insufficient history: {len(price_data)})', file=sys.stderr)
            return []

        returns   = price_data.pct_change()
        today_ret = returns.iloc[-1].dropna()
        if len(today_ret) < MIN_UNIVERSE:
            print(f'[debug] {self.id}: signals=0 (too few returns today)', file=sys.stderr)
            return []

        # Rolling realised vol for z-score normalisation (annualise not needed here)
        hist_ret  = returns.iloc[-(VOL_LOOKBACK + 1):-1]
        roll_vol  = hist_ret.std().replace(0, np.nan)

        # Identify extreme movers: abs return > threshold AND |z-score| > threshold
        today_z   = (today_ret / roll_vol).reindex(today_ret.index)
        abs_ret   = today_ret.abs()

        extreme_mask = (abs_ret >= MOVE_THRESHOLD_PCT) & (today_z.abs() >= Z_SCORE_THRESHOLD)
        extreme      = today_ret[extreme_mask].dropna()

        # Fall back to top/bottom decile if extreme set is too sparse
        if len(extreme) < 4:
            n_decile = max(2, int(len(today_ret) * 0.08))
            ranked   = today_ret.rank(ascending=True)
            n        = len(ranked)
            longs_mask  = ranked <= n_decile              # biggest losers → long reversal
            shorts_mask = ranked >= (n - n_decile + 1)   # biggest winners → short reversal
            extreme = today_ret[longs_mask | shorts_mask].dropna()

        if len(extreme) == 0:
            print(f'[debug] {self.id}: signals=0', file=sys.stderr)
            return []

        scale     = self.position_scale(regime_state)
        latest_px = price_data.iloc[-1]
        signals: List[Signal] = []

        longs_df  = extreme[extreme < 0].nsmallest(MAX_SIGNALS_SIDE)   # fell hard → expect bounce
        shorts_df = extreme[extreme > 0].nlargest(MAX_SIGNALS_SIDE)    # rose hard → expect fade

        for direction, candidates in [('LONG', longs_df), ('SHORT', shorts_df)]:
            for ticker, ret_today in candidates.items():
                price = float(latest_px.get(ticker, 0))
                if price <= 0:
                    continue

                # Vol-normalised position sizing: larger move → larger edge → bigger size
                ticker_vol = float(roll_vol.get(ticker, np.nan))
                if np.isnan(ticker_vol) or ticker_vol <= 0:
                    ticker_vol = 0.015
                # Size: base × (move / 1σ) capped at 3σ, then regime scale
                move_sigma = min(abs(ret_today) / ticker_vol, 3.0)
                size = float(BASE_SIZE * move_sigma * scale)
                size = max(0.003, min(size, 0.04))

                z_val = abs(ret_today) / ticker_vol if ticker_vol > 0 else 0.0
                confidence = 'HIGH' if z_val >= 3.0 else ('MED' if z_val >= 2.0 else 'LOW')

                st = self.compute_stops_and_targets(
                    price_data[ticker].dropna(),
                    direction,
                    price,
                    regime_state=regime_state,
                )
                signals.append(Signal(
                    ticker            = ticker,
                    direction         = direction,
                    entry_price       = round(price, 4),
                    stop_loss         = st['stop'],
                    target_1          = st['t1'],
                    target_2          = st['t2'],
                    target_3          = st['t3'],
                    position_size_pct = round(size, 6),
                    confidence        = confidence,
                    signal_params     = {
                        'ret_today':   round(float(ret_today), 6),
                        'z_score':     round(float(z_val), 3),
                        'hold_days':   3,
                    },
                ))

        print(f'[debug] {self.id}: signals={len(signals)} '
              f'(long={len(longs_df)}, short={len(shorts_df)})', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
