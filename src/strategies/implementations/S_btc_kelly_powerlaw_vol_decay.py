"""
BTC Kelly Power-Law / Vol-Decay Sizing (Vera-Marun 2026)
Source: http://arxiv.org/abs/2609.22612v1

Structural long-only BTC-USD allocation. Power-law growth log(P)=log(A)+
alpha*log(t) plus vol-decay log(sigma^2)=log(sigma0^2)-2*gamma*log(t)
(t=days since genesis) imply a near time-invariant Kelly fraction
K*(t)=(alpha/sigma0^2)*t^(2*gamma-1) since empirical gamma~1/2 -- a sizing
rule, not a timing signal.

Interpretation variant 1/2 (paper leaves window/cadence unspecified): fixed
6yr vol-decay regression window (paper range 4-9yr; a 9yr pick would smooth
more history in); quarterly-sampled sigma^2(t) points, not daily; t = days
since Bitcoin genesis (2009-01-03), not since first ledger row; stateless
re-fit every call rather than cached to an explicit cadence; raw K* capped
at PORTFOLIO_CAP=0.20 (not the paper's 1.0) since this is one sleeve of a
blended book -- the uncapped value is still carried in signal_params.
"""
from __future__ import annotations

import sys
from datetime import date
from typing import List, Optional

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal

__all__ = ['BtcKellyPowerlawVolDecay']

INSTRUMENT_CLASS = 'crypto'
STRATEGY_ID      = 'S_btc_kelly_powerlaw_vol_decay'
GENESIS_DATE     = date(2009, 1, 3)
GENESIS_TS       = pd.Timestamp(GENESIS_DATE)

MIN_LOOKBACK       = 2520   # ~10yr of daily rows before we trust the fit
VOL_WINDOW_YEARS   = 6      # chosen interpretation within paper's 4-9yr range
VOL_WINDOW_DAYS    = VOL_WINDOW_YEARS * 365
SAMPLE_STEP_DAYS   = 63     # quarterly sampling cadence for the vol regression
MIN_VOL_SAMPLES    = 8
KELLY_CAP_LEVERAGE = 1.0    # paper's raw cap
PORTFOLIO_CAP      = 0.20   # practical multi-strategy adaptation (see docstring)


def _fit_kelly_fraction(series: pd.Series) -> Optional[dict]:
    """Fit alpha (power-law growth) and gamma (vol-decay) on `series`
    (date-indexed BTC-USD closes) and return the Kelly fraction, or None if
    there isn't enough clean history to trust the regression."""
    if len(series) < MIN_LOOKBACK:
        return None
    try:
        idx    = pd.to_datetime(series.index)
        ages   = (idx - GENESIS_TS).days.to_numpy().astype(float)
        prices = series.to_numpy(dtype=float)
        valid  = (ages > 0) & (prices > 0)
        if int(valid.sum()) < MIN_LOOKBACK:
            return None
        ages_v, prices_v = ages[valid], prices[valid]
        alpha, _ = np.polyfit(np.log(ages_v), np.log(prices_v), 1)
        log_rets = np.diff(np.log(prices_v))
        ages_r   = ages_v[1:]
        if len(log_rets) < VOL_WINDOW_DAYS + SAMPLE_STEP_DAYS * MIN_VOL_SAMPLES:
            return None
        log_t_vol, log_sigma_vol = [], []
        for p in range(VOL_WINDOW_DAYS, len(log_rets), SAMPLE_STEP_DAYS):
            sigma_sq = float(np.var(log_rets[p - VOL_WINDOW_DAYS:p])) * 365.0  # annualized
            if sigma_sq > 0:
                log_t_vol.append(float(np.log(ages_r[p])))
                log_sigma_vol.append(float(np.log(sigma_sq)))
        if len(log_t_vol) < MIN_VOL_SAMPLES:
            return None
        slope_v, intercept_v = np.polyfit(log_t_vol, log_sigma_vol, 1)
        gamma, sigma0_sq = -slope_v / 2.0, float(np.exp(intercept_v))
        if alpha <= 0 or sigma0_sq <= 0:
            return None
        t_now  = float(ages_v[-1])
        k_star = max(0.0, min((alpha / sigma0_sq) * (t_now ** (2.0 * gamma - 1.0)), KELLY_CAP_LEVERAGE))
        return {'alpha': float(alpha), 'gamma': float(gamma), 'sigma0_sq': sigma0_sq,
                't_now': t_now, 'k_star': k_star, 'n_vol_samples': len(log_t_vol)}
    except Exception:
        return None


class BtcKellyPowerlawVolDecay(BaseStrategy):
    """Volatility-scaled, near-time-invariant Kelly allocation to BTC-USD
    (Vera-Marun 2026): structural sizing, not a market-timing signal."""

    id                = STRATEGY_ID
    name              = 'BTC Kelly Power-Law Vol-Decay'
    description       = (
        'Power-law growth + vol-decay exponent (gamma~1/2) imply a near '
        'time-invariant Kelly-optimal BTC-USD allocation fraction.'
    )
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = MIN_LOOKBACK
    instrument_class  = INSTRUMENT_CLASS
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = 1

    def generate_signals(
        self, prices: pd.DataFrame, regime: dict, universe: List[str], aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty or 'BTC-USD' not in prices.columns:
            signals: List[Signal] = []
            print(f'[debug] signals={len(signals)}', file=sys.stderr)
            return signals
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            signals = []
            print(f'[debug] signals={len(signals)}', file=sys.stderr)
            return signals
        series = prices['BTC-USD'].dropna()
        fit = _fit_kelly_fraction(series)
        if fit is None:
            signals = []
            print(f'[{STRATEGY_ID}] insufficient/invalid fit -> signals=0', file=sys.stderr)
            return signals
        current_price = float(series.iloc[-1])
        scale     = self.position_scale(regime_state)
        raw_size  = min(fit['k_star'], PORTFOLIO_CAP)
        pos_size  = round(raw_size * scale, 4)
        if pos_size < 0.001:
            signals = []
            print(f'[{STRATEGY_ID}] pos_size<0.001 -> signals=0', file=sys.stderr)
            return signals
        st = self.compute_stops_and_targets(
            series, direction='LONG', current_price=current_price, regime_state=regime_state)
        gamma_dev  = abs(fit['gamma'] - 0.5)
        confidence = 'HIGH' if gamma_dev < 0.05 else ('MED' if gamma_dev < 0.15 else 'LOW')

        sig = Signal(
            ticker            = 'BTC-USD',
            direction         = 'LONG',
            entry_price       = current_price,
            stop_loss         = float(st['stop']),
            target_1          = float(st['t1']),
            target_2          = float(st['t2']),
            target_3          = float(st['t3']),
            position_size_pct = pos_size,
            confidence        = confidence,
            signal_params     = {
                'alpha_power_law':      round(fit['alpha'], 4),
                'gamma_vol_decay':      round(fit['gamma'], 4),
                'sigma0_sq':            round(fit['sigma0_sq'], 6),
                'kelly_fraction_raw':   round(fit['k_star'], 4),
                'age_days_since_genesis': int(fit['t_now']),
                'vol_window_years':     VOL_WINDOW_YEARS,
                'n_vol_samples':        fit['n_vol_samples'],
                'regime':               regime_state,
            },
        )
        signals = [sig]
        print(f'[{STRATEGY_ID}] alpha={fit["alpha"]:.3f} gamma={fit["gamma"]:.3f} '
              f'k_star={fit["k_star"]:.3f} size={pos_size} regime={regime_state}', file=sys.stderr)
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


if __name__ == '__main__':
    # Regime-partitioned backtest skeleton (lifecycle promotion gate requirement).
    # Run: python3 src/strategies/implementations/S_btc_kelly_powerlaw_vol_decay.py
    import os, json
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))
    PARQUET_ROOT = os.environ.get('OPENCLAW_PARQUET_ROOT', '/root/openclaw/data/master')
    prices_s = pd.read_parquet(f'{PARQUET_ROOT}/prices.parquet', columns=['BTC-USD'])['BTC-USD'].dropna()
    reg_df   = pd.read_parquet(f'{PARQUET_ROOT}/historical_regimes.parquet')

    def _lookup_regime(dt):
        mask = reg_df.index <= dt
        return str(reg_df.loc[mask, 'state'].iloc[-1]) if mask.any() else 'LOW_VOL'

    rows, in_trade, ep, ed, er = [], False, None, None, None
    for pos in range(MIN_LOOKBACK, len(prices_s), SAMPLE_STEP_DAYS):
        dt, price = prices_s.index[pos], float(prices_s.iloc[pos])
        fit    = _fit_kelly_fraction(prices_s.iloc[:pos + 1])
        active = fit is not None and min(fit['k_star'], PORTFOLIO_CAP) > 0.001
        reg    = _lookup_regime(dt)
        if active and not in_trade:
            in_trade, ep, ed, er = True, price, dt, reg
        elif not active and in_trade:
            pnl = (price - ep) / ep
            rows.append({'strategy_id': STRATEGY_ID, 'signal_date': str(ed)[:10],
                         'regime_state': er, 'pnl': pnl, 'r_multiple': pnl / 0.02})
            in_trade = False
    if in_trade:
        price = float(prices_s.iloc[-1])
        pnl   = (price - ep) / ep
        rows.append({'strategy_id': STRATEGY_ID, 'signal_date': str(ed)[:10],
                     'regime_state': er, 'pnl': pnl, 'r_multiple': pnl / 0.02})
    trades_df = pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=['strategy_id', 'signal_date', 'regime_state', 'pnl', 'r_multiple'])
    print(f'[backtest] completed_trades={len(trades_df)}', file=sys.stderr)
    from backtest.quick_backtest import run_backtest_with_regime_partition
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 3, 'min_avg_r': 0.0},
    )
    print(json.dumps(result, indent=2, default=str))
