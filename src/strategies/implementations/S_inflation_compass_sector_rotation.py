"""
S_inflation_compass_sector_rotation — "Taming the Wildcard: David Varadi's
Inflation Compass" (Allocate Smartly, 2026).
https://allocatesmartly.com/taming-the-wildcard-david-varadis-inflation-compass/

Monthly sector-ETF rotation driven by a dual growth/inflation compass:
  - Growth axis:    SPY close vs SMA(200)             → Growth Up / Down
  - Inflation axis: sector-implied inflation composite → Inflation Up / Down
        composite = rel_return(XLE,XLF,XLI,XLB) - rel_return(XLP,XLV,XLU)
        over a trailing ~1-month (21 trading day) window

NOTE on T5YIE: the source article's "Original" variant gates on the 5-year
breakeven inflation rate (T5YIE > 2.0%) plus its momentum. T5YIE is not
carried in data/master/macro.parquet (only VIX/VIX3M/VVIX/VIX9D series are
ingested), so this port uses the sector-implied composite alone as the
inflation signal — the same market-based substitute the article's own
pseudocode allows via its "OR sector_implied_inflation.pct_change up" branch.

Quadrant -> target ETF:
  (Growth Up,   Inflation Up  ) -> XLE  (Energy)
  (Growth Up,   Inflation Down) -> XLK  (Technology)
  (Growth Down, Inflation Down) -> IEF  (7-10y Treasuries)
  (Growth Down, Inflation Up  ) -> XLU  (Utilities)

Rebalances monthly, on the final trading day of the month, per source spec.
"""
from __future__ import annotations
import sys
import pandas as pd
from typing import List
from pathlib import Path
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['InflationCompassSectorRotation']

STRATEGY_ID = 'S_inflation_compass_sector_rotation'
INSTRUMENT_CLASS = 'etp'

# Positive-inflation-beta sectors (cyclical)
INFLATION_POS = ['XLE', 'XLF', 'XLI', 'XLB']
# Negative-inflation-beta sectors (defensive)
INFLATION_NEG = ['XLP', 'XLV', 'XLU']

# (growth_signal, inflation_signal) -> target ETF
QUADRANT_MAP = {
    ( 1,  1): 'XLE',  # Growth Up   / Inflation Up    -> Energy
    ( 1, -1): 'XLK',  # Growth Up   / Inflation Down  -> Technology
    (-1, -1): 'IEF',  # Growth Down / Inflation Down  -> Treasuries
    (-1,  1): 'XLU',  # Growth Down / Inflation Up    -> Utilities
}

SMA_WINDOW    = 200
INFL_LOOKBACK = 21   # ~1 trading month


class InflationCompassSectorRotation(BaseStrategy):
    """Monthly growth x inflation macro-quadrant sector rotation (Varadi 2026)."""

    id                = STRATEGY_ID
    name              = 'InflationCompassSectorRotation'
    description       = (
        'Monthly sector-ETF rotation on growth (SPY vs SMA200) x inflation '
        '(sector-implied cyclical/defensive spread) macro quadrant.'
    )
    tier              = 2
    min_lookback      = 504
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    BASE_SIZE = 0.25   # concentrated single-ETF position
    MAX_SIGNALS = 1

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

        if len(prices) < self.min_lookback:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Monthly rebalance gate: only fire on the final trading day of the month.
        today = prices.index[-1]
        next_bday = today + pd.tseries.offsets.BDay(1)
        is_month_end = (next_bday.month != today.month)
        if not is_month_end:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # ── Growth signal: SPY close vs SMA(200) ──────────────────────────────
        if 'SPY' not in prices.columns:
            print('[debug] signals=0', file=sys.stderr)
            return []

        spy = prices['SPY'].dropna()
        if len(spy) < SMA_WINDOW:
            print('[debug] signals=0', file=sys.stderr)
            return []

        spy_close = float(spy.iloc[-1])
        spy_sma   = float(spy.iloc[-SMA_WINDOW:].mean())
        if spy_close <= 0 or spy_sma <= 0:
            print('[debug] signals=0', file=sys.stderr)
            return []
        growth_signal = 1 if spy_close > spy_sma else -1

        # ── Inflation signal: sector-implied composite spread ─────────────────
        pos_rets, neg_rets = [], []
        for t in INFLATION_POS:
            if t not in prices.columns:
                continue
            s = prices[t].dropna()
            if len(s) < INFL_LOOKBACK + 1:
                continue
            p0, p1 = float(s.iloc[-INFL_LOOKBACK - 1]), float(s.iloc[-1])
            if p0 > 0:
                pos_rets.append(p1 / p0 - 1.0)
        for t in INFLATION_NEG:
            if t not in prices.columns:
                continue
            s = prices[t].dropna()
            if len(s) < INFL_LOOKBACK + 1:
                continue
            p0, p1 = float(s.iloc[-INFL_LOOKBACK - 1]), float(s.iloc[-1])
            if p0 > 0:
                neg_rets.append(p1 / p0 - 1.0)

        if not pos_rets or not neg_rets:
            print('[debug] signals=0', file=sys.stderr)
            return []

        pos_mean = sum(pos_rets) / len(pos_rets)
        neg_mean = sum(neg_rets) / len(neg_rets)
        inflation_spread = pos_mean - neg_mean
        inflation_signal = 1 if inflation_spread > 0 else -1

        # ── Target ETF selection ──────────────────────────────────────────────
        quadrant = (growth_signal, inflation_signal)
        target   = QUADRANT_MAP.get(quadrant)
        if target is None or target not in prices.columns:
            print('[debug] signals=0', file=sys.stderr)
            return []

        etf_series = prices[target].dropna()
        if len(etf_series) < 14:
            print('[debug] signals=0', file=sys.stderr)
            return []

        current_price = float(etf_series.iloc[-1])
        if current_price <= 0:
            print('[debug] signals=0', file=sys.stderr)
            return []

        stops = self.compute_stops_and_targets(
            etf_series, 'LONG', current_price, regime_state=regime_state
        )

        scale         = self.position_scale(regime_state)
        position_size = float(min(self.BASE_SIZE * scale, 0.30))

        # ── Confidence: how decisively each axis fires ────────────────────────
        spy_dist_pct = abs(spy_close - spy_sma) / spy_sma
        infl_div_pct = abs(inflation_spread) / max(abs(neg_mean), 1e-4)
        if spy_dist_pct >= 0.05 and infl_div_pct >= 0.50:
            confidence = 'HIGH'
        elif spy_dist_pct >= 0.02 or infl_div_pct >= 0.20:
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
                'growth_signal':      growth_signal,
                'inflation_signal':   inflation_signal,
                'quadrant':           list(quadrant),
                'target_etf':         target,
                'spy_close':          round(spy_close, 4),
                'spy_sma_200':        round(spy_sma, 4),
                'inflation_spread':   round(inflation_spread, 4),
                'trigger':            'month_end_rebalance',
            },
        )]

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


# ─── Standalone backtest ──────────────────────────────────────────────────────

def _run_backtest() -> pd.DataFrame:
    """
    Simulate monthly growth x inflation sector rotation over 2017-2025 using
    data/master/prices.parquet. One trade record per calendar month (ETF held
    that month) tagged with the regime in effect at entry.
    """
    root = Path(__file__).resolve().parents[3]
    parquet_path = root / 'data' / 'master' / 'prices.parquet'

    needed = ['SPY'] + INFLATION_POS + INFLATION_NEG + list(set(QUADRANT_MAP.values()))

    try:
        df = pd.read_parquet(parquet_path, columns=['date', 'ticker', 'close'])
    except Exception as e:
        print(f'[backtest] Could not load prices: {e}', file=sys.stderr)
        return pd.DataFrame()

    df['date'] = pd.to_datetime(df['date'])
    df = df[df['ticker'].isin(needed)].sort_values('date')
    wide = df.pivot(index='date', columns='ticker', values='close').sort_index()

    reg_path = root / 'data' / 'master' / 'historical_regimes.parquet'
    if reg_path.exists():
        reg = pd.read_parquet(reg_path, columns=['date', 'regime'])
        reg['date'] = pd.to_datetime(reg['date'])
        reg = reg.set_index('date')['regime']
    else:
        reg = pd.Series(dtype=str)

    trades = []
    dates  = wide.index.tolist()

    prev_target, prev_target_px, prev_date = None, None, None

    for i in range(SMA_WINDOW, len(dates)):
        d = dates[i]

        next_bday = d + pd.tseries.offsets.BDay(1)
        if next_bday.month == d.month:
            continue  # only evaluate on the final trading day of the month

        if 'SPY' not in wide.columns:
            continue
        spy_slice = wide['SPY'].iloc[i - SMA_WINDOW:i + 1].dropna()
        if len(spy_slice) < SMA_WINDOW:
            continue
        spy_close = float(spy_slice.iloc[-1])
        spy_sma   = float(spy_slice.mean())
        if spy_close <= 0 or spy_sma <= 0:
            continue
        growth_signal = 1 if spy_close > spy_sma else -1

        pos_rets, neg_rets = [], []
        if i - INFL_LOOKBACK >= 0:
            for t in INFLATION_POS:
                if t in wide.columns and pd.notna(wide[t].iloc[i]) and pd.notna(wide[t].iloc[i - INFL_LOOKBACK]):
                    p0, p1 = float(wide[t].iloc[i - INFL_LOOKBACK]), float(wide[t].iloc[i])
                    if p0 > 0:
                        pos_rets.append(p1 / p0 - 1.0)
            for t in INFLATION_NEG:
                if t in wide.columns and pd.notna(wide[t].iloc[i]) and pd.notna(wide[t].iloc[i - INFL_LOOKBACK]):
                    p0, p1 = float(wide[t].iloc[i - INFL_LOOKBACK]), float(wide[t].iloc[i])
                    if p0 > 0:
                        neg_rets.append(p1 / p0 - 1.0)
        if not pos_rets or not neg_rets:
            continue

        inflation_signal = 1 if (sum(pos_rets) / len(pos_rets)) > (sum(neg_rets) / len(neg_rets)) else -1
        quadrant = (growth_signal, inflation_signal)
        target   = QUADRANT_MAP.get(quadrant)
        if target is None or target not in wide.columns:
            continue

        target_px = float(wide[target].iloc[i]) if pd.notna(wide[target].iloc[i]) else None
        if target_px is None or target_px <= 0:
            continue

        if prev_target is not None and prev_target_px is not None:
            exit_px = float(wide[prev_target].iloc[i]) if prev_target in wide.columns and pd.notna(wide[prev_target].iloc[i]) else prev_target_px
            pnl = (exit_px - prev_target_px) / prev_target_px
            risk_unit = 0.20
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

        prev_target, prev_target_px, prev_date = target, target_px, d

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
