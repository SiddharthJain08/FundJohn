"""
RSI(2) Pullback Mean-Reversion on Gold (GLD) — daily-bar adaptation.
Source: https://yuratrading.com/blogs/rsi2-pullback-gold
Title:  "Every Configuration Lost Money: RSI(2) Mean Reversion Meets Gold"

The source paper re-tests Connors' classic daily-equity RSI(2) pullback rule
on 1-hour gold bars with real tick-data costs and reports that EVERY swept
configuration lost money (win size too small to clear round-trip costs).
Hourly gold bars are not in our data ledger (`data_requirements` lists only
daily `prices`), so this is a daily-bar adaptation of the same thesis — a
forced substitution, not an interpretive choice.

Interpretation variant 2 of 2 (deliberately different, still defensible,
choices on the paper's ambiguous parameters — contrast with variant 1,
`S_rsi2_hourly_gold_meanrev_tv1.py`):
  - No trend filter. The paper's pseudocode is literally
    "IF RSI(2) < oversold THEN enter LONG; exit on RSI(2) > exit OR a
    time-stop" — no SMA/regime quality gate. Variant 1 added a SMA(200)
    filter as a common Connors-style refinement; this variant takes the
    pseudocode at face value and fires on every RSI(2) dip.
  - Classic Connors(1,2) thresholds: RSI(2) < 10 entry / RSI(2) > 70 exit
    (tighter entry, looser exit than variant 1's 15/65 — the textbook
    parameterization rather than a widened sweep).
  - Longer 20-trading-day time stop (vs. variant 1's 10). With no trend
    filter pre-screening setups, an unfiltered RSI(2) dip needs more room
    to mean-revert before being forced out on time.
  - A more punishing 12 bps round-trip cost assumption (vs. variant 1's
    5 bps) — this variant stress-tests the paper's central finding (real
    tick-data costs are what sink every configuration) with a cost
    structure closer to that finding's spirit, while still plausible for
    a liquid gold ETF.
"""
from __future__ import annotations
import sys
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal

__all__ = ['Rsi2HourlyGoldMeanrevTv2']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID      = 'S_rsi2_hourly_gold_meanrev_tv2'
TICKER           = 'GLD'
RSI_PERIOD        = 2
RSI_OVERSOLD      = 10.0
RSI_EXIT          = 70.0
TIME_STOP_DAYS    = 20
ROUND_TRIP_COST   = 0.0012   # 12 bps, more punishing round-trip cost assumption


class Rsi2HourlyGoldMeanrevTv2(BaseStrategy):
    """RSI(2) pullback mean-reversion, long-only, on GLD — no trend filter,
    classic Connors(10/70) thresholds, 20-day time stop (daily-bar adaptation
    of an hourly-gold falsification study — source paper found all swept
    configurations lost money net of realistic tick-data costs).
    """

    id                = STRATEGY_ID
    name              = 'RSI(2) Hourly Gold Mean-Reversion v2 (no trend filter)'
    description       = (
        'RSI(2) < 10 pullback (no trend filter) -> LONG GLD; exit on RSI(2) '
        '> 70 or a 20-day time stop. Daily-bar adaptation of a paper '
        'reporting the hourly-bar version loses money net of realistic costs.'
    )
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 30
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 1

    def default_parameters(self) -> dict:
        return {
            'rsi_oversold': RSI_OVERSOLD,
            'rsi_exit': RSI_EXIT,
            'time_stop_days': TIME_STOP_DAYS,
        }

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
        if TICKER not in prices.columns:
            return []

        series = prices[TICKER].dropna()
        if len(series) < self.min_lookback:
            return []

        current_price = float(series.iloc[-1])
        if pd.isna(current_price) or current_price <= 0:
            return []

        rsi_oversold = float(self.parameters.get('rsi_oversold', RSI_OVERSOLD))
        rsi_exit     = float(self.parameters.get('rsi_exit', RSI_EXIT))

        rsi_series = self.ind.rsi(series, RSI_PERIOD)
        rsi_now    = float(rsi_series.iloc[-1])
        if pd.isna(rsi_now):
            return []

        scale   = self.position_scale(regime_state)
        signals: List[Signal] = []

        if rsi_now > rsi_exit:
            # RSI-driven exit — harmless no-op if no open position on this ticker.
            signals.append(Signal(
                ticker=TICKER, direction='FLAT', entry_price=current_price,
                stop_loss=round(current_price * 0.95, 4),
                target_1=current_price, target_2=current_price, target_3=current_price,
                position_size_pct=0.0, confidence='MED',
                signal_params={'trigger': 'rsi2_exit', 'rsi2': round(rsi_now, 2),
                               'rsi_exit_threshold': rsi_exit, 'regime': regime_state},
            ))
        elif rsi_now < rsi_oversold:
            st = self.compute_stops_and_targets(series, 'LONG', current_price, regime_state=regime_state)
            signals.append(Signal(
                ticker=TICKER, direction='LONG', entry_price=current_price,
                stop_loss=st['stop'], target_1=st['t1'], target_2=st['t2'], target_3=st['t3'],
                position_size_pct=round(0.35 * scale, 4), confidence='LOW',
                signal_params={'trigger': 'rsi2_oversold_no_trend_filter', 'rsi2': round(rsi_now, 2),
                               'rsi_oversold_threshold': rsi_oversold, 'rsi_exit_threshold': rsi_exit,
                               'time_stop_days': TIME_STOP_DAYS, 'trend_filter': 'none_pure_pseudocode',
                               'regime': regime_state, 'scale': scale},
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


# ---------------------------------------------------------------------------
# Regime-partitioned backtest (daily-bar simulation of the entry/exit rule)
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    import os
    import json as _json
    from backtest.quick_backtest import run_backtest_with_regime_partition

    ROOT = os.environ.get('OPENCLAW_PARQUET_ROOT', '/root/openclaw/data/master')

    long_df = pd.read_parquet(f'{ROOT}/prices.parquet', columns=['ticker', 'date', 'close'])
    gld = long_df[long_df['ticker'] == TICKER].copy()
    gld['date'] = pd.to_datetime(gld['date'])
    close = gld.sort_values('date').set_index('date')['close'].sort_index()
    close = close.loc['2017-01-01':'2025-12-31']

    reg_df = pd.read_parquet(f'{ROOT}/historical_regimes.parquet')
    reg_df['date'] = pd.to_datetime(reg_df['date'])
    regime_col = 'state' if 'state' in reg_df.columns else 'regime'
    regime_map = dict(zip(reg_df['date'], reg_df[regime_col]))

    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / RSI_PERIOD, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / RSI_PERIOD, adjust=False).mean()
    rs = gain / loss.replace(0, float('inf'))
    rsi_vals = 100 - 100 / (1 + rs)

    rows = []
    in_position, entry_date, entry_price, hold_count = False, None, None, 0
    for dt in close.index:
        price, r = close.loc[dt], rsi_vals.loc[dt]
        if pd.isna(r):
            continue
        if in_position:
            hold_count += 1
            if r > RSI_EXIT or hold_count >= TIME_STOP_DAYS:
                raw_ret = (float(price) / float(entry_price)) - 1.0
                net_ret = raw_ret - ROUND_TRIP_COST
                rows.append({
                    'strategy_id': STRATEGY_ID, 'signal_date': entry_date,
                    'regime_state': regime_map.get(entry_date, 'LOW_VOL'),
                    'pnl': round(net_ret, 6), 'r_multiple': round(net_ret / 0.02, 4),
                })
                in_position = False
        elif float(r) < RSI_OVERSOLD:
            in_position, entry_date, entry_price, hold_count = True, dt, price, 0

    trades_df = pd.DataFrame(rows)
    print(f'[backtest] {len(trades_df)} trades', file=sys.stderr)
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(_json.dumps(result, indent=2, default=str))
