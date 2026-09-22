"""
Commodity Seasonal SSA (Singular Spectrum Analysis) Cross-Sectional Rotator.
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

Interpretation choices (variant 2 of 2 — deliberately DIFFERENT from a DVR/
t-stat colleague implementation on every axis the paper leaves ambiguous;
direction (LONG top-ranked / SHORT bottom-ranked), monthly rebalance, and
vol-normalisation as an available option are the paper's unambiguous claims
and are preserved):

  * Method: classical SSA (singular spectrum analysis), NOT DVR. For each
    ticker's monthly-return window we build a trajectory (Hankel) matrix,
    take its SVD, and treat the LEADING eigentriple as trend/noise and the
    SECOND+THIRD eigentriples (the classic annual-harmonic pair in SSA
    seasonality literature) as the seasonal component. That component is
    reconstructed via anti-diagonal (Hankel) averaging back into a
    denoised monthly series, and the seasonal forecast for the current
    calendar month is the mean of the reconstructed series at that
    month's historical phase. This is a materially different (eigen-
    decomposition-based) extraction than an OLS dummy-variable fit.
  * Vol-normalisation: LITERAL division by realized volatility (annualised
    daily-return std over the estimation window), using the optional
    `aux_data['realized_vol']` column when present and falling back to an
    in-window computation otherwise — operationalising the paper's
    "volatility-normalised variant" exactly as stated, rather than folding
    uncertainty into a t-statistic.
  * Estimation window: ROLLING 36 months (3y), not tv1's 5y and not the
    paper's literal 10y — a shorter window lets the seasonal eigentriple
    track regime drift faster and leaves ~7.5y of rolling history for the
    promotion-gate backtest to accumulate trades (this system's commodity-
    ETP history spans ~10.5y total, 2016-01 to date).
  * Cross-section width: a LOOSER MEDIAN SPLIT (top half LONG / bottom half
    SHORT of the ranked universe each month), rather than tv1's tighter
    top/bottom-3 (20%) cut — the paper only specifies "top-ranked /
    bottom-ranked", leaving the cut width unstated.
  * Universe: same 15-name liquid commodity ETP basket as tv1 (GLD/SLV/
    USO/UNG/DBA/DBC/DBB/DBE/CORN/WEAT/SOYB/CANE/PALL/PPLT/CPER) — basket
    composition is a data-availability constraint, not an interpretive
    choice, so it is held constant across variants for comparability.
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

WINDOW_MONTHS = 36   # ~3y rolling estimation window (deliberately shorter than tv1's 5y)
EMBED_L       = 12   # SSA trajectory-matrix embedding dimension (annual cycle)
MIN_OBS       = 24   # minimum monthly observations to attempt an SSA fit


def _month_boundary(idx: pd.DatetimeIndex) -> bool:
    """True on the first trading day of a month (monthly rebalance gate)."""
    if len(idx) < 2:
        return False
    d1 = pd.Timestamp(idx[-1])
    d0 = pd.Timestamp(idx[-2])
    return d1.month != d0.month


def _ssa_seasonal_reconstruction(y: np.ndarray, L: int = EMBED_L) -> np.ndarray:
    """Classical SSA: trajectory-matrix SVD, drop the leading (trend)
    eigentriple, reconstruct the series from the next two (seasonal
    harmonic pair) via anti-diagonal averaging. Returns a length-N array."""
    n = len(y)
    L = min(L, n // 2)
    if L < 2:
        return np.full(n, np.nan)
    K = n - L + 1
    X = np.empty((L, K), dtype='float64')
    for i in range(L):
        X[i, :] = y[i:i + K]

    U, S, Vt = np.linalg.svd(X, full_matrices=False)
    n_comp = len(S)
    if n_comp < 2:
        return np.full(n, np.nan)
    lo, hi = 1, min(3, n_comp)  # components 2..3 (0-indexed 1:3), skip trend (index 0)
    Xr = U[:, lo:hi] @ np.diag(S[lo:hi]) @ Vt[lo:hi, :]

    # Hankelization: average every anti-diagonal back into a length-n series.
    recon = np.zeros(n, dtype='float64')
    counts = np.zeros(n, dtype='float64')
    for i in range(L):
        for j in range(K):
            recon[i + j] += Xr[i, j]
            counts[i + j] += 1.0
    counts[counts == 0] = 1.0
    return recon / counts


def _ssa_month_scores(monthly_ret: pd.DataFrame, month: int, min_obs: int = MIN_OBS) -> pd.Series:
    """Per-ticker SSA seasonal score for calendar `month`: mean of the
    reconstructed seasonal component at that month's historical phase,
    divided by realized (in-window) volatility of the raw monthly series."""
    months = monthly_ret.index.month.values
    out = {}
    for ticker in monthly_ret.columns:
        y = monthly_ret[ticker].values.astype('float64')
        mask = ~np.isnan(y)
        if mask.sum() < min_obs:
            continue
        y_clean = y[mask]
        months_clean = months[mask]
        recon = _ssa_seasonal_reconstruction(y_clean)
        if np.all(np.isnan(recon)):
            continue
        phase_mask = months_clean == month
        if phase_mask.sum() < 2:
            continue
        seasonal_fcast = float(np.nanmean(recon[phase_mask]))
        vol = float(np.nanstd(y_clean, ddof=1))
        if vol <= 0 or not np.isfinite(vol):
            continue
        out[ticker] = seasonal_fcast / vol
    return pd.Series(out, dtype='float64')


class CommoditySeasonalSsaDvr(BaseStrategy):
    """Monthly cross-sectional commodity-ETP seasonal rotator via classical
    SSA (singular spectrum analysis). LONG top-half / SHORT bottom-half of
    the ranked universe by vol-normalised SSA seasonal score, rolling 3y
    window. Source: http://arxiv.org/abs/2609.12227v1 (variant 2: SSA, not
    DVR; literal realized-vol normalisation; median-split cross-section).
    """

    id                = STRATEGY_ID
    name              = 'Commodity Seasonal SSA'
    description       = (
        'Monthly LONG/SHORT median-split rotation across a 15-name commodity '
        'ETP basket, ranked by realized-vol-normalised SSA seasonal score of '
        'the current calendar month over a rolling 3y window.'
    )
    tier              = 3
    signal_frequency  = 'monthly'
    calendar_edge     = True   # window IS the signal; ports across regime flips
    min_lookback      = 756    # ~3y trading days
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = 16     # up to 8 long + 8 short on the full 15-name basket

    def default_parameters(self) -> dict:
        return {
            'window_months': WINDOW_MONTHS,
            'embed_l':       EMBED_L,
            'pos_size_frac': 0.025,   # per-leg allocation (pre-regime-scale)
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

        c = prices[available].astype('float64')
        monthly = c.resample('ME').last()
        mret = monthly.pct_change()

        asof = pd.Timestamp(prices.index[-1])
        month = asof.month

        # Completed months only, rolling window ending before the just-started month.
        mret = mret[mret.index < asof.replace(day=1)]
        mret = mret.tail(window_months)
        if len(mret) < MIN_OBS:
            print('[debug] signals=0', file=sys.stderr)
            return []

        realized_vol_aux = (aux_data or {}).get('realized_vol') if aux_data else None

        scores = _ssa_month_scores(mret, month)
        if len(scores) < 6:
            print(f'[{STRATEGY_ID}] only {len(scores)} usable SSA scores — returning []', file=sys.stderr)
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Literal vol-normalisation override: if a ledger-provided realized_vol
        # is available for a ticker, rescale the score by (in-window vol / ledger vol)
        # so the final normaliser matches the ledger's realized_vol definition.
        if isinstance(realized_vol_aux, dict):
            for ticker in list(scores.index):
                rv = realized_vol_aux.get(ticker)
                if rv and np.isfinite(rv) and rv > 0:
                    in_window_vol = float(np.nanstd(mret[ticker].dropna().values, ddof=1))
                    if in_window_vol > 0:
                        scores[ticker] = scores[ticker] * (in_window_vol / float(rv))

        ranked = scores.sort_values(ascending=False)
        half = len(ranked) // 2
        if half < 1:
            print('[debug] signals=0', file=sys.stderr)
            return []
        longs  = ranked.head(half)
        shorts = ranked.tail(half)
        shorts = shorts[~shorts.index.isin(longs.index)]

        scale    = self.position_scale(regime_state)
        pos_frac = p.get('pos_size_frac', 0.025)
        current  = c.iloc[-1]

        signals: List[Signal] = []

        def _emit(ticker: str, score: float, direction: str):
            raw = current.get(ticker)
            if raw is None or not np.isfinite(raw) or raw <= 0:
                return
            price = float(raw)
            series = c[ticker].dropna()
            if len(series) < 14:
                return
            stops = self.compute_stops_and_targets(series, direction, price, regime_state=regime_state)
            abs_s = abs(score)
            if abs_s >= 1.5:
                conf = 'HIGH'
            elif abs_s >= 0.5:
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
                    'month':         month,
                    'ssa_score':     round(float(score), 4),
                    'window_months': window_months,
                    'regime':        regime_state,
                },
            ))

        for ticker, score in longs.items():
            _emit(ticker, float(score), 'LONG')
        for ticker, score in shorts.items():
            _emit(ticker, float(score), 'SHORT')

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
        if len(window) < MIN_OBS:
            continue
        scores = _ssa_month_scores(window, month)
        if len(scores) < 6:
            continue
        ranked = scores.sort_values(ascending=False)
        half = len(ranked) // 2
        if half < 1:
            continue
        longs  = ranked.head(half)
        shorts = ranked.tail(half)
        shorts = shorts[~shorts.index.isin(longs.index)]

        fwd_ret = mret_full.loc[dt]
        entry_dt = dt

        prior_regimes = reg_series[reg_series.index <= entry_dt]
        rstate = str(prior_regimes.iloc[-1]) if not prior_regimes.empty else 'LOW_VOL'

        for ticker, score in longs.items():
            r = fwd_ret.get(ticker)
            if r is None or pd.isna(r):
                continue
            rows.append({'strategy_id': STRATEGY_ID, 'signal_date': entry_dt, 'regime_state': rstate,
                         'pnl': float(r), 'r_multiple': round(float(r) / 0.02, 4)})
        for ticker, score in shorts.items():
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
