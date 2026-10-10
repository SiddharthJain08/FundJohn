"""S_expectile_wasserstein_robust_portfolio — robust mean-expectile cross-
sectional equity sleeve.

Source: http://arxiv.org/abs/2610.11917v1 "Multi-period Mean-Expectile
Portfolio Optimization under Wasserstein Ambiguity: Reformulation,
Degeneracy and the Role of the Ground Metric" (Yadav & Mehra, 2026).

Thesis: replacing CVaR with the expectile risk measure in a Wasserstein-
robust portfolio problem gives a tractable worst-case reformulation
(envelope theorem) whose price-of-robustness damps more gracefully than
CVaR as the ambiguity radius grows. Translated here into a daily
cross-sectional rank-and-select sleeve (house rule against covariance-
based optimizers; the backtest harness scores one day at a time, not a
T-period LP).

Interpretation variant 2 of 2. The abstract is unambiguous that CVaR is
replaced by the expectile under Wasserstein ambiguity; everything else
(expectile level, ground metric, radius construction, long/short
constraint, multi-period collapse, selection/sizing) is free. This
variant deliberately resolves each of those free choices DIFFERENTLY
from variant 1:

  - Expectile level tau = 0.40 (near-symmetric, mild downside weighting)
    instead of variant 1's tau = 0.10 (aggressive downside tail). This is
    the milder reading flagged as an equally defensible alternative in
    variant 1's own docstring.
  - Ground metric = L2 (quadratic/Euclidean distance on returns) instead
    of variant 1's L1. Under an L2 ground metric the natural dispersion
    statistic coupling to the ambiguity ball is the standard deviation,
    not the MAD; the worst-case expectile dilation is therefore taken as
    risk_robust = risk + epsilon directly (epsilon itself already carries
    the L2 scale), rather than variant 1's risk + epsilon*max(tau,1-tau)
    L1 dilation.
  - Ambiguity radius epsilon is a single FIXED global scalar per
    rebalance (epsilon = ROBUSTNESS_FRAC * cross-sectional mean of
    per-ticker return std-dev), not variant 1's per-ticker
    decision-dependent MAD-based radius. This is the "single fixed
    constant" reading the paper's own structural results treat as the
    simpler, non-decision-dependent special case.
  - No-short constraint is NOT taken literally: direction_vocab permits
    SHORT, so this variant runs a market-neutral long/short decile
    spread (LONG the best-scored decile, SHORT the worst-scored decile)
    rather than variant 1's long-only top-5%.
  - Multi-period collapse uses an EXPONENTIALLY-WEIGHTED mean/expectile
    estimate over the lookback window (recent observations weighted
    more heavily, halflife = LOOKBACK/4) instead of variant 1's flat
    equal-weighted window — a different, still single-period, reading
    of how to fold a multi-period plan into one rebalance.
  - Selection/sizing: decile (10%) long/short legs, equal-weighted within
    each leg, scaled so gross exposure matches variant 1's conventions —
    not a solved LP portfolio (no scipy.optimize / covariance inversion
    per repo policy).
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal

__all__ = ['ExpectileWassersteinRobustPortfolioTV2']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_expectile_wasserstein_robust_portfolio'

TAU              = 0.40     # near-symmetric expectile level (mild downside weighting)
ROBUSTNESS_FRAC  = 0.5      # epsilon = ROBUSTNESS_FRAC * mean cross-sectional std
LOOKBACK         = 1260     # ~5y trading days (spec min_lookback_required)
MIN_UNIVERSE     = 90       # spec minimum_universe_size (90 FTSE constituents)
DECILE_FRAC      = 0.10     # top/bottom decile legs
BASE_SIZE_PCT    = 0.015    # smaller per-name size since both legs are populated
MAX_ITER         = 30
RISK_FLOOR       = 1e-4
EWM_HALFLIFE     = LOOKBACK / 4.0


def _ewm_expectile(returns: pd.DataFrame, tau: float, halflife: float, max_iter: int = MAX_ITER) -> pd.Series:
    """Fixed-point solve for the per-column tau-expectile of `returns`, using
    exponentially time-decayed weights (recent rows weighted more heavily) in
    place of variant 1's flat-window weighting."""
    n = len(returns)
    decay = 0.5 ** (1.0 / halflife)
    age = np.arange(n - 1, -1, -1)
    time_w = pd.Series(decay ** age, index=returns.index)
    tw_sum = time_w.sum()

    m = returns.mul(time_w, axis=0).sum() / tw_sum
    for _ in range(max_iter):
        above = returns.gt(m, axis=1)
        tau_w = above.astype(float) * tau + (~above).astype(float) * (1.0 - tau)
        combined = tau_w.mul(time_w, axis=0)
        denom = combined.sum()
        new_m = (returns * combined).sum() / denom.replace(0, np.nan)
        new_m = new_m.fillna(m)
        if float((new_m - m).abs().max()) < 1e-6:
            m = new_m
            break
        m = new_m
    return m, time_w


def _robust_scores(returns: pd.DataFrame) -> pd.Series:
    """Robust mean / worst-case-expectile-risk score, per ticker (higher = better)."""
    expectile, time_w = _ewm_expectile(returns, TAU, EWM_HALFLIFE)
    tw_sum = time_w.sum()
    mean_ret = returns.mul(time_w, axis=0).sum() / tw_sum
    risk = -expectile  # positive when the downside-weighted fixed point is negative

    std = returns.std()
    epsilon = ROBUSTNESS_FRAC * float(std.mean())  # single fixed global radius (L2 reading)
    risk_robust = (risk + epsilon).clip(lower=RISK_FLOOR)
    return mean_ret / risk_robust


class ExpectileWassersteinRobustPortfolioTV2(BaseStrategy):
    """Market-neutral long/short decile sleeve ranked by robust mean / worst-
    case-expectile-risk score (Wasserstein-dilated tau=0.40 expectile, L2
    ground metric via a fixed global std-based radius)."""

    id                = STRATEGY_ID
    name              = 'ExpectileWassersteinRobustPortfolioTV2'
    description       = (
        'Rank-based long/short decile sleeve: robust mean-return / worst-case-'
        'expectile-risk score (tau=0.40, L2-Wasserstein-dilated by a fixed '
        'global 0.5x mean-std radius, EWM-weighted lookback). Per Yadav & '
        'Mehra (2026), variant 2 of 2.'
    )
    tier              = 2
    min_lookback      = LOOKBACK
    active_in_regimes = ['HIGH_VOL', 'CRISIS']
    instrument_class  = INSTRUMENT_CLASS
    MAX_SIGNALS       = 30

    def default_parameters(self) -> dict:
        return {'tau': TAU, 'robustness_frac': ROBUSTNESS_FRAC, 'base_size': BASE_SIZE_PCT}

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

        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < MIN_UNIVERSE:
            print(f'[debug] signals=0 (universe {len(tickers)} < {MIN_UNIVERSE})', file=sys.stderr)
            return []

        price_data = prices[tickers].ffill().dropna(how='all')
        if len(price_data) < LOOKBACK + 5:
            print(f'[debug] signals=0 (need {LOOKBACK + 5} rows, got {len(price_data)})', file=sys.stderr)
            return []

        returns_full = price_data.pct_change().iloc[-LOOKBACK:]
        valid_counts = returns_full.notna().sum()
        common = valid_counts[valid_counts >= LOOKBACK * 0.9].index.tolist()
        if len(common) < MIN_UNIVERSE:
            print(f'[debug] signals=0 (valid {len(common)} < {MIN_UNIVERSE})', file=sys.stderr)
            return []

        returns = returns_full[common].dropna(how='all')
        score = _robust_scores(returns).dropna()
        if len(score) < MIN_UNIVERSE:
            print(f'[debug] signals=0 (scored {len(score)} < {MIN_UNIVERSE})', file=sys.stderr)
            return []

        ranked = score.sort_values(ascending=False)
        n_leg = max(1, int(len(ranked) * DECILE_FRAC))
        long_tickers = ranked.index[:n_leg].tolist()
        short_tickers = ranked.index[-n_leg:].tolist()

        per_leg_cap = max(1, self.MAX_SIGNALS // 2)
        long_tickers = long_tickers[:per_leg_cap]
        short_tickers = short_tickers[:per_leg_cap]

        scale = self.position_scale(regime_state)
        base_size = float(self.parameters.get('base_size', BASE_SIZE_PCT))
        latest = price_data.iloc[-1]

        long_cut = ranked.loc[long_tickers].quantile(0.5) if long_tickers else 0.0
        short_cut = ranked.loc[short_tickers].quantile(0.5) if short_tickers else 0.0

        signals: List[Signal] = []
        for ticker, direction, conf_cut, better in (
            *((t, 'LONG', long_cut, True) for t in long_tickers),
            *((t, 'SHORT', short_cut, False) for t in short_tickers),
        ):
            px = float(latest.get(ticker, 0.0))
            if px <= 0:
                continue
            size = round(min(base_size * scale, 1.0), 4)
            s = float(score[ticker])
            confidence = 'HIGH' if (s > conf_cut if better else s < conf_cut) else 'MED'
            stops = self.compute_stops_and_targets(
                price_data[ticker].dropna(), direction=direction, current_price=px,
                regime_state=regime_state,
            )
            signals.append(Signal(
                ticker            = ticker,
                direction         = direction,
                entry_price       = round(px, 4),
                stop_loss         = float(stops['stop']),
                target_1          = float(stops['t1']),
                target_2          = float(stops['t2']),
                target_3          = float(stops['t3']),
                position_size_pct = size,
                confidence        = confidence,
                signal_params     = {
                    'robust_score': round(s, 6),
                    'tau':          TAU,
                    'regime':       regime_state,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


# ── Regime-partitioned backtest ───────────────────────────────────────────────
if __name__ == '__main__':
    import json
    import os

    from backtest.quick_backtest import run_backtest_with_regime_partition

    ROOT = os.environ.get('OPENCLAW_PARQUET_ROOT', '/root/openclaw/data/master')
    REBALANCE_DAYS = 21
    HOLD_DAYS = 21

    try:
        long_df = pd.read_parquet(os.path.join(ROOT, 'prices.parquet'))
        wide = long_df.pivot_table(index='date', columns='ticker', values='close')
        wide.index = pd.to_datetime(wide.index)
        wide = wide.sort_index().loc['2017-01-01':'2025-12-31'].ffill()

        reg_df = pd.read_parquet(os.path.join(ROOT, 'historical_regimes.parquet'))
        reg_df['date'] = pd.to_datetime(reg_df['date'])
        regime_map = dict(zip(reg_df['date'], reg_df['regime']))

        valid_cols = wide.columns[wide.notna().sum() >= LOOKBACK + HOLD_DAYS + 5]
        wide = wide[valid_cols]
        rows = []

        idx = LOOKBACK
        while idx + HOLD_DAYS < len(wide):
            window = wide.iloc[idx - LOOKBACK:idx]
            counts = window.notna().sum()
            common = counts[counts >= LOOKBACK * 0.9].index.tolist()
            if len(common) >= MIN_UNIVERSE:
                returns = window[common].pct_change().dropna(how='all')
                score = _robust_scores(returns).dropna()
                if len(score) >= MIN_UNIVERSE:
                    ranked = score.sort_values(ascending=False)
                    n_leg = max(1, int(len(ranked) * DECILE_FRAC))
                    longs = ranked.index[:n_leg].tolist()
                    shorts = ranked.index[-n_leg:].tolist()
                    entry_date = wide.index[idx]
                    exit_date = wide.index[idx + HOLD_DAYS]
                    regime_state = regime_map.get(entry_date, 'LOW_VOL')
                    for t in longs:
                        p0, p1 = wide[t].iloc[idx], wide[t].iloc[idx + HOLD_DAYS]
                        if pd.notna(p0) and pd.notna(p1) and p0 > 0:
                            pnl = (p1 - p0) / p0
                            rows.append({'strategy_id': STRATEGY_ID, 'signal_date': str(entry_date.date()),
                                         'regime_state': regime_state, 'pnl': pnl, 'r_multiple': pnl / 0.05})
                    for t in shorts:
                        p0, p1 = wide[t].iloc[idx], wide[t].iloc[idx + HOLD_DAYS]
                        if pd.notna(p0) and pd.notna(p1) and p0 > 0:
                            pnl = (p0 - p1) / p0
                            rows.append({'strategy_id': STRATEGY_ID, 'signal_date': str(entry_date.date()),
                                         'regime_state': regime_state, 'pnl': pnl, 'r_multiple': pnl / 0.05})
            idx += REBALANCE_DAYS

        trades_df = pd.DataFrame(rows)
        if trades_df.empty:
            print(json.dumps({'error': 'no trades generated'}))
        else:
            result = run_backtest_with_regime_partition(
                trades_df, strategy_id=STRATEGY_ID,
                thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
            )
            print(json.dumps(result, default=str, indent=2))
    except Exception as e:
        import traceback
        print(json.dumps({'error': str(e), 'traceback': traceback.format_exc()}))
