"""
Volatility Indicators for Predicting S&P 500 Drawdowns — QuantSeeker (2026)
https://www.quantseeker.com/p/volatility-indicators-for-predicting

Two volatility indicators carry incremental predictive information about future
S&P 500 drawdowns:
  1. HAR-RV realized-vol forecaster — captures persistence in realized vol via
     daily/weekly/monthly squared-return components (Corsi 2009 variant).
  2. VX futures timing indicator — VIX level z-score + VIX momentum z-score,
     which together time transitions into VX futures backwardation (stress).

Composite drawdown score > threshold → FLAT (sidestep the drawdown).
Composite drawdown score ≤ threshold → LONG SPY (hold equity exposure).

Exact formula and thresholds are paywalled; indicators reconstructed from
abstract + landing page description of the incremental predictive power claim.
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['VolIndicatorsSP500DrawdownTiming']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_vol_indicators_sp500_drawdown_timing'
MARKET_PROXY     = 'SPY'

# Lookback windows
RV_SHORT_W  = 5    # short-term realized vol (days)
RV_MED_W    = 21   # medium-term realized vol (≈ 1 month)
RV_LONG_W   = 63   # long-term realized vol (≈ 1 quarter)
VIX_HIST_W  = 252  # VIX normalization window (≈ 1 year)
VIX_MOM_W   = 10   # VIX momentum look-back (2 weeks)
NORM_W      = 252  # rolling z-score window for composite

DRAWDOWN_THRESHOLD = 0.30   # score > this → FLAT
BASE_SIZE          = 0.10   # base portfolio fraction


def _har_rv_score(spy_rets: pd.Series) -> pd.Series:
    """
    HAR-RV realized-vol indicator (Indicator 1).
    Weighted sum of daily / weekly / monthly squared returns normalized by
    the long-run (quarterly) variance level.  Higher score → higher expected
    future realized vol → higher drawdown risk.
    """
    rv_1  = spy_rets.pow(2)
    rv_5  = rv_1.rolling(RV_SHORT_W, min_periods=3).mean()
    rv_21 = rv_1.rolling(RV_MED_W,   min_periods=10).mean()
    rv_63 = rv_1.rolling(RV_LONG_W,  min_periods=30).mean()
    # HAR-RV composite: 0.4 daily + 0.3 weekly + 0.3 monthly
    har = 0.4 * rv_1 + 0.3 * rv_5 + 0.3 * rv_21
    # Ratio to long-run level; captures regime-adjusted elevation
    return (har - rv_63) / (rv_63 + 1e-10)


def _vx_timing_score(vix_aligned: pd.Series) -> pd.Series:
    """
    VX futures timing indicator (Indicator 2).
    Combines VIX level z-score (contango/backwardation proxy) and VIX
    momentum z-score.  Both rising together signals backwardation / stress.
    """
    # Level component: VIX vs its 1-year rolling mean/std
    mu = vix_aligned.rolling(VIX_HIST_W, min_periods=126).mean()
    sd = vix_aligned.rolling(VIX_HIST_W, min_periods=126).std()
    level_z = (vix_aligned - mu) / (sd + 1e-8)

    # Momentum component: 10-day VIX change standardized by rolling std
    chg    = vix_aligned.diff(VIX_MOM_W)
    chg_sd = chg.rolling(VIX_HIST_W, min_periods=126).std()
    mom_z  = chg / (chg_sd + 1e-8)

    return 0.5 * level_z + 0.5 * mom_z


def _roll_zscore(s: pd.Series, w: int = NORM_W, mp: int = 63) -> pd.Series:
    mu = s.rolling(w, min_periods=mp).mean()
    sd = s.rolling(w, min_periods=mp).std()
    return (s - mu) / (sd + 1e-8)


class VolIndicatorsSP500DrawdownTiming(BaseStrategy):
    """
    Tactically times SPY LONG/FLAT allocation using two vol indicators:
    HAR-RV realized-vol forecaster + VIX level/momentum VX timing signal.
    LONG when composite drawdown score is low; FLAT when score is elevated.
    Source: QuantSeeker (2026).
    """

    id                = STRATEGY_ID
    name              = 'VolIndicatorsSP500DrawdownTiming'
    description       = (
        'HAR-RV + VIX timing composite predicts S&P 500 drawdowns; '
        'LONG SPY when score is low, FLAT when drawdown risk is elevated.'
    )
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = VIX_HIST_W + NORM_W + 10  # ~2.4 years
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = 1

    def default_parameters(self) -> dict:
        return {
            'drawdown_threshold': DRAWDOWN_THRESHOLD,
            'base_size':          BASE_SIZE,
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
        if MARKET_PROXY not in prices.columns:
            print(f'[debug] {STRATEGY_ID}: signals=0 (SPY not in prices)', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print(f'[debug] {STRATEGY_ID}: signals=0 (regime={regime_state} excluded)', file=sys.stderr)
            return []

        # Resolve VIX — prefer price-panel column (^VIX), fall back to aux_data
        vix_series = None
        if '^VIX' in prices.columns:
            vix_series = prices['^VIX'].dropna()
        if vix_series is None or vix_series.empty:
            macro = (aux_data or {}).get('macro') or {}
            vix_series = macro.get('VIX')
        if vix_series is None or (hasattr(vix_series, 'empty') and vix_series.empty):
            print(f'[debug] {STRATEGY_ID}: signals=0 (VIX unavailable)', file=sys.stderr)
            return []

        min_bars = VIX_HIST_W + NORM_W + 10
        if len(prices) < min_bars:
            print(f'[debug] {STRATEGY_ID}: signals=0 (insufficient bars={len(prices)})', file=sys.stderr)
            return []

        spy_rets    = prices[MARKET_PROXY].pct_change()
        vix_aligned = pd.Series(vix_series).reindex(prices.index, method='ffill')

        # Indicator 1: HAR-RV realized-vol forecaster
        har_raw  = _har_rv_score(spy_rets.reindex(prices.index))
        har_norm = _roll_zscore(har_raw)

        # Indicator 2: VX futures timing
        vx_raw   = _vx_timing_score(vix_aligned)
        vx_norm  = _roll_zscore(vx_raw)

        # Composite drawdown score (equal weight)
        score = 0.5 * har_norm + 0.5 * vx_norm

        latest_score = score.dropna().iloc[-1] if not score.dropna().empty else float('nan')
        if np.isnan(latest_score):
            print(f'[debug] {STRATEGY_ID}: signals=0 (score is NaN)', file=sys.stderr)
            return []

        threshold = float(self.parameters.get('drawdown_threshold', DRAWDOWN_THRESHOLD))
        if latest_score > threshold:
            print(
                f'[debug] {STRATEGY_ID}: signals=0 (FLAT score={latest_score:.3f} > {threshold})',
                file=sys.stderr,
            )
            return []

        # Generate LONG SPY signal
        spy_price = float(prices[MARKET_PROXY].dropna().iloc[-1])
        if spy_price <= 0:
            print(f'[debug] {STRATEGY_ID}: signals=0 (invalid SPY price)', file=sys.stderr)
            return []

        scale    = self.position_scale(regime_state)
        pos_size = round(float(self.parameters.get('base_size', BASE_SIZE)) * scale, 4)
        pos_size = max(0.01, min(pos_size, 0.20))

        spy_series = prices[MARKET_PROXY].dropna()
        st = self.compute_stops_and_targets(
            spy_series,
            direction='LONG',
            current_price=spy_price,
            regime_state=regime_state,
        )

        abs_score  = abs(latest_score)
        confidence = 'HIGH' if abs_score < 0.10 else ('MED' if abs_score < 0.30 else 'LOW')

        har_last = float(har_norm.dropna().iloc[-1]) if not har_norm.dropna().empty else 0.0
        vx_last  = float(vx_norm.dropna().iloc[-1])  if not vx_norm.dropna().empty  else 0.0

        sig = Signal(
            ticker            = MARKET_PROXY,
            direction         = 'LONG',
            entry_price       = spy_price,
            stop_loss         = float(st['stop']),
            target_1          = float(st['t1']),
            target_2          = float(st['t2']),
            target_3          = float(st['t3']),
            position_size_pct = pos_size,
            confidence        = confidence,
            signal_params     = {
                'drawdown_score': round(float(latest_score), 4),
                'har_z':          round(har_last, 4),
                'vx_z':           round(vx_last,  4),
                'regime':         regime_state,
            },
        )

        print(
            f'[debug] {STRATEGY_ID}: signals=1 '
            f'score={latest_score:.4f} har_z={har_last:.3f} vx_z={vx_last:.3f} '
            f'regime={regime_state} size={pos_size}',
            file=sys.stderr,
        )
        return [sig]


def run_regime_backtest():
    """
    Offline regime-partitioned backtest for lifecycle promotion gate.
    Loads prices + macro from master parquets, runs the vol-indicator drawdown
    timing logic over the full history, then calls
    run_backtest_with_regime_partition() so eligible_regimes_proposed is
    available for candidate→staging promotion.
    """
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

    from backtest.quick_backtest import run_backtest_with_regime_partition

    base = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'data', 'master')
    prices_path = os.path.join(base, 'prices.parquet')
    macro_path  = os.path.join(base, 'macro.parquet')
    regime_path = os.path.join(base, 'historical_regimes.parquet')

    # Load daily close prices (wide: date × ticker)
    raw    = pd.read_parquet(prices_path, columns=['ticker', 'date', 'close'])
    prices = raw.pivot(index='date', columns='ticker', values='close')
    prices.index = pd.to_datetime(prices.index)
    prices.sort_index(inplace=True)

    if MARKET_PROXY not in prices.columns:
        print(f'[backtest] {MARKET_PROXY} not in prices — aborting', file=sys.stderr)
        return None

    # Load VIX
    mac      = pd.read_parquet(macro_path)
    mac['date'] = pd.to_datetime(mac['date'])
    vix_df   = mac[mac['series'] == 'VIX'] if 'series' in mac.columns else mac[mac.get('name', mac.columns[0]) == 'VIX']
    vix_s    = vix_df.set_index('date')['value'].sort_index() if 'value' in vix_df.columns else pd.Series(dtype=float)

    # Fall back to ^VIX column in price panel
    if vix_s.empty and '^VIX' in prices.columns:
        vix_s = prices['^VIX'].dropna()

    if vix_s.empty:
        print('[backtest] VIX unavailable — aborting', file=sys.stderr)
        return None

    # Load historical regimes
    reg = pd.read_parquet(regime_path)
    reg.index = pd.to_datetime(reg.index if reg.index.name else reg.get('date', reg.index))
    regime_col = next(
        (c for c in ('regime_state', 'state', 'regime') if c in reg.columns), None
    )

    spy_rets    = prices[MARKET_PROXY].pct_change()
    vix_aligned = vix_s.reindex(prices.index, method='ffill')

    har_raw  = _har_rv_score(spy_rets)
    har_norm = _roll_zscore(har_raw)
    vx_raw   = _vx_timing_score(vix_aligned)
    vx_norm  = _roll_zscore(vx_raw)
    score    = (0.5 * har_norm + 0.5 * vx_norm).dropna()

    rows = []
    spy_fwd = prices[MARKET_PROXY].pct_change().shift(-1)
    for signal_date, s in score.items():
        if np.isnan(s):
            continue
        direction = 'FLAT' if s > DRAWDOWN_THRESHOLD else 'LONG'
        pnl = float(spy_fwd.get(signal_date, 0.0) or 0.0) if direction == 'LONG' else 0.0
        r   = pnl / max(abs(pnl), 1e-6) if direction == 'LONG' and pnl != 0.0 else 0.0
        regime_state = 'LOW_VOL'
        if regime_col and signal_date in reg.index:
            regime_state = str(reg.loc[signal_date, regime_col])
        rows.append({
            'strategy_id': STRATEGY_ID,
            'signal_date': str(signal_date.date()),
            'regime_state': regime_state,
            'direction': direction,
            'pnl': pnl,
            'r_multiple': r,
        })

    trades_df = pd.DataFrame(rows)
    if trades_df.empty:
        print('[backtest] no trades generated', file=sys.stderr)
        return None

    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(
        f'[backtest] eligible_regimes_proposed={result.get("eligible_regimes_proposed")}',
        file=sys.stderr,
    )
    return result


if __name__ == '__main__':
    run_regime_backtest()
