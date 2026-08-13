"""
Weekend Effect Monday Short — Keim & Stambaugh (1984)
Source: https://doi.org/10.1111/j.1540-6261.1984.tb03675.x

Monday equity returns are systematically negative across all firm sizes and
market structures — a persistent calendar anomaly that survives across
settlement regimes, specialist structures, and OTC bid-price controls.

Signal: SHORT the cross-section of R3000 names on Monday open; exit Monday close.
Equal-weight across universe; size ∝ Friday-to-Monday correlation rank.
"""
from __future__ import annotations
import os
import sys
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE
from src.strategies.universe_default import r3000 as universe_filter

__all__ = ['WeekendEffectMondayShort']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_weekend_effect_monday_short'

# Maximum number of tickers to short simultaneously
MAX_POSITIONS    = 50
# Minimum lookback for Friday return computation (trading days)
MIN_LOOKBACK     = 5


class WeekendEffectMondayShort(BaseStrategy):
    """Short R3000 equities on Monday open; exit Monday close.

    Implements the Keim & Stambaugh (1984) weekend-effect anomaly: Monday
    returns are systematically negative across all size tiers. We rank the
    universe by prior-Friday-to-Monday expected spread (trailing 12-week
    Friday→Monday return), short the top-ranked names on each Monday.
    """

    id                = STRATEGY_ID
    name              = 'Weekend Effect Monday Short'
    description       = (
        'Short R3000 equities on Monday open based on the Keim-Stambaugh '
        'weekend-effect anomaly (1984); exit Monday close.'
    )
    tier              = 2
    signal_frequency  = 'daily'
    calendar_edge     = True   # window IS the signal; ports across regime flips (2026-08-13)
    min_lookback      = 252
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    def default_parameters(self) -> dict:
        return {'max_positions': MAX_POSITIONS, 'lookback_weeks': 12}

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

        today = prices.index[-1]
        # Only fire on Mondays (weekday 0)
        if today.weekday() != 0:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        scale        = self.position_scale(regime_state)
        max_pos      = int(self.parameters.get('max_positions', MAX_POSITIONS))
        lookback_wks = int(self.parameters.get('lookback_weeks', 12))
        lookback_td  = lookback_wks * 5  # approx trading days

        # Filter universe to tickers present in prices
        tickers = [t for t in universe if t in prices.columns]
        if not tickers:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Need at least MIN_LOOKBACK rows
        if len(prices) < MIN_LOOKBACK:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Compute trailing Friday→Monday return for each ticker
        # Strategy: rank tickers by how negative their Monday returns have been
        window = prices[tickers].iloc[-min(lookback_td, len(prices)):]

        monday_returns: dict = {}
        idx = window.index
        for i, dt in enumerate(idx):
            if dt.weekday() != 0 or i == 0:
                continue
            prev_dt = idx[i - 1]
            # prev should be Friday (weekday 4) — skip if gap is too large
            if (dt - prev_dt).days > 4:
                continue
            row_curr = window.loc[dt]
            row_prev = window.loc[prev_dt]
            for t in tickers:
                p_curr = row_curr.get(t)
                p_prev = row_prev.get(t)
                if pd.isna(p_curr) or pd.isna(p_prev) or float(p_prev) == 0:
                    continue
                ret = (float(p_curr) - float(p_prev)) / float(p_prev)
                monday_returns.setdefault(t, []).append(ret)

        if not monday_returns:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Rank by mean Monday return (ascending = most negative first)
        mean_mon_ret = {t: sum(v) / len(v) for t, v in monday_returns.items() if len(v) >= 3}
        if not mean_mon_ret:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        ranked = sorted(mean_mon_ret.items(), key=lambda x: x[1])  # ascending: worst first
        candidates = ranked[:max_pos]

        current_prices = prices[tickers].iloc[-1]
        signals: List[Signal] = []
        pos_size = round(1.0 / max(len(candidates), 1) * scale, 4)

        for ticker, avg_mon_ret in candidates:
            cp = current_prices.get(ticker)
            if cp is None or pd.isna(cp):
                continue
            current_price = float(cp)
            if current_price <= 0:
                continue

            st = self.compute_stops_and_targets(
                prices[ticker], 'SHORT', current_price, regime_state=regime_state
            )

            # Confidence based on consistency of negative Monday returns
            mon_rets = monday_returns.get(ticker, [])
            neg_frac = sum(1 for r in mon_rets if r < 0) / len(mon_rets) if mon_rets else 0
            if neg_frac >= 0.65:
                conf = 'HIGH'
            elif neg_frac >= 0.55:
                conf = 'MED'
            else:
                conf = 'LOW'

            signals.append(Signal(
                ticker=ticker,
                direction='SHORT',
                entry_price=current_price,
                stop_loss=st['stop'],
                target_1=st['t1'],
                target_2=st['t2'],
                target_3=st['t3'],
                position_size_pct=pos_size,
                confidence=conf,
                signal_params={
                    'trigger': 'monday_short',
                    'avg_monday_return': round(avg_mon_ret, 6),
                    'neg_monday_frac': round(neg_frac, 4),
                    'lookback_weeks': lookback_wks,
                    'regime': regime_state,
                    'scale': scale,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


# ---------------------------------------------------------------------------
# Regime-partitioned backtest
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    from backtest.quick_backtest import run_backtest_with_regime_partition
    import json as _json

    PARQUET_ROOT = os.environ.get('OPENCLAW_PARQUET_ROOT', '/root/openclaw/data/master')

    # Load SPY as proxy for a single-ticker version to check feasibility
    PROXY = 'SPY'
    prices_df = pd.read_parquet(f'{PARQUET_ROOT}/prices.parquet', columns=[PROXY])
    reg_df    = pd.read_parquet(f'{PARQUET_ROOT}/historical_regimes.parquet')
    prices_df.index = pd.to_datetime(prices_df.index)
    reg_df.index    = pd.to_datetime(reg_df.index)

    prices_df = prices_df.dropna(subset=[PROXY])
    all_tds   = sorted(prices_df.index.tolist())

    rows = []
    for i, dt in enumerate(all_tds):
        if dt.weekday() != 0 or i == 0:
            continue
        prev_dt = all_tds[i - 1]
        if (dt - prev_dt).days > 4:
            continue
        p_curr = prices_df[PROXY].get(dt)
        p_prev = prices_df[PROXY].get(prev_dt)
        if p_curr is None or p_prev is None or pd.isna(p_curr) or pd.isna(p_prev):
            continue
        if float(p_prev) == 0:
            continue
        pnl = -(float(p_curr) - float(p_prev)) / float(p_prev)  # SHORT
        regime_row = reg_df[reg_df.index <= dt].iloc[-1] if not reg_df.empty else None
        rstate = str(regime_row['state']) if (regime_row is not None and 'state' in reg_df.columns) else 'LOW_VOL'
        rows.append({
            'strategy_id': STRATEGY_ID,
            'signal_date': dt,
            'regime_state': rstate,
            'pnl': pnl,
            'r_multiple': round(pnl / 0.02, 4),
        })

    trades_df = pd.DataFrame(rows)
    print(f'[backtest] {len(trades_df)} trades', file=sys.stderr)
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(_json.dumps(result, indent=2, default=str))
