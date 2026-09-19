"""
Commodity Seasonal DVR (Dummy-Variable Regression) Cross-Sectional Rotator.
Source: Kosch, R. & Forsberg, R. (2026), "Seasonal Trading in Commodity
Futures: Evidence from Regression and Singular Spectrum Signals."
http://arxiv.org/abs/2609.12227v1

Hypothesis: recurring harvest/weather/storage-driven demand cycles create
exploitable calendar-month return patterns in commodity markets. The paper
proposes THREE extraction methods (OLS dummy-variable regression, classical
SSA, robust low-rank SSA), each optionally vol-normalised, fit on rolling
windows and used to rank contracts long/short. NOTE: the paper's own
Maximum-Entropy-Bootstrap stress test finds no *statistically robust*
outperformance vs an equal-weight benchmark — this is a candidate on
literature support, not a proven edge; treat the backtest below as the
promotion gate, not the paper's abstract.

Interpretation choices (variant 1 of 2 — paper is ambiguous on all of the
below; direction (LONG top-ranked / SHORT bottom-ranked), monthly rebalance,
and vol-normalisation as an available option are the paper's unambiguous
claims and are preserved):

  * Method: DVR only (OLS on calendar-month dummies). The SSA / robust
    low-rank SSA variants are a materially different (eigendecomposition-
    based) extraction and are left to a separate strategy_id if adopted —
    bundling three estimators behind one signal would make the promotion
    gate's Sharpe attributable to none of them individually.
  * Estimation window: ROLLING 1260 trading days (~5y), not the paper's
    literal "10-year rolling window" — the live commodity-ETP price history
    in this system's master parquet only spans ~10.5y total (2016-01 to
    date), so a literal 10y window would leave almost no room for the
    window to actually roll across regimes before running out of history.
    5y is the largest window that still produces multiple independent
    fits across the backtest span.
  * Significance filter + vol-normalisation: score = per-ticker OLS
    t-statistic for the current calendar month's dummy coefficient
    (coefficient / standard-error), which is inherently both a magnitude
    and an uncertainty-normalised measure — this operationalises the
    paper's "volatility-normalised variant" as a t-stat rather than
    literally dividing by realized_vol, since t-stat also down-weights
    thin-sample months.
  * Universe: 15-name liquid commodity ETP basket (proxying the paper's
    15 commodity futures) — GLD/SLV/USO/UNG/DBA/DBC/DBB/DBE/CORN/WEAT/
    SOYB/CANE/PALL/PPLT/CPER — all confirmed present in
    data/master/prices.parquet with ~2016-01 to date coverage.
  * Cross-section width: LONG top 3 / SHORT bottom 3 of the up-to-15-name
    ranked universe each month (a 20% cut on each side), rather than a
    tighter top/bottom-1 or a looser median split.
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal

__all__ = ['CommoditySeasonalSsaDvr']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID      = 'S_commodity_seasonal_ssa_dvr'

# 15-name liquid commodity ETP basket (proxy for the paper's commodity futures).
BASKET = (
    'GLD', 'SLV', 'USO', 'UNG', 'DBA', 'DBC', 'DBB', 'DBE',
    'CORN', 'WEAT', 'SOYB', 'CANE', 'PALL', 'PPLT', 'CPER',
)

WINDOW_MONTHS = 60   # ~5y rolling estimation window
MIN_MONTH_OBS = 4    # minimum same-month observations to trust a coefficient
LEG_COUNT     = 3    # names LONG and names SHORT each rebalance


def _month_boundary(idx: pd.DatetimeIndex) -> bool:
    """True on the first trading day of a month (monthly rebalance gate)."""
    if len(idx) < 2:
        return False
    d1 = pd.Timestamp(idx[-1])
    d0 = pd.Timestamp(idx[-2])
    return d1.month != d0.month


def _dvr_month_tstats(monthly_ret: pd.DataFrame, month: int, min_obs: int = MIN_MONTH_OBS) -> pd.Series:
    """Per-ticker OLS dummy-variable-regression t-stat for calendar `month`.

    Fits return ~ C(month) (12 orthogonal dummies, no intercept) via lstsq —
    for an orthogonal dummy design this reduces to per-month sample means,
    but computing it via the regression residual gives a proper t-stat
    (coef / standard-error), not just a raw mean.
    """
    months = monthly_ret.index.month.values
    dummies = np.zeros((len(months), 12), dtype='float64')
    for i, m in enumerate(months):
        dummies[i, m - 1] = 1.0

    out = {}
    for ticker in monthly_ret.columns:
        y = monthly_ret[ticker].values.astype('float64')
        mask = ~np.isnan(y)
        if mask.sum() < 24:  # need enough total obs for a stable fit
            continue
        X = dummies[mask]
        yv = y[mask]
        n_m = int(X[:, month - 1].sum())
        if n_m < min_obs:
            continue
        coef, *_ = np.linalg.lstsq(X, yv, rcond=None)
        resid = yv - X @ coef
        dof = max(len(yv) - 12, 1)
        sigma = float(np.sqrt((resid ** 2).sum() / dof))
        if sigma <= 0:
            continue
        se = sigma / np.sqrt(n_m)
        if se <= 0:
            continue
        out[ticker] = float(coef[month - 1] / se)
    return pd.Series(out, dtype='float64')


class CommoditySeasonalSsaDvr(BaseStrategy):
    """Monthly cross-sectional commodity-ETP seasonal rotator via OLS
    dummy-variable regression. LONG top-3 / SHORT bottom-3 ranked t-stat
    of the current calendar month's seasonal coefficient, rolling 5y window.
    Source: http://arxiv.org/abs/2609.12227v1
    """

    id                = STRATEGY_ID
    name              = 'Commodity Seasonal DVR'
    description       = (
        'Monthly LONG/SHORT rotation across a 15-name commodity ETP basket, '
        'ranked by OLS dummy-variable-regression t-stat of the current '
        'calendar month seasonal coefficient over a rolling 5y window.'
    )
    tier              = 3
    signal_frequency  = 'monthly'
    calendar_edge     = True   # window IS the signal; ports across regime flips (2026-08-13)
    min_lookback      = 1260   # ~5y trading days
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = 2 * LEG_COUNT

    def default_parameters(self) -> dict:
        return {
            'window_months': WINDOW_MONTHS,
            'min_month_obs': MIN_MONTH_OBS,
            'leg_count':     LEG_COUNT,
            'pos_size_frac': 0.03,   # per-leg allocation (pre-regime-scale); 6 legs = 0.18 gross
        }

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print(f'[{STRATEGY_ID}] no price data — returning []', file=sys.stderr)
            print('[debug] signals=0', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []

        available = [t for t in BASKET if t in prices.columns]
        if len(available) < 6:
            print(f'[{STRATEGY_ID}] only {len(available)} basket names available — returning []', file=sys.stderr)
            print('[debug] signals=0', file=sys.stderr)
            return []

        if len(prices) < self.min_lookback or not _month_boundary(prices.index):
            print('[debug] signals=0', file=sys.stderr)
            return []

        p = self.parameters
        window_months = int(p.get('window_months', WINDOW_MONTHS))
        min_obs       = int(p.get('min_month_obs', MIN_MONTH_OBS))
        leg_count     = int(p.get('leg_count', LEG_COUNT))

        c = prices[available].astype('float64')
        monthly = c.resample('ME').last()
        mret = monthly.pct_change()

        asof = pd.Timestamp(prices.index[-1])
        month = asof.month

        # Completed months only, rolling window ending before the just-started month.
        mret = mret[mret.index < asof.replace(day=1)]
        mret = mret.tail(window_months)
        if len(mret) < 24:
            print('[debug] signals=0', file=sys.stderr)
            return []

        tstats = _dvr_month_tstats(mret, month, min_obs=min_obs)
        if len(tstats) < 6:
            print(f'[{STRATEGY_ID}] only {len(tstats)} usable t-stats — returning []', file=sys.stderr)
            print('[debug] signals=0', file=sys.stderr)
            return []

        ranked = tstats.sort_values(ascending=False)
        longs  = ranked.head(min(leg_count, len(ranked) // 2))
        shorts = ranked.tail(min(leg_count, len(ranked) // 2))
        # Guard against overlap on a tiny cross-section.
        shorts = shorts[~shorts.index.isin(longs.index)]

        scale    = self.position_scale(regime_state)
        pos_frac = p.get('pos_size_frac', 0.03)
        current  = c.iloc[-1]

        signals: List[Signal] = []

        def _emit(ticker: str, tstat: float, direction: str):
            raw = current.get(ticker)
            if raw is None or not np.isfinite(raw) or raw <= 0:
                return
            price = float(raw)
            series = c[ticker].dropna()
            if len(series) < 14:
                return
            stops = self.compute_stops_and_targets(series, direction, price, regime_state=regime_state)
            abs_t = abs(tstat)
            if abs_t >= 2.0:
                conf = 'HIGH'
            elif abs_t >= 1.0:
                conf = 'MED'
            else:
                conf = 'LOW'
            signals.append(Signal(
                ticker            = ticker,
                direction         = direction,
                entry_price       = price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = round(pos_frac * scale, 6),
                confidence        = conf,
                signal_params     = {
                    'month':        month,
                    'dvr_tstat':    round(float(tstat), 4),
                    'window_months': window_months,
                    'regime':       regime_state,
                },
            ))

        for ticker, tstat in longs.items():
            _emit(ticker, float(tstat), 'LONG')
        for ticker, tstat in shorts.items():
            _emit(ticker, float(tstat), 'SHORT')

        signals = signals[:self.MAX_SIGNALS]
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


# ---------------------------------------------------------------------------
# Regime-partitioned backtest
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    import json as _json
    from backtest.unified_backtest import load_prices_panels, load_regimes

    prices_df, _bars = load_prices_panels(tickers=BASKET)
    reg_series = load_regimes()

    available = [t for t in BASKET if t in prices_df.columns]
    c = prices_df[available].astype('float64').sort_index()
    monthly_close = c.resample('ME').last()
    mret_full = monthly_close.pct_change()

    rows = []
    month_ends = mret_full.index
    for i, dt in enumerate(month_ends):
        if i < WINDOW_MONTHS:
            continue
        month = dt.month
        window = mret_full.iloc[max(0, i - WINDOW_MONTHS):i]  # strictly prior months
        if len(window) < 24:
            continue
        tstats = _dvr_month_tstats(window, month, min_obs=MIN_MONTH_OBS)
        if len(tstats) < 6:
            continue
        ranked = tstats.sort_values(ascending=False)
        longs  = ranked.head(LEG_COUNT)
        shorts = ranked.tail(LEG_COUNT)
        shorts = shorts[~shorts.index.isin(longs.index)]

        # Realized return for month `dt` = the forward monthly return we just fit toward.
        fwd_ret = mret_full.loc[dt]
        entry_dt = dt  # month-end timestamp used as the signal_date proxy

        prior_regimes = reg_series[reg_series.index <= entry_dt]
        rstate = str(prior_regimes.iloc[-1]) if not prior_regimes.empty else 'LOW_VOL'

        for ticker, tstat in longs.items():
            r = fwd_ret.get(ticker)
            if r is None or pd.isna(r):
                continue
            rows.append({'strategy_id': STRATEGY_ID, 'signal_date': entry_dt, 'regime_state': rstate,
                         'pnl': float(r), 'r_multiple': round(float(r) / 0.02, 4)})
        for ticker, tstat in shorts.items():
            r = fwd_ret.get(ticker)
            if r is None or pd.isna(r):
                continue
            pnl = -float(r)
            rows.append({'strategy_id': STRATEGY_ID, 'signal_date': entry_dt, 'regime_state': rstate,
                         'pnl': pnl, 'r_multiple': round(pnl / 0.02, 4)})

    trades_df = pd.DataFrame(rows)
    print(f'[backtest] {len(trades_df)} trades', file=sys.stderr)

    from backtest.quick_backtest import run_backtest_with_regime_partition
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(_json.dumps(result, indent=2, default=str))
