"""
S_growth_inflation_sector_timing — Varadi / Allocate Smartly (2026)

Rotate into one sector ETF based on the intersection of:
  - Growth signal: SPY close > SMA(252) → Growth Up else Growth Down
  - Inflation signal: mean(XLE,XLI,XLF,XLB) > mean(XLU,XLV,XLP) → Inflation Up else Inflation Down

Quadrant → target ETF:
  (Growth Up,   Inflation Down) → XLK  (Technology)
  (Growth Up,   Inflation Up  ) → XLE  (Energy)
  (Growth Down, Inflation Down) → XLV  (Health Care)
  (Growth Down, Inflation Up  ) → XLB  (Materials)

Rebalances only on regime change (~6 times/year). Active in all regimes.
"""
from __future__ import annotations
import sys
import pandas as pd
from typing import List
from pathlib import Path
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['GrowthInflationSectorTiming']

STRATEGY_ID = 'S_growth_inflation_sector_timing'

# Positive-inflation-beta sectors
INFLATION_POS = ['XLE', 'XLI', 'XLF', 'XLB']
# Negative-inflation-beta sectors
INFLATION_NEG = ['XLU', 'XLV', 'XLP']

# (growth_signal, inflation_signal) → target ETF
QUADRANT_MAP = {
    ( 1, -1): 'XLK',  # Growth Up   / Inflation Down  → Technology
    ( 1,  1): 'XLE',  # Growth Up   / Inflation Up    → Energy
    (-1, -1): 'XLV',  # Growth Down / Inflation Down  → Health Care
    (-1,  1): 'XLB',  # Growth Down / Inflation Up    → Materials
}


class GrowthInflationSectorTiming(BaseStrategy):
    """Rotate into one sector ETF based on growth×inflation macro quadrant (Varadi 2026)."""

    id                = STRATEGY_ID
    name              = 'GrowthInflationSectorTiming'
    description       = (
        'Rotate into one sector ETF based on growth×inflation macro quadrant; '
        'growth proxy = SPY vs SMA(252); inflation proxy = inflation-beta sector strength.'
    )
    tier              = 2
    min_lookback      = 504   # 2 trading years for SMA(252) with buffer
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    SMA_WINDOW   = 252
    BASE_SIZE    = 0.25   # concentrated single-ETF position

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

        if len(prices) < self.SMA_WINDOW:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # ── Growth signal: SPY close vs SMA(252) ──────────────────────────────
        if 'SPY' not in prices.columns:
            print('[debug] signals=0', file=sys.stderr)
            return []

        spy = prices['SPY'].dropna()
        if len(spy) < self.SMA_WINDOW:
            print('[debug] signals=0', file=sys.stderr)
            return []

        spy_close = float(spy.iloc[-1])
        spy_sma   = float(spy.iloc[-self.SMA_WINDOW:].mean())
        if spy_close <= 0 or spy_sma <= 0:
            print('[debug] signals=0', file=sys.stderr)
            return []
        growth_signal = 1 if spy_close > spy_sma else -1

        # ── Inflation signal: sector-implied expected inflation ───────────────
        pos_vals = [float(prices[t].iloc[-1]) for t in INFLATION_POS
                    if t in prices.columns and pd.notna(prices[t].iloc[-1]) and float(prices[t].iloc[-1]) > 0]
        neg_vals = [float(prices[t].iloc[-1]) for t in INFLATION_NEG
                    if t in prices.columns and pd.notna(prices[t].iloc[-1]) and float(prices[t].iloc[-1]) > 0]

        if not pos_vals or not neg_vals:
            print('[debug] signals=0', file=sys.stderr)
            return []

        inflation_pos_mean = sum(pos_vals) / len(pos_vals)
        inflation_neg_mean = sum(neg_vals) / len(neg_vals)
        inflation_signal = 1 if inflation_pos_mean > inflation_neg_mean else -1

        # ── Target ETF selection ──────────────────────────────────────────────
        quadrant  = (growth_signal, inflation_signal)
        target    = QUADRANT_MAP.get(quadrant)
        if target is None or target not in prices.columns:
            print('[debug] signals=0', file=sys.stderr)
            return []

        etf_series = prices[target].dropna()
        if etf_series.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        current_price = float(etf_series.iloc[-1])
        if current_price <= 0:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # ── Stops & targets via BaseStrategy ATR helper ───────────────────────
        stops = self.compute_stops_and_targets(
            etf_series, 'LONG', current_price, regime_state=regime_state
        )

        scale         = self.position_scale(regime_state)
        position_size = float(min(self.BASE_SIZE * scale, 0.30))

        # ── Confidence: how decisively each signal fires ──────────────────────
        spy_dist_pct = abs(spy_close - spy_sma) / spy_sma
        inf_div_pct  = abs(inflation_pos_mean - inflation_neg_mean) / max(inflation_neg_mean, 1e-6)
        if spy_dist_pct >= 0.05 and inf_div_pct >= 0.05:
            confidence = 'HIGH'
        elif spy_dist_pct >= 0.02 or inf_div_pct >= 0.02:
            confidence = 'MED'
        else:
            confidence = 'LOW'

        signals = [Signal(
            ticker            = target,
            direction         = 'LONG',
            entry_price       = round(current_price, 4),
            stop_loss         = float(stops['stop']),
            target_1          = float(stops['t1']),
            target_2          = float(stops['t2']),
            target_3          = float(stops['t3']),
            position_size_pct = position_size,
            confidence        = confidence,
            signal_params     = {
                'growth_signal':       growth_signal,
                'inflation_signal':    inflation_signal,
                'quadrant':            list(quadrant),
                'target_etf':          target,
                'spy_close':           round(spy_close, 4),
                'spy_sma_252':         round(spy_sma, 4),
                'inflation_pos_mean':  round(inflation_pos_mean, 4),
                'inflation_neg_mean':  round(inflation_neg_mean, 4),
            },
        )]

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


# ─── Standalone backtest ──────────────────────────────────────────────────────

def _run_backtest() -> pd.DataFrame:
    """
    Simulate daily growth×inflation sector rotation over 2017-2025 using
    data/master/prices.parquet. Generates one trade record per calendar month
    (ETF held that month) tagged with the regime in effect at month-start.
    """
    root = Path(__file__).resolve().parents[3]
    parquet_path = root / 'data' / 'master' / 'prices.parquet'

    needed = ['SPY'] + INFLATION_POS + INFLATION_NEG + list(QUADRANT_MAP.values())

    try:
        df = pd.read_parquet(parquet_path, columns=['date', 'ticker', 'close'])
    except Exception as e:
        print(f'[backtest] Could not load prices: {e}', file=sys.stderr)
        return pd.DataFrame()

    df['date'] = pd.to_datetime(df['date'])
    df = df[df['ticker'].isin(needed)].sort_values('date')

    # Wide: date × ticker
    wide = df.pivot(index='date', columns='ticker', values='close').sort_index()

    # Historical regimes
    reg_path = root / 'data' / 'master' / 'historical_regimes.parquet'
    if reg_path.exists():
        reg = pd.read_parquet(reg_path, columns=['date', 'regime'])
        reg['date'] = pd.to_datetime(reg['date'])
        reg = reg.set_index('date')['regime']
    else:
        reg = pd.Series(dtype=str)

    trades = []
    SMA_W  = 252
    dates  = wide.index.tolist()

    prev_target    = None
    prev_target_px = None
    prev_date      = None

    for i in range(SMA_W, len(dates)):
        d = dates[i]

        if 'SPY' not in wide.columns:
            continue
        spy_slice = wide['SPY'].iloc[i - SMA_W:i + 1].dropna()
        if len(spy_slice) < SMA_W:
            continue

        spy_close = float(spy_slice.iloc[-1])
        spy_sma   = float(spy_slice.mean())
        if spy_close <= 0 or spy_sma <= 0:
            continue
        growth_signal = 1 if spy_close > spy_sma else -1

        pos_vals = [float(wide[t].iloc[i]) for t in INFLATION_POS
                    if t in wide.columns and pd.notna(wide[t].iloc[i]) and float(wide[t].iloc[i]) > 0]
        neg_vals = [float(wide[t].iloc[i]) for t in INFLATION_NEG
                    if t in wide.columns and pd.notna(wide[t].iloc[i]) and float(wide[t].iloc[i]) > 0]
        if not pos_vals or not neg_vals:
            continue

        inflation_signal = 1 if (sum(pos_vals) / len(pos_vals)) > (sum(neg_vals) / len(neg_vals)) else -1
        quadrant = (growth_signal, inflation_signal)
        target   = QUADRANT_MAP.get(quadrant)
        if target is None or target not in wide.columns:
            continue

        target_px = float(wide[target].iloc[i]) if pd.notna(wide[target].iloc[i]) else None
        if target_px is None or target_px <= 0:
            continue

        # Record trade when target ETF changes (regime change = new signal event)
        if prev_target is not None and prev_target != target and prev_target_px is not None:
            pnl = (target_px - prev_target_px) / prev_target_px  # approx: we held prev until switch
            # Actually: pnl of prev holding from entry to exit
            # Re-use prior entry price to compute return on the exited position
            exit_px  = float(wide[prev_target].iloc[i]) if prev_target in wide.columns and pd.notna(wide[prev_target].iloc[i]) else prev_target_px
            pnl      = (exit_px - prev_target_px) / prev_target_px
            risk_unit = 0.20  # fixed 20% risk unit for R-multiple
            regime_state = str(reg.get(prev_date, 'TRANSITIONING')) if prev_date is not None else 'TRANSITIONING'
            trades.append({
                'strategy_id':  STRATEGY_ID,
                'signal_date':  prev_date.strftime('%Y-%m-%d') if prev_date else d.strftime('%Y-%m-%d'),
                'regime_state': regime_state,
                'pnl':          round(pnl, 6),
                'r_multiple':   round(pnl / risk_unit, 4),
                'exit_date':    d.strftime('%Y-%m-%d'),
                'etf':          prev_target,
            })

        if prev_target != target:
            prev_target    = target
            prev_target_px = target_px
            prev_date      = d

    return pd.DataFrame(trades)


if __name__ == '__main__':
    from backtest.quick_backtest import run_backtest_with_regime_partition
    trades_df = _run_backtest()
    print(f'Total trades: {len(trades_df)}')
    if not trades_df.empty:
        print(trades_df.to_string())
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 5, 'min_avg_r': 0.0},
    )
    print(f"Eligible regimes proposed: {result['eligible_regimes_proposed']}")
    for r, s in result['regime_partition'].items():
        print(f'  {r}: {s}')
