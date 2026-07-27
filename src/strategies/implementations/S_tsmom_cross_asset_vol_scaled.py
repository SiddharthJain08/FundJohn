"""
S_tsmom_cross_asset_vol_scaled — Time-Series Momentum, Cross-Asset, Volatility-Scaled
(Moskowitz, Ooi & Pedersen 2012; Elm Wealth 2010)

Assets whose own past 12-month return (skipping last month) is positive tend to
continue rising over the next 1–12 months; volatility-scaled long-short across
equity index, bond, commodity, and currency ETFs.
"""
from __future__ import annotations
import sys
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['TSMOMCrossAssetVolScaled']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID = 'S_tsmom_cross_asset_vol_scaled'

# Cross-asset ETP universe spanning 4 asset classes (21 instruments)
ETP_UNIVERSE = [
    # Equity index ETFs
    'SPY', 'QQQ', 'IWM', 'MDY', 'EFA', 'EEM', 'VWO',
    # Bond ETFs
    'TLT', 'IEF', 'SHY', 'LQD', 'HYG', 'TIP',
    # Commodity ETFs
    'GLD', 'SLV', 'USO', 'DBC',
    # Currency / FX ETFs
    'UUP', 'FXE', 'FXY', 'FXB',
]

_ASSET_CLASS_MAP: dict[str, str] = {
    **{t: 'equity_index' for t in ('SPY', 'QQQ', 'IWM', 'MDY', 'EFA', 'EEM', 'VWO')},
    **{t: 'bond'         for t in ('TLT', 'IEF', 'SHY', 'LQD', 'HYG', 'TIP')},
    **{t: 'commodity'    for t in ('GLD', 'SLV', 'USO', 'DBC')},
    **{t: 'currency'     for t in ('UUP', 'FXE', 'FXY', 'FXB')},
}

TARGET_VOL  = 0.10   # 10% annualised vol target per position
MAX_WEIGHT  = 0.15   # hard cap per position


class TSMOMCrossAssetVolScaled(BaseStrategy):
    """Vol-scaled long-short TSMOM across equity index, bond, commodity, and currency ETFs."""

    id          = STRATEGY_ID
    name        = 'TSMOMCrossAssetVolScaled'
    description = (
        'Assets with positive own past 12-month return (skipping last month) tend to '
        'continue rising; volatility-scaled long-short across equity index, bond, '
        'commodity, and currency ETFs (Moskowitz, Ooi & Pedersen 2012).'
    )
    tier        = 2
    min_lookback = 504
    # Cross-asset TSMOM has documented alpha across all regimes; best in extremes
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    LOOKBACK_LONG  = 252   # 12-month lookback
    LOOKBACK_SKIP  = 21    # skip last 1 month (reversal avoidance)
    VOL_WINDOW     = 63    # 3-month realized vol

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
        scale = self.position_scale(regime_state)

        if aux_data is None:
            aux_data = {}

        min_rows = self.LOOKBACK_LONG + self.LOOKBACK_SKIP
        if len(prices) < min_rows:
            print(f'[debug] signals=0 reason=insufficient_history({len(prices)}<{min_rows})', file=sys.stderr)
            return []

        # Use the fixed ETP universe intersected with available columns
        available = [t for t in ETP_UNIVERSE if t in prices.columns]
        if len(available) < 5:
            print(f'[debug] signals=0 reason=insufficient_etps({len(available)})', file=sys.stderr)
            return []

        # Realized vol from aux_data (preferred) — shape: [dates x tickers]
        rv_last = None
        rv_raw = aux_data.get('realized_vol')
        if rv_raw is not None and not (hasattr(rv_raw, 'empty') and rv_raw.empty):
            try:
                rv_last = rv_raw.iloc[-1]
            except Exception:
                rv_last = None

        signals: List[Signal] = []
        for ticker in available:
            series = prices[ticker].dropna()
            if len(series) < min_rows:
                continue
            current_price = float(series.iloc[-1])
            if current_price <= 0:
                continue

            # r_12_1: t-252 to t-21 (skip last month to avoid 1-month reversal)
            p_start = float(series.iloc[-(self.LOOKBACK_LONG + self.LOOKBACK_SKIP)])
            p_end   = float(series.iloc[-self.LOOKBACK_SKIP])
            if p_start <= 0:
                continue
            r_12_1 = (p_end / p_start) - 1.0

            direction = 'LONG' if r_12_1 > 0 else 'SHORT'

            # Realized vol: prefer aux_data, fall back to price-derived
            rv = None
            if rv_last is not None and ticker in rv_last.index:
                rv_val = float(rv_last[ticker])
                if rv_val > 0:
                    rv = rv_val
            if rv is None:
                pct_rets = series.pct_change().dropna()
                if len(pct_rets) >= self.VOL_WINDOW:
                    rv = float(pct_rets.iloc[-self.VOL_WINDOW:].std()) * (252 ** 0.5)
            if rv is None or rv <= 0:
                rv = 0.15  # 15% fallback

            weight = float(min(TARGET_VOL / rv * scale, MAX_WEIGHT))
            weight = max(weight, 0.01)

            abs_r = abs(r_12_1)
            if abs_r > 0.20:
                confidence = 'HIGH'
            elif abs_r > 0.10:
                confidence = 'MED'
            else:
                confidence = 'LOW'

            stops = self.compute_stops_and_targets(
                series, direction, current_price, regime_state=regime_state
            )

            signals.append(Signal(
                ticker=ticker,
                direction=direction,
                entry_price=float(current_price),
                stop_loss=float(stops['stop']),
                target_1=float(stops['t1']),
                target_2=float(stops['t2']),
                target_3=float(stops['t3']),
                position_size_pct=weight,
                confidence=confidence,
                signal_params={
                    'r_12_1':        round(r_12_1, 4),
                    'realized_vol':  round(rv, 4),
                    'asset_class':   _ASSET_CLASS_MAP.get(ticker, 'unknown'),
                },
            ))

        signals = signals[:self.MAX_SIGNALS]
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


# ── Standalone regime-partitioned backtest ────────────────────────────────────

def _run_backtest(prices_path: str, regimes_path: str,
                  start: str = '2017-01-01', end: str = '2025-12-31') -> pd.DataFrame:
    """Vectorised monthly-bar backtest returning trades_df."""
    import os
    empty = pd.DataFrame(columns=['strategy_id', 'signal_date', 'regime_state', 'pnl', 'r_multiple'])
    if not os.path.exists(prices_path):
        return empty

    # Load only the ETP columns that exist in the parquet
    all_cols = pd.read_parquet(prices_path, columns=[]).columns.tolist()
    load_cols = [t for t in ETP_UNIVERSE if t in all_cols]
    if not load_cols:
        return empty
    prices_df = pd.read_parquet(prices_path, columns=load_cols)
    prices_df = prices_df.loc[start:end]

    regimes: dict[str, str] = {}
    if os.path.exists(regimes_path):
        reg_df = pd.read_parquet(regimes_path)
        dc = 'date' if 'date' in reg_df.columns else reg_df.columns[0]
        sc = 'regime_state' if 'regime_state' in reg_df.columns else reg_df.columns[1]
        for _, row in reg_df.iterrows():
            regimes[str(row[dc])[:10]] = str(row[sc])

    strategy = TSMOMCrossAssetVolScaled()
    records = []

    # Use SPY as the reference series for monthly dates
    ref_col = next((c for c in ('SPY', 'EFA', 'TLT') if c in prices_df.columns), load_cols[0])
    ref = prices_df[ref_col].dropna()
    monthly_dates = ref.resample('MS').first().dropna().index

    min_rows = strategy.LOOKBACK_LONG + strategy.LOOKBACK_SKIP
    for sig_date in monthly_dates:
        date_str = str(sig_date.date())
        prices_win = prices_df.loc[:sig_date]
        if len(prices_win) < min_rows:
            continue
        regime_state = regimes.get(date_str, 'LOW_VOL')
        sigs = strategy.generate_signals(
            prices_win, {'state': regime_state}, load_cols, aux_data={}
        )
        if not sigs:
            continue
        # Equal-weight P&L proxy: average 21-day forward return across all signals
        pnl_list = []
        for sig in sigs:
            if sig.ticker not in prices_df.columns:
                continue
            fut = prices_df[sig.ticker].dropna().loc[sig_date:]
            if len(fut) < 22:
                continue
            entry = float(fut.iloc[0])
            exit_ = float(fut.iloc[min(21, len(fut) - 1)])
            if entry <= 0:
                continue
            ret = (exit_ - entry) / entry
            pnl_list.append(ret if sig.direction == 'LONG' else -ret)
        if not pnl_list:
            continue
        avg_pnl = float(sum(pnl_list) / len(pnl_list))
        records.append({
            'strategy_id':  STRATEGY_ID,
            'signal_date':  date_str,
            'regime_state': regime_state,
            'pnl':          avg_pnl,
            'r_multiple':   avg_pnl / 0.02 if avg_pnl != 0 else 0.0,
        })

    return pd.DataFrame(records)


if __name__ == '__main__':
    import json
    from backtest.quick_backtest import run_backtest_with_regime_partition

    PARQUET_ROOT = '/root/openclaw/data/master'
    trades_df = _run_backtest(
        prices_path=f'{PARQUET_ROOT}/prices.parquet',
        regimes_path=f'{PARQUET_ROOT}/historical_regimes.parquet',
    )
    print(f'Backtest produced {len(trades_df)} trades', file=sys.stderr)

    result = run_backtest_with_regime_partition(
        trades_df,
        strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(json.dumps(result, indent=2, default=str))
