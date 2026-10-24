"""
ECF Bias-Corrected Liquidity Premium — exploit Roll-model spread
misclassification.

Source: Rana, A., Ravichandran Iyer, S., Juneja, S. (2026), "Bias correcting
transaction cost models for characteristic function based estimators"
(arXiv:2610.25445).

Hypothesis: Roll's classical effective-spread estimator infers the bid-ask
spread from the NEGATIVE first-order serial covariance of daily price
changes. When that covariance turns POSITIVE, practitioners "rescue" the
formula by taking the absolute value before the square root, which
systematically OVERSTATES cost for names whose true microstructure cost is
low. The paper's characteristic-function estimator instead applies a
first-order finite-sample bias correction to the serial covariance before
clamping at zero, reporting near-zero cost for exactly those names. The gap
between the naive ("Roll") estimate and the bias-corrected ("ECF") estimate
identifies mispriced liquidity: LONG names Roll over-penalizes (truly
cheap), SHORT names it under-penalizes (truly expensive) — per the paper's
direction_vocab and decile convention.

Interpretation variant 2 of 2 (abstract-only extraction; no formula
appendix). This variant deliberately differs from sibling variant 1 (tv1)
on every ambiguous choice:
  (a) price-change convention: RAW price differences (P_t - P_t-1), matching
      the paper's literal wording "autocovariance of daily price changes" —
      Roll's original 1984 formula and the classical CF-spread literature
      are both specified on price levels, not log returns. tv1 used log
      returns. The resulting $-spread is normalized to bps by dividing by
      the window-average price before cross-sectional ranking, since a
      comparison across tickers of different price levels otherwise isn't
      meaningful.
  (b) bias-correction functional form: a MULTIPLICATIVE small-sample
      correction, cov1_corrected = cov1 * n/(n-2) — the standard
      degrees-of-freedom-style inflation applied to a lag-1 sample
      autocovariance — rather than tv1's ADDITIVE var/n term. Both are
      valid O(1/n) first-order corrections; this is the alternative the
      tv1 docstring explicitly flagged as a colleague's plausible reading.
  (c) formation window: the FULL min_lookback_required=504-day (~2y) window
      estimated fresh at each rebalance, rather than tv1's adaptive rolling
      126-day window — a CF-based estimator's asymptotic distribution
      (the paper's stated advantage over Roll) needs more observations to
      be trustworthy, so this variant favors the longer, noisier-but-larger
      sample over short-window adaptivity.
  (d) rebalance cadence: QUARTERLY (first trading day of Jan/Apr/Jul/Oct)
      instead of monthly — paired with the full 504-day window, monthly
      re-estimation would mostly re-measure the same overlapping history;
      quarterly matches the slower-moving nature of a 2-year estimate.
  (e) eligible regimes: ['LOW_VOL', 'TRANSITIONING'] only — tv1 also ran in
      HIGH_VOL. The bias-correction term here assumes a fairly stable
      variance structure over the full 2-year window; under HIGH_VOL that
      assumption is weaker, so this variant is more conservative.
The paper's unambiguous spec items are kept as-is: rank by
(roll_spread - ecf_spread), LONG top decile, SHORT bottom decile,
minimum_universe_size=100, min_lookback_required=504 trading days. Data:
close-price panel only (engine-supplied `prices`) — no volume/financials.
"""
from __future__ import annotations

import sys
from typing import List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from strategies.safe_ops import safe_div

__all__ = ['EcfBiasCorrectedLiquidityPremiumTv2']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_ecf_bias_corrected_liquidity_premium'


class EcfBiasCorrectedLiquidityPremiumTv2(BaseStrategy):
    """LONG names Roll's naive abs-covariance spread over-penalizes (truly
    low-cost, top decile of roll-minus-ecf gap); SHORT names it
    under-penalizes (truly high-cost, bottom decile). Raw price-change
    autocovariance, multiplicative n/(n-2) bias correction, full 504-day
    formation window, quarterly cadence."""

    id                = STRATEGY_ID
    name              = 'EcfBiasCorrectedLiquidityPremiumTv2'
    description       = ('Roll naive abs-covariance spread (raw price diffs) vs multiplicatively '
                         'bias-corrected ECF spread over a full 2y window: LONG top-decile gap '
                         '(Roll over-penalized/cheap), SHORT bottom-decile; quarterly, >=100 names.')
    tier              = 2
    signal_frequency  = 'quarterly'
    min_lookback      = 504          # paper's min_lookback_required (2y of daily obs)
    instrument_class  = INSTRUMENT_CLASS
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 20

    FORMATION_DAYS  = 504   # full min_lookback window, re-struck quarterly
    MIN_UNIVERSE    = 100   # paper's minimum_universe_size
    LEG_COUNT       = 10
    BASE_SIZE_LONG  = 0.015
    BASE_SIZE_SHORT = 0.012

    def default_parameters(self) -> dict:
        return {
            'formation_days': self.FORMATION_DAYS,
            'leg_count':      self.LEG_COUNT,
        }

    def _quarter_boundary(self, prices: pd.DataFrame) -> bool:
        """True on the first trading day of a calendar quarter."""
        if len(prices) < 2:
            return False
        d1 = pd.Timestamp(prices.index[-1])
        d0 = pd.Timestamp(prices.index[-2])
        q1 = (d1.month - 1) // 3
        q0 = (d0.month - 1) // 3
        return q1 != q0 or d1.year != d0.year

    @staticmethod
    def _roll_vs_ecf_spread(diffs: pd.DataFrame, avg_price: pd.Series) -> tuple[pd.Series, pd.Series]:
        """Vectorized per-column (ticker) Roll-naive vs bias-corrected-ECF
        effective-spread estimate from a window of RAW price changes,
        normalized to a relative (bps-of-price) scale.

        Roll (naive):   2*sqrt(|cov1|) / price                      — abs() rescue when cov1 >= 0
        ECF (corrected): 2*sqrt(max(0, -(cov1 * n/(n-2)))) / price   — multiplicative small-sample
                                                                        inflation before the sign clamp
        """
        d_t   = diffs.iloc[1:]
        d_lag = diffs.shift(1).iloc[1:]
        n     = d_t.count()                                     # per-ticker valid-pair count
        mean_t, mean_l = d_t.mean(), d_lag.mean()
        cov1  = ((d_t - mean_t) * (d_lag - mean_l)).mean()       # per-ticker lag-1 sample autocovariance
        dof_factor = n / (n - 2).clip(lower=1)                   # multiplicative first-order correction
        cov1_corr  = cov1 * dof_factor
        roll_spread_px = 2.0 * np.sqrt(cov1.abs())
        ecf_spread_px  = 2.0 * np.sqrt((-cov1_corr).clip(lower=0.0))
        roll_spread = safe_div(roll_spread_px, avg_price)
        ecf_spread  = safe_div(ecf_spread_px, avg_price)
        return roll_spread, ecf_spread

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty or len(prices) < self.min_lookback:
            print('[debug] signals=0', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []

        if not (self._quarter_boundary(prices) or self.cadence_reset(regime)):
            print('[debug] signals=0', file=sys.stderr)
            return []

        fdays = int(self.parameters.get('formation_days', self.FORMATION_DAYS))
        pool  = [t for t in universe if t in prices.columns] or list(prices.columns)
        if len(pool) < self.MIN_UNIVERSE:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # `prices.index` is a union across every instrument, including 24/7
        # crypto that prints on calendar days equities don't trade. Restrict
        # to days where a meaningful fraction of the pool actually reports,
        # or the trailing fdays+1-row slice under-counts real equity prints.
        counts    = prices[pool].notna().sum(axis=1)
        eq_thresh = max(self.MIN_UNIVERSE, int(0.05 * len(pool)))
        eq_days   = prices.index[counts >= eq_thresh]
        if len(eq_days) < fdays + 2:
            print('[debug] signals=0', file=sys.stderr)
            return []

        eq = prices.loc[eq_days, pool]
        window     = eq.iloc[-(fdays + 1):].astype('float64')
        window     = window.where(window > 0)
        valid_cols = [t for t in window.columns if window[t].notna().sum() >= fdays]
        if len(valid_cols) < self.MIN_UNIVERSE:
            print('[debug] signals=0', file=sys.stderr)
            return []
        window    = window[valid_cols]
        diffs     = window.diff()
        avg_price = window.mean()

        roll_spread, ecf_spread = self._roll_vs_ecf_spread(diffs, avg_price)
        gap = (roll_spread - ecf_spread).replace([np.inf, -np.inf], np.nan).dropna()
        if len(gap) < self.MIN_UNIVERSE:
            print('[debug] signals=0', file=sys.stderr)
            return []

        rank_pct = gap.rank(pct=True)
        n_leg = min(int(self.parameters.get('leg_count', self.LEG_COUNT)),
                    max(1, int(len(gap) * 0.10)))
        # Top decile: Roll overstates cost relative to ECF -> truly cheap -> LONG.
        longs  = gap[rank_pct >= 0.90].sort_values(ascending=False).head(n_leg)
        # Bottom decile: Roll understates cost relative to ECF -> truly expensive -> SHORT.
        shorts = gap[rank_pct <= 0.10].sort_values(ascending=True).head(n_leg)

        scale   = self.position_scale(regime_state)
        current = window.iloc[-1]

        def _conf(ext: float) -> str:
            if ext >= 0.95:
                return 'HIGH'
            if ext >= 0.88:
                return 'MED'
            return 'LOW'

        signals: List[Signal] = []
        for leg, direction, base in ((longs, 'LONG', self.BASE_SIZE_LONG),
                                     (shorts, 'SHORT', self.BASE_SIZE_SHORT)):
            for ticker, g in leg.items():
                if len(signals) >= self.MAX_SIGNALS:
                    break
                raw = current.get(ticker)
                if raw is None or not np.isfinite(raw) or raw <= 0:
                    continue
                price  = float(raw)
                series = prices[ticker].dropna()
                stops  = self.compute_stops_and_targets(series, direction, price,
                                                         regime_state=regime_state)
                rp        = float(rank_pct[ticker])
                extremity = rp if direction == 'LONG' else 1.0 - rp
                signals.append(Signal(
                    ticker=ticker,
                    direction=direction,
                    entry_price=price,
                    stop_loss=stops['stop'],
                    target_1=stops['t1'],
                    target_2=stops['t2'],
                    target_3=stops['t3'],
                    position_size_pct=round(base * scale, 6),
                    confidence=_conf(extremity),
                    signal_params={
                        'roll_spread_bps': round(float(roll_spread[ticker]) * 1e4, 4),
                        'ecf_spread_bps':  round(float(ecf_spread[ticker]) * 1e4, 4),
                        'gap_bps':         round(float(g) * 1e4, 4),
                        'rank_pct':        round(rp, 4),
                    },
                ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


# ---------------------------------------------------------------------------
# Regime-partitioned backtest
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    import os
    import json as _json

    from backtest.quick_backtest import run_backtest_with_regime_partition

    PARQUET_ROOT = os.environ.get('OPENCLAW_PARQUET_ROOT', '/root/openclaw/data/master')
    long_df = pd.read_parquet(f'{PARQUET_ROOT}/prices.parquet', columns=['ticker', 'date', 'close'])
    long_df['date'] = pd.to_datetime(long_df['date'])
    prices_df = long_df.pivot(index='date', columns='ticker', values='close').sort_index()

    reg_df = pd.read_parquet(f'{PARQUET_ROOT}/historical_regimes.parquet')
    reg_df['date'] = pd.to_datetime(reg_df['date'])
    reg_df = reg_df.sort_values('date')

    def _regime_on(dt):
        mask = reg_df['date'] <= dt
        return str(reg_df.loc[mask, 'regime'].iloc[-1]) if mask.any() else 'LOW_VOL'

    strategy = EcfBiasCorrectedLiquidityPremiumTv2()
    universe = list(prices_df.columns)
    hold_days = 63  # ~1 trading quarter, matches the quarterly cadence

    rows = []
    for i in range(strategy.min_lookback, len(prices_df) - hold_days):
        snap = prices_df.iloc[:i]
        dt   = snap.index[-1]
        if not strategy._quarter_boundary(snap):
            continue  # skip non-quarter-boundary days up front
        regime_s = _regime_on(dt)
        signals  = strategy.generate_signals(snap, {'state': regime_s}, universe)
        exit_dt  = prices_df.index[i + hold_days]
        for s in signals:
            exit_px = prices_df[s.ticker].get(exit_dt)
            if exit_px is None or pd.isna(exit_px):
                continue
            ret = (float(exit_px) - s.entry_price) / s.entry_price
            if s.direction == 'SHORT':
                ret = -ret
            risk = abs(s.entry_price - s.stop_loss)
            rows.append({
                'strategy_id':  STRATEGY_ID,
                'signal_date':  str(dt.date()),
                'regime_state': regime_s,
                'pnl':          float(ret),
                'r_multiple':   float(ret * s.entry_price / risk) if risk > 0 else 0.0,
            })

    if not rows:
        print('No trades generated', file=sys.stderr)
        sys.exit(1)

    result = run_backtest_with_regime_partition(
        pd.DataFrame(rows), strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(_json.dumps(result, indent=2, default=str))
