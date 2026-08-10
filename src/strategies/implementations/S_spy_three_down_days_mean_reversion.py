"""
SPY Three Down Days Mean Reversion — S_spy_three_down_days_mean_reversion

Three consecutive bearish candles on SPY (close < open each day) signal a
short-term mean-reversion bounce; entering long at close on day 3 and exiting
on the first bullish candle captures a persistent oversold snap-back in the
S&P 500 ETF.

Source: https://tradinginvestingstrategies.substack.com/p/the-famous-3-down-days-trading-strategy-backtest
Reported: win_rate=66.8%, profit_factor=1.80, max_drawdown=22.5%, 603 trades (1993–2025)
"""
from __future__ import annotations

import os
import sys
import numpy as np
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['SpyThreeDownDaysMeanReversion']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID      = 'S_spy_three_down_days_mean_reversion'

TICKER = 'SPY'
# Position size: 10% per signal, scaled by regime
BASE_POS_SIZE = 0.10


class SpyThreeDownDaysMeanReversion(BaseStrategy):
    """Three consecutive bearish SPY candles → LONG mean-reversion bounce."""

    id                = STRATEGY_ID
    name              = 'SPY Three Down Days Mean Reversion'
    description       = (
        'Three consecutive bearish candles on SPY (close < open each day) '
        'signal a short-term mean-reversion bounce into the next bullish session.'
    )
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = 5
    # Mean-reversion works across calm and volatile regimes; exclude CRISIS
    # where gap-risk overwhelms any snap-back edge.
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 1

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

        # Need OHLC columns for SPY; prices DataFrame is wide (ticker columns) or
        # may be a multi-level column frame.  We support both layouts:
        #   (a) flat columns: 'SPY_open', 'SPY_close'  — suffix style
        #   (b) multi-index: (price_type, ticker)       — level style
        #   (c) single-ticker close-only column 'SPY'
        close_col = open_col = None

        if isinstance(prices.columns, pd.MultiIndex):
            # (b) multi-index — look for ('close', 'SPY') and ('open', 'SPY')
            if ('close', TICKER) in prices.columns and ('open', TICKER) in prices.columns:
                close_series = prices[('close', TICKER)].dropna()
                open_series  = prices[('open', TICKER)].dropna()
            else:
                print(f'[{STRATEGY_ID}] MultiIndex missing close/open for {TICKER}', file=sys.stderr)
                print('[debug] signals=0', file=sys.stderr)
                return []
        elif f'{TICKER}_close' in prices.columns and f'{TICKER}_open' in prices.columns:
            # (a) suffix style
            close_series = prices[f'{TICKER}_close'].dropna()
            open_series  = prices[f'{TICKER}_open'].dropna()
        elif TICKER in prices.columns:
            # (c) close-only — synthesize open = prev close as a fallback
            print(f'[{STRATEGY_ID}] WARNING: close-only panel — synthesizing open=prev_close; '
                  f'"bearish candle" degrades to a down-close day', file=sys.stderr)
            close_series = prices[TICKER].dropna()
            open_series  = close_series.shift(1).dropna()
            close_series = close_series.loc[open_series.index]
        else:
            print(f'[{STRATEGY_ID}] {TICKER} not in prices', file=sys.stderr)
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Align the two series
        aligned = pd.DataFrame({'close': close_series, 'open': open_series}).dropna()
        if len(aligned) < 4:
            print(f'[{STRATEGY_ID}] insufficient rows: {len(aligned)}', file=sys.stderr)
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Bearish candle: close < open (each bar independently)
        bearish = aligned['close'] < aligned['open']

        # Check the last 3 bars
        if len(bearish) < 3:
            print('[debug] signals=0', file=sys.stderr)
            return []

        last3 = bearish.iloc[-3:]
        if not last3.all():
            # Not three consecutive bearish candles — no signal
            print('[debug] signals=0', file=sys.stderr)
            return []

        current_price = float(aligned['close'].iloc[-1])
        scale = self.position_scale(regime_state)

        # Confidence: higher when the three-day decline is deeper
        three_day_ret = (aligned['close'].iloc[-1] / aligned['close'].iloc[-4] - 1.0
                         if len(aligned) >= 4 else -0.01)
        if three_day_ret < -0.03:
            confidence = 'HIGH'
        elif three_day_ret < -0.015:
            confidence = 'MED'
        else:
            confidence = 'LOW'

        stops = self.compute_stops_and_targets(
            aligned['close'],
            direction='LONG',
            current_price=current_price,
            regime_state=regime_state,
        )

        print(
            f'[{STRATEGY_ID}] 3-down-days detected: '
            f'px={current_price:.2f} 3d_ret={three_day_ret:.3%} '
            f'regime={regime_state} confidence={confidence}',
            file=sys.stderr,
        )
        print('[debug] signals=1', file=sys.stderr)

        return [Signal(
            ticker            = TICKER,
            direction         = 'LONG',
            entry_price       = current_price,
            stop_loss         = stops['stop'],
            target_1          = stops['t1'],
            target_2          = stops['t2'],
            target_3          = stops['t3'],
            position_size_pct = round(BASE_POS_SIZE * scale, 4),
            confidence        = confidence,
            signal_params     = {
                'three_day_return': round(float(three_day_ret), 6),
                'bearish_streak':   3,
                'regime':           regime_state,
                'scale':            scale,
            },
        )]


# ---------------------------------------------------------------------------
# Regime-partitioned backtest (required by lifecycle promotion gate)
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    from backtest.quick_backtest import run_backtest_with_regime_partition

    PARQUET_ROOT = os.environ.get('OPENCLAW_PARQUET_ROOT', '/root/openclaw/data/master')
    # Long master panel (ticker/date/open/close …): predicate-pushdown to SPY rows and the
    # real open + close columns only — never load the whole parquet.
    prices  = pd.read_parquet(
        os.path.join(PARQUET_ROOT, 'prices.parquet'),
        columns=['ticker', 'date', 'open', 'close'],
        filters=[('ticker', '==', TICKER)],
    )
    prices  = prices[prices['ticker'] == TICKER].drop(columns=['ticker'])
    prices['date'] = pd.to_datetime(prices['date'])
    prices  = prices.set_index('date').sort_index()
    regimes = pd.read_parquet(os.path.join(PARQUET_ROOT, 'historical_regimes.parquet'))

    if 'date' in regimes.columns:
        regimes = regimes.set_index('date')
    regime_series = (
        regimes['regime_state'] if 'regime_state' in regimes.columns else regimes.iloc[:, 0]
    )

    # Real OHLC open from the master panel — no prev-close synthesis needed
    aligned = prices[['close', 'open']].dropna()

    bearish = aligned['close'] < aligned['open']
    rows = []
    for i in range(3, len(aligned)):  # earliest valid signal: bars 0-2 bearish, entry at bar 2
        last3 = bearish.iloc[i - 3 : i]
        if not last3.all():
            continue

        dt = aligned.index[i - 1]
        try:
            r_state = str(regime_series.loc[dt])
        except (KeyError, TypeError):
            r_state = 'LOW_VOL'

        entry_px = float(aligned['close'].iloc[i - 1])
        # Exit: walk forward to the first bullish candle (close > open) and exit at that
        # bar's close; max-hold 10 bars so an extended bearish streak can't walk forever
        exit_px = float(aligned['close'].iloc[min(i + 9, len(aligned) - 1)])
        for j in range(i, min(i + 10, len(aligned))):
            if float(aligned['close'].iloc[j]) > float(aligned['open'].iloc[j]):
                exit_px = float(aligned['close'].iloc[j])
                break

        pnl       = exit_px - entry_px
        r_multiple = pnl / max(entry_px * 0.02, 1e-9)

        rows.append({
            'strategy_id':  STRATEGY_ID,
            'signal_date':  str(dt.date() if hasattr(dt, 'date') else dt)[:10],
            'regime_state': r_state,
            'pnl':          float(pnl),
            'r_multiple':   float(r_multiple),
        })

    trades_df = pd.DataFrame(rows)
    print(f'[backtest] total trades: {len(trades_df)}', file=sys.stderr)
    result = run_backtest_with_regime_partition(
        trades_df,
        strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    import json
    print(json.dumps(result, indent=2, default=str))
