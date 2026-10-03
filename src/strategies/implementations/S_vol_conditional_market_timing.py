"""
Volatility-Conditional Market Timing — Marquering & Verbeek (2004)
"The Economic Value of Predicting Stock Index Returns and Volatility",
Journal of Financial and Quantitative Analysis. https://doi.org/10.1017/s0022109000003136

Recursively re-estimated OLS forecasts of next-period S&P 500 return AND
realized volatility from lagged macro predictors (short rate, term spread)
and lagged realized vol [§3]. The two forecasts feed a myopic mean-variance
weight:
    weight = clip(E[ret] / (risk_aversion * vol_forecast^2), 0, 1)   (no short sales)
LONG SPY when weight > 0, else FLAT. Return predictability is concentrated
in high-volatility periods [§4-5], hence HIGH_VOL/TRANSITIONING eligibility.

Dividend yield (one of the paper's three predictors) is not present in the
macro ledger; the short rate (DTB3) and term spread (T10Y3M) — the ledger's
closest equivalents to the paper's predictor set — are used in its place.
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['VolConditionalMarketTiming']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_vol_conditional_market_timing'
MARKET_PROXY     = 'SPY'
TRAIN_WINDOW     = 756      # ~3 trading years [spec min_lookback_required]
VOL_WINDOW       = 21       # realized-vol estimation window
RISK_AVERSION    = 4.0      # myopic mean-variance risk-aversion coefficient [§3]
BASE_SIZE        = 0.12     # base gross fraction of portfolio at weight=1.0


def _short_rate_and_term_spread(aux_data: dict, idx: pd.DatetimeIndex):
    """Daily short-rate + term-spread series aligned to `idx`, ffilled from macro.

    aux_data['macro'] is {series_name: pd.Series(date_index -> value)}.
    Short rate: DTB3 (3mo T-bill). Term spread: T10Y3M (10yr - 3mo). Dividend
    yield is not in the ledger; these two rate series are the closest
    available substitutes for the paper's predictor set [§3].
    """
    macro = (aux_data or {}).get('macro') or {}
    short_rate = None
    for c in ('DTB3', 'DGS3MO', 'NYFED_EFFR', 'DFF'):
        if c in macro and macro[c] is not None and len(macro[c]) > 0:
            short_rate = macro[c]
            break
    term_spread = None
    for c in ('T10Y3M', 'T10Y2Y'):
        if c in macro and macro[c] is not None and len(macro[c]) > 0:
            term_spread = macro[c]
            break
    if short_rate is None or term_spread is None:
        return None

    idx = pd.DatetimeIndex(idx)
    sr = pd.Series(short_rate).astype(float)
    sr.index = pd.to_datetime(sr.index)
    ts = pd.Series(term_spread).astype(float)
    ts.index = pd.to_datetime(ts.index)

    sr_aligned = sr.reindex(sr.index.union(idx)).ffill().reindex(idx)
    ts_aligned = ts.reindex(ts.index.union(idx)).ffill().reindex(idx)
    return pd.DataFrame({'short_rate': sr_aligned, 'term_spread': ts_aligned}, index=idx)


def _ols_forecast(X_train: np.ndarray, y_train: np.ndarray, x_now: np.ndarray):
    """Plain multivariate OLS (with intercept) on X_train/y_train, forecast x_now.
    Returns None on degenerate input (too few rows for the predictor count)."""
    n, k = X_train.shape
    if n < max(30, 3 * (k + 1)):
        return None
    Xd = np.column_stack([np.ones(n), X_train])
    try:
        coef, *_ = np.linalg.lstsq(Xd, y_train, rcond=None)
    except np.linalg.LinAlgError:
        return None
    x_row = np.concatenate([[1.0], x_now])
    return float(x_row @ coef)


class VolConditionalMarketTiming(BaseStrategy):
    """
    Marquering & Verbeek (2004) joint return+volatility timing. Recursive OLS
    forecasts of next-period SPY return and realized volatility from lagged
    short rate / term spread / lagged vol feed a myopic mean-variance weight;
    LONG SPY when the weight is positive, else FLAT.
    """

    id                = STRATEGY_ID
    name              = 'VolConditionalMarketTiming'
    description       = (
        'Recursive-OLS joint return+volatility forecast (Marquering & Verbeek 2004) '
        'drives a mean-variance weight on SPY; LONG when weight > 0, else FLAT.'
    )
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = TRAIN_WINDOW + VOL_WINDOW
    active_in_regimes = ['HIGH_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 1

    def default_parameters(self) -> dict:
        return {
            'train_window':  TRAIN_WINDOW,
            'risk_aversion': RISK_AVERSION,
            'base_size':     BASE_SIZE,
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

        spy = prices[MARKET_PROXY].dropna()
        if len(spy) < self.min_lookback:
            print(f'[debug] {STRATEGY_ID}: signals=0 (insufficient bars={len(spy)})', file=sys.stderr)
            return []

        macro_df = _short_rate_and_term_spread(aux_data, spy.index)
        if macro_df is None or macro_df.dropna().empty:
            print(f'[debug] {STRATEGY_ID}: signals=0 (macro unavailable)', file=sys.stderr)
            return []

        rets = spy.pct_change()
        realized_vol = rets.rolling(VOL_WINDOW).std()

        data = pd.DataFrame({
            'ret':          rets,
            'realized_vol': realized_vol,
            'short_rate':   macro_df['short_rate'],
            'term_spread':  macro_df['term_spread'],
        })
        # Predictors observed through t forecast outcomes realized at t+1 (no look-ahead).
        data['ret_t1'] = data['ret'].shift(-1)
        data['vol_t1'] = data['realized_vol'].shift(-1)
        data = data.dropna()

        train_window = int(self.parameters.get('train_window', TRAIN_WINDOW))
        if len(data) < train_window + 2:
            print(f'[debug] {STRATEGY_ID}: signals=0 (data after align={len(data)})', file=sys.stderr)
            return []

        train   = data.iloc[-(train_window + 1):-1]   # trailing expanding-style window, excludes "now"
        now_row = data.iloc[-1]

        X_train = train[['realized_vol', 'short_rate', 'term_spread']].values.astype(float)
        x_now   = now_row[['realized_vol', 'short_rate', 'term_spread']].values.astype(float)

        ret_forecast = _ols_forecast(X_train, train['ret_t1'].values.astype(float), x_now)
        vol_forecast = _ols_forecast(X_train, train['vol_t1'].values.astype(float), x_now)

        if ret_forecast is None or vol_forecast is None:
            print(f'[debug] {STRATEGY_ID}: signals=0 (OLS fit failed)', file=sys.stderr)
            return []

        vol_forecast = max(vol_forecast, 1e-6)   # guard against non-positive vol forecast

        risk_aversion = float(self.parameters.get('risk_aversion', RISK_AVERSION))
        weight = ret_forecast / (risk_aversion * vol_forecast ** 2)
        weight = max(0.0, min(weight, 1.0))      # no short sales [§3]

        if weight <= 0.0:
            print(
                f'[debug] {STRATEGY_ID}: signals=0 (weight={weight:.4f} <= 0, '
                f'ret_fc={ret_forecast:.5f}, vol_fc={vol_forecast:.5f})', file=sys.stderr,
            )
            return []

        spy_price = float(spy.iloc[-1])
        if spy_price <= 0:
            print(f'[debug] {STRATEGY_ID}: signals=0 (invalid SPY price)', file=sys.stderr)
            return []

        scale    = self.position_scale(regime_state)
        pos_size = round(float(self.parameters.get('base_size', BASE_SIZE)) * weight * scale, 4)
        pos_size = max(0.01, min(pos_size, 0.20))

        st = self.compute_stops_and_targets(
            spy, direction='LONG', current_price=spy_price, regime_state=regime_state,
        )

        sharpe_forecast = ret_forecast / vol_forecast
        confidence = 'HIGH' if weight > 0.6 else ('MED' if weight > 0.25 else 'LOW')

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
                'ret_forecast':    round(float(ret_forecast), 6),
                'vol_forecast':    round(float(vol_forecast), 6),
                'sharpe_forecast': round(float(sharpe_forecast), 4),
                'weight':          round(float(weight), 4),
                'regime':          regime_state,
                'train_n':         len(train),
            },
        )

        print(
            f'[debug] {STRATEGY_ID}: signals=1 weight={weight:.4f} '
            f'ret_fc={ret_forecast:.5f} vol_fc={vol_forecast:.5f} regime={regime_state}',
            file=sys.stderr,
        )
        return [sig]


def run_regime_backtest():
    """Offline helper: compute regime-partitioned backtest metrics for the
    lifecycle promotion gate (candidate -> staging requires eligible_regimes_proposed)."""
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
    from backtest.quick_backtest import run_backtest_with_regime_partition

    prices_path = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'data', 'master', 'prices.parquet')
    macro_path  = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'data', 'master', 'macro.parquet')
    regime_path = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'data', 'master', 'historical_regimes.parquet')

    raw = pd.read_parquet(prices_path, columns=['ticker', 'date', 'close'])
    prices = raw.pivot(index='date', columns='ticker', values='close')
    prices.index = pd.to_datetime(prices.index)
    prices.sort_index(inplace=True)

    if MARKET_PROXY not in prices.columns:
        print('[backtest] SPY not in prices — aborting', file=sys.stderr)
        return None

    mac = pd.read_parquet(macro_path)
    mac['date'] = pd.to_datetime(mac['date'])
    macro = {}
    for series_name in ('DTB3', 'DGS3MO', 'T10Y3M', 'T10Y2Y'):
        sub = mac[mac['series'] == series_name]
        if not sub.empty:
            macro[series_name] = sub.set_index('date')['value'].sort_index()

    reg = pd.read_parquet(regime_path)
    reg['date'] = pd.to_datetime(reg['date'] if 'date' in reg.columns else reg.index)
    if 'date' in reg.columns:
        reg = reg.set_index('date')
    regime_col = next((c for c in ('regime_state', 'state', 'regime') if c in reg.columns), None)

    spy = prices[MARKET_PROXY].dropna()
    macro_df = _short_rate_and_term_spread({'macro': macro}, spy.index)
    if macro_df is None:
        print('[backtest] macro unavailable — aborting', file=sys.stderr)
        return None

    rets = spy.pct_change()
    realized_vol = rets.rolling(VOL_WINDOW).std()
    data = pd.DataFrame({
        'ret':          rets,
        'realized_vol': realized_vol,
        'short_rate':   macro_df['short_rate'],
        'term_spread':  macro_df['term_spread'],
    })
    data['ret_t1'] = data['ret'].shift(-1)
    data['vol_t1'] = data['realized_vol'].shift(-1)
    data = data.dropna()

    rows = []
    for i in range(TRAIN_WINDOW, len(data) - 1):
        train   = data.iloc[i - TRAIN_WINDOW:i]
        now_row = data.iloc[i]
        X_train = train[['realized_vol', 'short_rate', 'term_spread']].values.astype(float)
        x_now   = now_row[['realized_vol', 'short_rate', 'term_spread']].values.astype(float)
        ret_fc = _ols_forecast(X_train, train['ret_t1'].values.astype(float), x_now)
        vol_fc = _ols_forecast(X_train, train['vol_t1'].values.astype(float), x_now)
        if ret_fc is None or vol_fc is None:
            continue
        vol_fc = max(vol_fc, 1e-6)
        weight = max(0.0, min(ret_fc / (RISK_AVERSION * vol_fc ** 2), 1.0))
        signal_date  = data.index[i]
        regime_state = 'LOW_VOL'
        if regime_col and signal_date in reg.index:
            regime_state = str(reg.loc[signal_date, regime_col])
        direction = 'LONG' if weight > 0.0 else 'FLAT'
        pnl = float(data['ret_t1'].iloc[i]) * weight if direction == 'LONG' else 0.0
        rows.append({
            'strategy_id':  STRATEGY_ID,
            'signal_date':  str(signal_date.date()),
            'regime_state': regime_state,
            'direction':    direction,
            'pnl':          pnl,
            'r_multiple':   pnl / max(abs(pnl), 1e-6) if direction == 'LONG' else 0.0,
        })

    trades_df = pd.DataFrame(rows)
    if trades_df.empty:
        print('[backtest] no trades generated', file=sys.stderr)
        return None

    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(f'[backtest] eligible_regimes_proposed={result.get("eligible_regimes_proposed")}', file=sys.stderr)
    return result


if __name__ == '__main__':
    run_regime_backtest()
