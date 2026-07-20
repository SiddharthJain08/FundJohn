"""S_valuation_bounded_tsmom — Valuation-Bounded Time-Series Momentum

Hypothesis (LeCompte, Suominen, Hjalmarsson 2026):
  Equity TSMOM earns its premium only when CAPE, dividend yield, and term spread
  are in a normal (non-extreme) range.  When the composite "Boundaries" metric
  exceeds a threshold the momentum signal inverts, so the strategy goes FLAT near
  valuation extremes.

Instrument class: etp  (trades SPY)
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

INSTRUMENT_CLASS = "etp"
STRATEGY_ID = "S_valuation_bounded_tsmom"

__all__ = ['S_valuation_bounded_tsmom']


class S_valuation_bounded_tsmom(BaseStrategy):
    """Equity TSMOM gated by Boundaries metric (CAPE + div-yield + term-spread extremes)."""

    id          = STRATEGY_ID
    name        = 'S_valuation_bounded_tsmom'
    description = (
        'Equity market time-series momentum earns its premium only when CAPE, dividend yield, '
        'and term spread are in a normal (non-extreme) range; near valuation extremes the trend '
        'reverses, so gating TSMOM on a composite Boundaries metric improves risk-adjusted returns.'
    )
    tier        = 2
    min_lookback = 1260  # 5 years minimum; 10-yr rolling window needs ~2520

    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

    # Boundary threshold: sum of two squared normalised values; max=2.0
    # Values below this indicate normal (non-extreme) valuations → follow TSMOM
    BOUNDARY_THRESHOLD = 0.5

    def generate_signals(
        self,
        prices: pd.DataFrame,
        regime: dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        scale = self.position_scale(regime_state)

        try:
            # ── Locate SPY price series ──────────────────────────────────────
            if 'SPY' in prices.columns:
                spy_prices = prices['SPY'].dropna()
            elif 'close' in prices.columns:
                spy_prices = prices['close'].dropna()
            else:
                print(f'[debug] signals=0 reason=no_spy_column', file=sys.stderr)
                return []

            if len(spy_prices) < 252:
                print(f'[debug] signals=0 reason=insufficient_history', file=sys.stderr)
                return []

            current_price = float(spy_prices.iloc[-1])

            # ── 12-month excess return TSMOM ─────────────────────────────────
            ret_12m = float(spy_prices.iloc[-1] / spy_prices.iloc[-252] - 1)
            rf = self._get_risk_free(aux_data)
            excess_ret_12m = ret_12m - rf
            tsmom_long = excess_ret_12m > 0   # True → LONG, False → would SHORT

            # ETP class: do not SHORT — go FLAT when momentum is negative
            if not tsmom_long:
                print(f'[debug] signals=0 reason=tsmom_negative', file=sys.stderr)
                return []

            # ── Boundaries gate ──────────────────────────────────────────────
            within_bounds = self._boundaries_ok(aux_data)
            if not within_bounds:
                print(f'[debug] signals=0 reason=valuation_extreme', file=sys.stderr)
                return []

            # ── Build signal ─────────────────────────────────────────────────
            stops = self.compute_stops_and_targets(
                spy_prices, 'LONG', current_price, regime_state=regime_state
            )
            signals = [Signal(
                ticker='SPY',
                direction='LONG',
                entry_price=current_price,
                stop_loss=float(stops['stop']),
                target_1=float(stops['t1']),
                target_2=float(stops['t2']),
                target_3=float(stops['t3']),
                position_size_pct=float(min(0.20 * scale, 1.0)),
                confidence='MED',
                signal_params={
                    'ret_12m': round(ret_12m, 4),
                    'excess_ret_12m': round(excess_ret_12m, 4),
                    'rf': round(rf, 4),
                    'within_bounds': True,
                },
            )]

        except Exception as e:
            print(f'[debug] signals=0 error={e}', file=sys.stderr)
            return []

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _get_risk_free(self, aux_data: dict) -> float:
        """Return annualised risk-free rate (fraction, not %)."""
        if not aux_data:
            return 0.0
        macro = aux_data.get('macro')
        if macro is None or macro.empty:
            return 0.0
        for col in ('DGS3MO', 'TB3MS', 'RF', 'rf', 'risk_free', 'T3M'):
            if col in macro.columns:
                series = macro[col].dropna()
                if len(series) == 0:
                    continue
                val = float(series.iloc[-1])
                return val / 100.0 if val > 1.0 else val
        return 0.0

    def _boundaries_ok(self, aux_data: dict) -> bool:
        """Return True when valuation Boundaries metric is below threshold (normal range)."""
        if not aux_data:
            return True   # fail-open: no macro → assume normal
        macro = aux_data.get('macro')
        if macro is None or macro.empty or len(macro) < 100:
            return True

        window = min(2520, len(macro))  # ~10 trading years

        def _scaled(col):
            """Scale column to [-1, 1] using rolling 10-yr min/max. Returns latest value."""
            s = macro[col].dropna()
            if len(s) < 50:
                return None
            rmin = s.rolling(window, min_periods=50).min()
            rmax = s.rolling(window, min_periods=50).max()
            rng = (rmax - rmin).replace(0.0, np.nan)
            scaled = 2.0 * (s - rmin) / rng - 1.0
            val = scaled.iloc[-1]
            return None if pd.isna(val) else float(val)

        # ── Term spread (required for Boundaries metric) ─────────────────────
        ts_col = next((c for c in ('TERM_SPREAD', 'T10Y2Y', 'T10Y3M', 'T10YFF', 'SLOPE', 'term_spread')
                       if c in macro.columns), None)
        if ts_col is None:
            return True  # can't compute boundaries → fail-open
        ts_val = _scaled(ts_col)
        if ts_val is None:
            return True

        # ── CAPE (required) ──────────────────────────────────────────────────
        cape_col = next((c for c in ('CAPE', 'shiller_pe', 'SHILLER_PE', 'cape', 'cyclically_adjusted_pe')
                         if c in macro.columns), None)
        if cape_col is not None:
            cape_val = _scaled(cape_col)
            if cape_val is not None:
                boundaries_cape = ts_val ** 2 + cape_val ** 2
                if boundaries_cape >= self.BOUNDARY_THRESHOLD:
                    return False

        # ── Dividend yield (optional; tightens gate when available) ──────────
        dy_col = next((c for c in ('DIV_YIELD', 'div_yield', 'DIVIDEND_YIELD', 'SP500_DIV_YIELD', 'dividend_yield')
                       if c in macro.columns), None)
        if dy_col is not None:
            dy_val = _scaled(dy_col)
            if dy_val is not None:
                boundaries_dy = ts_val ** 2 + dy_val ** 2
                if boundaries_dy >= self.BOUNDARY_THRESHOLD:
                    return False

        return True


# ── Standalone regime-partitioned backtest ────────────────────────────────────

def _run_backtest(prices_path: str, macro_path: str, regimes_path: str,
                  start: str = '2017-01-01', end: str = '2025-12-31') -> pd.DataFrame:
    """Simple vectorised backtest returning trades_df for run_backtest_with_regime_partition."""
    import os

    # ── Load prices ──────────────────────────────────────────────────────────
    if not os.path.exists(prices_path):
        return pd.DataFrame(columns=['strategy_id', 'signal_date', 'regime_state', 'pnl', 'r_multiple'])
    prices_df = pd.read_parquet(prices_path, columns=['SPY'] if 'SPY' in
                                pd.read_parquet(prices_path, columns=[]).columns else None)
    if 'SPY' not in prices_df.columns:
        return pd.DataFrame(columns=['strategy_id', 'signal_date', 'regime_state', 'pnl', 'r_multiple'])
    spy = prices_df['SPY'].dropna()
    spy = spy.loc[start:end]
    if len(spy) < 260:
        return pd.DataFrame(columns=['strategy_id', 'signal_date', 'regime_state', 'pnl', 'r_multiple'])

    # ── Load macro ───────────────────────────────────────────────────────────
    macro_df = pd.DataFrame()
    if os.path.exists(macro_path):
        macro_df = pd.read_parquet(macro_path)

    # ── Load historical regimes ──────────────────────────────────────────────
    regimes: dict[str, str] = {}
    if os.path.exists(regimes_path):
        reg_df = pd.read_parquet(regimes_path)
        date_col = 'date' if 'date' in reg_df.columns else reg_df.columns[0]
        state_col = 'regime_state' if 'regime_state' in reg_df.columns else reg_df.columns[1]
        for _, row in reg_df.iterrows():
            regimes[str(row[date_col])[:10]] = str(row[state_col])

    strategy = S_valuation_bounded_tsmom()
    records = []

    # Monthly signals: first trading day of each month
    monthly_dates = spy.resample('MS').first().index

    for sig_date in monthly_dates:
        date_str = str(sig_date.date())
        # History up to sig_date
        spy_hist = spy.loc[:sig_date]
        if len(spy_hist) < 252:
            continue

        # Build prices frame
        prices_win = pd.DataFrame({'SPY': spy_hist})

        # Build macro frame up to sig_date
        macro_win = macro_df.loc[:sig_date] if not macro_df.empty and hasattr(macro_df.index, 'date') else macro_df

        regime_state = regimes.get(date_str, 'LOW_VOL')
        regime = {'state': regime_state}

        sigs = strategy.generate_signals(prices_win, regime, ['SPY'],
                                         aux_data={'macro': macro_win} if not macro_win.empty else None)
        if not sigs:
            continue

        # 1-month forward return as proxy PnL
        future = spy.loc[sig_date:]
        if len(future) < 22:
            continue
        entry = float(future.iloc[0])
        exit_ = float(future.iloc[min(21, len(future) - 1)])
        pnl_pct = (exit_ - entry) / entry if entry > 0 else 0.0

        # Approximate R-multiple using 2% stop
        stop_dist = entry * 0.02
        r_multiple = pnl_pct / (stop_dist / entry) if stop_dist > 0 else 0.0

        records.append({
            'strategy_id': STRATEGY_ID,
            'signal_date': date_str,
            'regime_state': regime_state,
            'pnl': float(pnl_pct),
            'r_multiple': float(r_multiple),
        })

    return pd.DataFrame(records)


if __name__ == '__main__':
    import json
    from backtest.quick_backtest import run_backtest_with_regime_partition

    PARQUET_ROOT = '/root/openclaw/data/master'
    trades_df = _run_backtest(
        prices_path=f'{PARQUET_ROOT}/prices.parquet',
        macro_path=f'{PARQUET_ROOT}/macro.parquet',
        regimes_path=f'{PARQUET_ROOT}/historical_regimes.parquet',
    )
    print(f'Backtest produced {len(trades_df)} trades', file=sys.stderr)

    result = run_backtest_with_regime_partition(
        trades_df,
        strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(json.dumps(result, indent=2, default=str))
