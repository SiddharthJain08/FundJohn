"""
Inverse Volatility Risk Parity.

Source: https://www.pyquantnews.com/the-pyquant-newsletter/build-your-own-risk-parity-portfolio

Thesis: weighting positions inversely to their realized volatility (equal
risk contribution) produces more stable, better risk-adjusted returns than
cap- or equal-weighted allocation because no single volatile name dominates
portfolio risk. LONG-only, no directional signal on individual names — this
is pure allocation logic, rebalanced monthly.

  w_i = (1 / realized_vol_i) / sum_j(1 / realized_vol_j)

MAX_SIGNALS caps emitted positions to the 50 lowest-vol eligible names
(highest weight under inverse-vol scoring) and renormalizes among them, since
a hard ceiling on signal count is required by the platform contract — the
equal-risk-contribution property is preserved within the capped book.
"""
from __future__ import annotations

import math
import sys
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal

__all__ = ['InverseVolatilityRiskParity']

INSTRUMENT_CLASS = 'equity'

STRATEGY_ID  = 'S_inverse_volatility_risk_parity'
VOL_PERIOD   = 60     # trading days for realized vol estimate
MIN_LOOKBACK = 90     # min price history before computing vol
MIN_ASSETS   = 20     # minimum eligible names to form a risk-parity book
TOTAL_ALLOC  = 0.60   # pre-regime-scale gross allocation across the whole sleeve


class InverseVolatilityRiskParity(BaseStrategy):
    """Inverse-volatility risk parity: weight LONG positions so each name
    contributes roughly equal risk, rebalanced monthly.
    Source: https://www.pyquantnews.com/the-pyquant-newsletter/build-your-own-risk-parity-portfolio
    """

    id                = STRATEGY_ID
    name              = 'Inverse Volatility Risk Parity'
    description       = (
        'LONG-only equal-risk-contribution allocation: weight each name '
        'inversely to its 60-day realized vol, rebalanced monthly.'
    )
    tier              = 2
    signal_frequency  = 'monthly'
    min_lookback      = MIN_LOOKBACK
    # Regime-partitioned backtest (2713 ticker-months, 2017-2025 SP500 panel)
    # shows positive avg-R in LOW_VOL/TRANSITIONING but negative avg-R in
    # HIGH_VOL/CRISIS — expected for a LONG-only, unhedged equity sleeve with
    # no vol-targeting overlay: inverse-vol weighting equalizes *risk
    # contribution* across names, it does not hedge market beta in a broad
    # drawdown. Restricting to the regimes the evidence supports rather than
    # claiming the paper's "more robust in all conditions" framing.
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 50

    def default_parameters(self) -> dict:
        return {
            'vol_period':       VOL_PERIOD,
            'min_assets':       MIN_ASSETS,
            'total_allocation': TOTAL_ALLOC,
        }

    def _is_month_boundary(self, prices: pd.DataFrame) -> bool:
        """True if the last bar is the first trading day of a new month."""
        if not isinstance(prices.index, pd.DatetimeIndex) or len(prices) < 2:
            return True  # can't tell — allow through so backtests work
        return prices.index[-1].month != prices.index[-2].month

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

        # Monthly rebalance only
        if not (self._is_month_boundary(prices) or self.cadence_reset(regime)):
            print('[debug] signals=0', file=sys.stderr)
            return []

        if len(prices) < self.min_lookback:
            print('[debug] signals=0', file=sys.stderr)
            return []

        vol_period  = int(self.parameters.get('vol_period', VOL_PERIOD))
        min_assets  = int(self.parameters.get('min_assets', MIN_ASSETS))
        total_alloc = float(self.parameters.get('total_allocation', TOTAL_ALLOC))

        candidates = [t for t in universe if t in prices.columns] if universe else list(prices.columns)
        if not candidates:
            print('[debug] signals=0', file=sys.stderr)
            return []

        vols: dict = {}
        prices_now: dict = {}
        for ticker in candidates:
            series = prices[ticker].dropna()
            n = min(vol_period, len(series) - 1)
            if n < 20:
                continue
            current_price = float(series.iloc[-1])
            if current_price <= 0:
                continue
            log_rets = (series / series.shift(1)).apply(math.log).dropna().iloc[-n:]
            if log_rets.empty:
                continue
            vol = float(log_rets.std()) * math.sqrt(252)
            if vol <= 0 or not math.isfinite(vol):
                continue
            vols[ticker] = vol
            prices_now[ticker] = current_price

        if len(vols) < min_assets:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Cap to the MAX_SIGNALS lowest-vol names, renormalize within the cap
        # so equal-risk-contribution still holds for the emitted book.
        ranked   = sorted(vols.items(), key=lambda kv: kv[1])[: self.MAX_SIGNALS]
        inv_vols = {t: 1.0 / v for t, v in ranked}
        total_iv = sum(inv_vols.values())
        weights  = {t: iv / total_iv for t, iv in inv_vols.items()}

        scale = self.position_scale(regime_state)

        vol_values = sorted(v for _, v in ranked)
        lo_cut = vol_values[len(vol_values) // 3]
        hi_cut = vol_values[(2 * len(vol_values)) // 3]

        signals: List[Signal] = []
        for ticker, w in weights.items():
            series        = prices[ticker].dropna()
            current_price = prices_now[ticker]
            stops = self.compute_stops_and_targets(
                series, direction='LONG', current_price=current_price, regime_state=regime_state,
            )
            v = vols[ticker]
            confidence = 'HIGH' if v <= lo_cut else ('LOW' if v >= hi_cut else 'MED')
            pos_size = min(round(w * total_alloc * scale, 4), 1.0)

            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = current_price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = pos_size,
                confidence        = confidence,
                signal_params     = {
                    'realized_vol': round(v, 4),
                    'weight':       round(w, 4),
                    'regime':       regime_state,
                    'scale':        round(scale, 4),
                },
                features          = {'realized_vol_60d': round(v, 4)},
            ))

        signals = signals[: self.MAX_SIGNALS]
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


# ---------------------------------------------------------------------------
# Regime-partitioned backtest
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    import json as _json
    import numpy as np
    from backtest.unified_backtest import load_prices_panels, load_regimes

    prices_df, _bars = load_prices_panels()
    reg_series = load_regimes()

    log_rets = (prices_df / prices_df.shift(1)).apply(np.log)
    # Actual last trading day per calendar month (resample('ME').index gives
    # calendar month-end labels, which are not guaranteed to be trading days).
    monthly_idx = pd.Series(prices_df.index, index=prices_df.index).groupby(
        prices_df.index.to_period('M')
    ).last().values
    monthly_idx = pd.DatetimeIndex(monthly_idx)

    rows = []
    for i in range(1, len(monthly_idx) - 1):
        reb_date  = monthly_idx[i]
        next_date = monthly_idx[i + 1]

        window = log_rets.loc[:reb_date].iloc[-VOL_PERIOD:]
        if len(window) < 20:
            continue
        vol = window.std() * math.sqrt(252)
        vol = vol[(vol > 0) & vol.notna()]
        if len(vol) < MIN_ASSETS:
            continue

        ranked   = vol.sort_values().iloc[:50]
        inv_vol  = 1.0 / ranked
        weights  = inv_vol / inv_vol.sum()

        entry_prices = prices_df.loc[reb_date]
        exit_prices  = prices_df.loc[next_date]

        prior_regimes = reg_series[reg_series.index <= reb_date]
        rstate = str(prior_regimes.iloc[-1]) if not prior_regimes.empty else 'LOW_VOL'

        for ticker, w in weights.items():
            ep = entry_prices.get(ticker)
            xp = exit_prices.get(ticker)
            if ep is None or xp is None or pd.isna(ep) or pd.isna(xp) or float(ep) <= 0:
                continue
            pnl = (float(xp) - float(ep)) / float(ep)
            rows.append({
                'strategy_id': STRATEGY_ID, 'signal_date': reb_date, 'regime_state': rstate,
                'pnl': pnl * float(w), 'r_multiple': round(pnl / 0.02, 4),
            })

    trades_df = pd.DataFrame(rows)
    print(f'[backtest] {len(trades_df)} trades', file=sys.stderr)

    from backtest.quick_backtest import run_backtest_with_regime_partition
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(_json.dumps(result, indent=2, default=str))
