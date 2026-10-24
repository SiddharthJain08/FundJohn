"""
ECF Bias-Corrected Liquidity Premium — exploit Roll-model spread
misclassification.

Source: Rana, A., Ravichandran Iyer, S., Juneja, S. (2026), "Bias correcting
transaction cost models for characteristic function based estimators"
(arXiv:2610.25445).

Hypothesis: Roll's classical effective-spread estimator infers the bid-ask
spread from the NEGATIVE first-order serial covariance of daily price
changes (spread = 2*sqrt(-cov1)). When that serial covariance turns
POSITIVE, the textbook formula is undefined, so practitioners routinely
"rescue" it by taking the absolute value of the covariance before the
square root — a naive patch that systematically OVERSTATES transaction
cost for names whose true microstructure cost is actually low. The paper's
characteristic-function-based estimator instead applies a first-order
finite-sample bias correction to the serial covariance before clamping it
at zero, so it reports near-zero cost for exactly these names. The gap
between the naive ("Roll") estimate and the bias-corrected ("ECF") estimate
identifies names the market systematically misprices for liquidity: LONG
the names Roll over-penalizes (truly cheap to trade), SHORT the names Roll
under-penalizes (truly expensive to trade) — per the paper's stated
direction_vocab and decile convention.

Variant 1/2 — only an abstract is available (no formula appendix), so the
covariance-estimation window, the bias term and the rebalance cadence below
are this implementation's defensible choices, deliberately different from
a colleague's plausible reading:
  - formation window = 126 trading days (~6mo) for the rolling serial-
    covariance estimate, re-struck monthly — a colleague could just as
    defensibly estimate the whole 504-day history at once; this variant
    keeps the estimate adaptive to the recent microstructure regime.
  - bias term = var(returns)/n, the known O(-1/n) finite-sample bias of a
    lag-1 sample autocovariance under near-white-noise, added back before
    clamping — a colleague could instead apply a Newey-West-style HAC
    correction; this variant uses the simpler first-order term the paper's
    abstract explicitly calls out ("first-order bias-corrected").
  - returns are LOG returns (not raw price differences), so the resulting
    spread estimate is already a relative (%-of-price) cost and comparable
    across tickers of different price levels without any extra scaling.
The paper's unambiguous spec items are kept as-is: rank by
(roll_spread - ecf_spread), LONG the top decile, SHORT the bottom decile,
minimum_universe_size=100, min_lookback_required=504 trading days. Data:
close-price panel only (engine-supplied `prices`) — no volume/financials,
matching the paper's price-change-only estimator.
"""
from __future__ import annotations

import sys
from typing import List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal

__all__ = ['EcfBiasCorrectedLiquidityPremium']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_ecf_bias_corrected_liquidity_premium'


class EcfBiasCorrectedLiquidityPremium(BaseStrategy):
    """LONG names Roll's naive serial-covariance spread estimator
    over-penalizes (truly low-cost, top decile of roll-minus-ecf gap);
    SHORT names it under-penalizes (truly high-cost, bottom decile)."""

    id                = STRATEGY_ID
    name              = 'ECF Bias-Corrected Liquidity Premium'
    description       = ('Roll naive abs-covariance spread vs first-order bias-corrected ECF spread: '
                         'LONG top-decile gap (Roll over-penalized/cheap), SHORT bottom-decile '
                         '(Roll under-penalized/expensive); monthly, 2y min history, >=100 names.')
    tier              = 2
    signal_frequency  = 'monthly'
    min_lookback      = 504          # paper's min_lookback_required (2y of daily obs)
    instrument_class  = INSTRUMENT_CLASS
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 20

    FORMATION_DAYS  = 126   # rolling serial-covariance estimation window (~6mo)
    MIN_UNIVERSE    = 100   # paper's minimum_universe_size
    LEG_COUNT       = 10
    BASE_SIZE_LONG  = 0.015
    BASE_SIZE_SHORT = 0.012

    def default_parameters(self) -> dict:
        return {
            'formation_days': self.FORMATION_DAYS,
            'leg_count':      self.LEG_COUNT,
        }

    def _month_boundary(self, prices: pd.DataFrame) -> bool:
        """True on the first trading day of a month (monthly cadence gate)."""
        if len(prices) < 2:
            return False
        d1 = pd.Timestamp(prices.index[-1])
        d0 = pd.Timestamp(prices.index[-2])
        return d1.month != d0.month

    @staticmethod
    def _roll_vs_ecf_spread(rets: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
        """Vectorized per-column (ticker) Roll-naive vs bias-corrected-ECF
        effective-spread estimate from a window of log returns.

        Roll (naive):   2*sqrt(|cov1|)                    — abs() rescue when cov1 >= 0
        ECF (corrected): 2*sqrt(max(0, -(cov1 + var/n)))   — first-order bias added back
                                                              before the sign clamp
        """
        r_t   = rets.iloc[1:]
        r_lag = rets.shift(1).iloc[1:]
        n     = r_t.count()                                  # per-ticker valid-pair count
        mean_t, mean_l = r_t.mean(), r_lag.mean()
        cov1  = ((r_t - mean_t) * (r_lag - mean_l)).mean()    # per-ticker lag-1 sample autocovariance
        var   = rets.var(ddof=1)
        bias_term  = var / n.clip(lower=1)
        cov1_corr  = cov1 + bias_term
        roll_spread = 2.0 * np.sqrt(cov1.abs())
        ecf_spread  = 2.0 * np.sqrt((-cov1_corr).clip(lower=0.0))
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

        if not (self._month_boundary(prices) or self.cadence_reset(regime)):
            print('[debug] signals=0', file=sys.stderr)
            return []

        fdays = int(self.parameters.get('formation_days', self.FORMATION_DAYS))
        pool  = [t for t in universe if t in prices.columns] or list(prices.columns)
        if len(pool) < self.MIN_UNIVERSE:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # `prices.index` is a union across every instrument, including 24/7
        # crypto that prints on calendar days equities don't trade. Slicing
        # the trailing fdays+1 ROWS without filtering those union-only days
        # out shrinks the effective equity formation window (observed: a
        # nominal 127-row slice held as few as ~88 real equity prints),
        # starving the serial-covariance estimate. Restrict to days where a
        # meaningful fraction of the pool actually reports.
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
        window = window[valid_cols]
        rets   = np.log(window / window.shift(1))

        roll_spread, ecf_spread = self._roll_vs_ecf_spread(rets)
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

    strategy = EcfBiasCorrectedLiquidityPremium()
    universe = list(prices_df.columns)
    hold_days = 21  # ~1 trading month

    rows = []
    for i in range(strategy.min_lookback, len(prices_df) - hold_days):  # every day: month-boundary is a single exact day, a >1 step can skip past it
        snap = prices_df.iloc[:i]
        dt   = snap.index[-1]
        if not strategy._month_boundary(snap):
            continue  # skip non-month-boundary days up front; cheaper than a full generate_signals call
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
