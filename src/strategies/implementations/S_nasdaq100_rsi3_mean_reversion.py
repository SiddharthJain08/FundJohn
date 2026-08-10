# Source: https://backtest.substack.com/p/buying-weakness-on-the-nasdaq-100
# Johnson 2026 — RSI(3)<10 oversold bounce, top-half ATRP(63) filter, 1.75% exit.
# OOS 2007-2015: CAGR 11.97%, win_rate 64.15%, PF 1.69, maxDD 8.54%.
from __future__ import annotations

import sys
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_nasdaq100_rsi3_mean_reversion'

RSI_PERIOD      = 3
RSI_THRESHOLD   = 10.0    # oversold trigger
ATRP_PERIOD     = 63      # ATRP(63) volatility filter window
PROFIT_TARGET   = 0.0175  # 1.75% exit (prior_close * 1.0175)
ENTRY_DISC      = 0.99    # limit entry 1% below the day's CLOSE (close-only panel proxy for the paper's low)
BASE_POSITION   = 0.08    # 8% per position
MAX_POSITIONS   = 12


def _rsi(series: pd.Series, n: int) -> pd.Series:
    d = series.diff()
    ag = d.clip(lower=0).ewm(alpha=1.0 / n, adjust=False).mean()
    al = (-d).clip(lower=0).ewm(alpha=1.0 / n, adjust=False).mean()
    rs = ag / al.replace(0.0, float('nan'))
    return 100.0 - (100.0 / (1.0 + rs))


class Nasdaq100Rsi3MeanReversion(BaseStrategy):
    """RSI(3)<10 oversold bounce on high-volatility large-cap equities — 1.75% profit target."""

    id                = STRATEGY_ID
    name              = 'Nasdaq100 RSI(3) Mean Reversion'
    description       = ('Short-term mean reversion: RSI(3)<10 oversold bounce '
                         'on top-half ATRP(63) names, 1.75% profit target.')
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = 100
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = MAX_POSITIONS

    def default_parameters(self) -> dict:
        return {
            'rsi_period':    RSI_PERIOD,
            'rsi_threshold': RSI_THRESHOLD,
            'atrp_period':   ATRP_PERIOD,
            'profit_target': PROFIT_TARGET,
            'entry_disc':    ENTRY_DISC,
            'base_position': BASE_POSITION,
            'max_positions': MAX_POSITIONS,
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

        rsi_p    = int(self.parameters.get('rsi_period', RSI_PERIOD))
        rsi_thr  = float(self.parameters.get('rsi_threshold', RSI_THRESHOLD))
        atrp_p   = int(self.parameters.get('atrp_period', ATRP_PERIOD))
        pt       = float(self.parameters.get('profit_target', PROFIT_TARGET))
        disc     = float(self.parameters.get('entry_disc', ENTRY_DISC))
        base_pos = float(self.parameters.get('base_position', BASE_POSITION))
        max_pos  = int(self.parameters.get('max_positions', MAX_POSITIONS))

        min_bars = atrp_p + rsi_p + 5
        if len(prices) < min_bars:
            print('[debug] signals=0', file=sys.stderr)
            return []

        scale   = self.position_scale(regime_state)
        tickers = [t for t in universe if t in prices.columns]
        if not tickers:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Rank by ATRP(63); keep top half (high-vol names only)
        atrp_map: dict[str, float] = {}
        for t in tickers:
            s = prices[t].dropna()
            if len(s) < atrp_p + 2:
                continue
            atr_val = s.diff().abs().rolling(atrp_p).mean().iloc[-1]
            if pd.notna(atr_val) and float(atr_val) > 0 and float(s.iloc[-1]) > 0:
                atrp_map[t] = float(atr_val) / float(s.iloc[-1])

        if not atrp_map:
            print('[debug] signals=0', file=sys.stderr)
            return []

        ranked     = sorted(atrp_map, key=atrp_map.get, reverse=True)
        high_vol   = set(ranked[:max(1, len(ranked) // 2)])

        signals: List[Signal] = []

        for ticker in ranked:
            if ticker not in high_vol:
                break
            if len(signals) >= max_pos:
                break

            s = prices[ticker].dropna()
            if len(s) < min_bars:
                continue

            rsi_val = float(_rsi(s, rsi_p).iloc[-1])
            if pd.isna(rsi_val) or rsi_val >= rsi_thr:
                continue

            close  = float(s.iloc[-1])
            entry  = round(close * disc, 4)       # limit ~1% below today's close
            tgt1   = round(close * (1.0 + pt), 4)  # 1.75% profit target

            stops  = self.compute_stops_and_targets(
                s, 'LONG', entry, regime_state=regime_state
            )

            if rsi_val < 5.0:
                conf = 'HIGH'
            elif rsi_val < 7.5:
                conf = 'MED'
            else:
                conf = 'LOW'

            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = entry,
                stop_loss         = float(stops['stop']),
                target_1          = tgt1,
                target_2          = float(stops['t2']),
                target_3          = float(stops['t3']),
                position_size_pct = float(round(base_pos * scale, 4)),
                confidence        = conf,
                signal_params     = {
                    'rsi_3':    round(rsi_val, 2),
                    'atrp_63':  round(atrp_map[ticker], 6),
                    'regime':   regime_state,
                    'scale':    scale,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


if __name__ == '__main__':
    import os
    from backtest.quick_backtest import run_backtest_with_regime_partition
    ROOT      = os.environ.get('OPENCLAW_PARQUET_ROOT', '/root/openclaw/data/master')
    prices_df = pd.read_parquet(f'{ROOT}/prices.parquet')
    reg_df    = pd.read_parquet(f'{ROOT}/historical_regimes.parquet')
    reg_df['date'] = pd.to_datetime(reg_df['date'])
    reg_map   = reg_df.set_index('date')['regime_state'].to_dict()
    strategy  = Nasdaq100Rsi3MeanReversion()
    universe  = list(prices_df.columns)
    rows = []
    for i in range(100, len(prices_df)):
        bar_date = prices_df.index[i]
        r_state  = reg_map.get(pd.Timestamp(bar_date), 'LOW_VOL')
        sigs     = strategy.generate_signals(prices_df.iloc[:i], {'state': r_state}, universe)
        for sig in sigs:
            future = prices_df[sig.ticker].dropna()
            # Signal comes from iloc[:i] (last bar i-1), so bar i is the first fillable bar — include it
            future = future[future.index >= bar_date].head(5)
            if future.empty:
                continue
            hit   = any(float(p) >= sig.target_1 for p in future)
            pnl   = (sig.target_1 - sig.entry_price) if hit else (sig.stop_loss - sig.entry_price)
            denom = max(abs(sig.entry_price - sig.stop_loss), 1e-6)
            rows.append({'strategy_id': STRATEGY_ID, 'signal_date': str(bar_date)[:10],
                         'regime_state': r_state, 'pnl': float(pnl), 'r_multiple': float(pnl / denom)})
    trades_df = pd.DataFrame(rows)
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print('regime_partition:', result['regime_partition'])
    print('eligible_regimes_proposed:', result['eligible_regimes_proposed'])
