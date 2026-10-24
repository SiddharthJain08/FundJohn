"""
GEM Strategy: Antonacci's Dual Momentum Rules

Source: https://bestfolio.app/blog/strategy-spotlight-gem (2026), "GEM Strategy:
Antonacci's Dual Momentum Rules" — a blog replication of Gary Antonacci's
Global Equities Momentum (GEM) with parameter stress-tests and a same-dates
comparison against ADM/CDM variants (not a primary academic contribution).

Hypothesis: combining relative momentum (US vs international equities) with
absolute momentum (winner vs the risk-free rate) each month captures the
momentum premium while the absolute-momentum filter rotates into bonds to
avoid the worst equity drawdowns.

Rule (monthly):
  r_us   = 12-month total return of SPY
  r_intl = 12-month total return of ACWX (falls back to VEU if ACWX has
           insufficient history on a given date)
  winner = SPY if r_us > r_intl else the international leg
  r_rf   = trailing 3-month T-bill yield (DTB3, macro.parquet) as a decimal
           annualized rate, used as the absolute-momentum hurdle
  IF return(winner) > r_rf: LONG winner ELSE: LONG AGG (bonds)

This strategy ignores the `universe` argument — GEM's universe is the fixed
4-ticker set {SPY, ACWX, VEU, AGG}, not a cross-sectional equity screen
(precedent: S_btc_gold_dual_momentum_rotation).
"""
from __future__ import annotations
import sys
from typing import List

import pandas as pd

from strategies.base import BaseStrategy, Signal

__all__ = ['GemDualMomentumRotation']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID = 'S_gem_dual_momentum_rotation'

US_TICKER       = 'SPY'
INTL_TICKER     = 'ACWX'
INTL_FALLBACK   = 'VEU'
BOND_TICKER     = 'AGG'
RF_SERIES       = 'DTB3'          # 3-month T-bill yield, annualized %
MOM_MONTHS      = 12              # 12-1 month-end dual momentum
BASE_SIZE       = 0.90            # single-leg allocation before regime scale


class GemDualMomentumRotation(BaseStrategy):
    """Antonacci GEM: relative momentum (SPY vs intl) + absolute momentum
    (winner vs T-bill rate) rotates into the winning equity leg or AGG."""

    id                = STRATEGY_ID
    name              = 'GEM Dual Momentum Rotation'
    description       = ('Monthly relative momentum (SPY vs ACWX/VEU) picks the stronger equity '
                          'leg; absolute momentum vs the T-bill rate rotates into AGG when the '
                          'winner trails cash.')
    tier              = 2
    signal_frequency  = 'monthly'
    min_lookback       = 504   # ~2 years daily, per spec min_lookback_required
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    def default_parameters(self) -> dict:
        return {
            'mom_months': MOM_MONTHS,
            'base_size':  BASE_SIZE,
        }

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty or US_TICKER not in prices.columns:
            print('[debug] signals=0 (no SPY column)', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL') if isinstance(regime, dict) else 'LOW_VOL'
        if not self.should_run(regime_state):
            print('[debug] signals=0 (regime gate)', file=sys.stderr)
            return []

        if len(prices) < self.min_lookback:
            print(f'[debug] signals=0 (insufficient history: {len(prices)})', file=sys.stderr)
            return []

        p = self.parameters
        mom_months = int(p.get('mom_months', MOM_MONTHS))

        intl_ticker = INTL_TICKER if INTL_TICKER in prices.columns else (
            INTL_FALLBACK if INTL_FALLBACK in prices.columns else None)
        if intl_ticker is None or BOND_TICKER not in prices.columns:
            print('[debug] signals=0 (missing intl/bond leg)', file=sys.stderr)
            return []

        legs = [US_TICKER, intl_ticker, BOND_TICKER]
        monthly = prices[legs].resample('ME').last()
        min_obs = mom_months + 1
        if len(monthly) < min_obs:
            print(f'[debug] signals=0 (monthly obs {len(monthly)} < {min_obs})', file=sys.stderr)
            return []

        r_us   = self._trailing_return(monthly[US_TICKER], mom_months)
        r_intl = self._trailing_return(monthly[intl_ticker], mom_months)
        if r_us is None or r_intl is None:
            print('[debug] signals=0 (insufficient leg history)', file=sys.stderr)
            return []

        winner_ticker = US_TICKER if r_us > r_intl else intl_ticker
        winner_mom    = r_us if r_us > r_intl else r_intl

        r_rf = self._risk_free_rate(aux_data, prices.index)

        chosen_ticker = winner_ticker if winner_mom > r_rf else BOND_TICKER

        price_series = prices[chosen_ticker].dropna()
        if price_series.empty:
            print('[debug] signals=0 (chosen leg has no price history)', file=sys.stderr)
            return []
        current_price = float(price_series.iloc[-1])
        if not (current_price == current_price and current_price > 0):
            print('[debug] signals=0 (bad last price)', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)
        base_size = float(p.get('base_size', BASE_SIZE))
        position_size = round(base_size * scale, 4)
        if position_size < 0.01:
            print('[debug] signals=0 (position size below floor)', file=sys.stderr)
            return []

        edge = winner_mom - r_rf
        if chosen_ticker == BOND_TICKER:
            confidence = 'HIGH' if edge < -0.05 else 'MED'
        else:
            confidence = 'HIGH' if edge > 0.10 else ('MED' if edge > 0.03 else 'LOW')

        stops = self.compute_stops_and_targets(
            price_series, 'LONG', current_price, regime_state=regime_state
        )

        signals = [Signal(
            ticker            = chosen_ticker,
            direction         = 'LONG',
            entry_price       = current_price,
            stop_loss         = float(stops['stop']),
            target_1          = float(stops['t1']),
            target_2          = float(stops['t2']),
            target_3          = float(stops['t3']),
            position_size_pct = position_size,
            confidence        = confidence,
            signal_params     = {
                'r_us':          round(float(r_us), 6),
                'r_intl':        round(float(r_intl), 6),
                'intl_ticker':   intl_ticker,
                'r_rf':          round(float(r_rf), 6),
                'winner_ticker': winner_ticker,
                'rebalance':     'monthly',
            },
        )]
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _trailing_return(monthly_series: pd.Series, months: int) -> 'float | None':
        """12-month total return off month-end closes. None if insufficient history."""
        s = monthly_series.dropna()
        if len(s) < months + 1:
            return None
        start_val = float(s.iloc[-1 - months])
        end_val   = float(s.iloc[-1])
        if start_val <= 0:
            return None
        return end_val / start_val - 1.0

    @staticmethod
    def _risk_free_rate(aux_data: dict, date_index: pd.DatetimeIndex) -> float:
        """Trailing DTB3 (3-month T-bill, annualized %) as a decimal rate, aligned
        to the last available trading day. 0.0 fallback (no risk-free hurdle)
        when macro is unavailable rather than silently going flat."""
        macro = (aux_data or {}).get('macro') or {}
        s = macro.get('DTB3')
        if not isinstance(s, pd.Series) or s.empty:
            return 0.0
        s = s.dropna().sort_index()
        if s.empty:
            return 0.0
        aligned = s.reindex(pd.DatetimeIndex(date_index), method='ffill')
        last = aligned.iloc[-1]
        if pd.isna(last):
            last = s.iloc[-1]
        return float(last) / 100.0


# ---------------------------------------------------------------------------
# Regime-partitioned backtest
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    import json as _json
    from backtest.unified_backtest import load_prices_panels, load_regimes
    from strategies.aux_data_loader import load_aux_data

    legs = [US_TICKER, INTL_TICKER, INTL_FALLBACK, BOND_TICKER]
    prices_df, _bars = load_prices_panels(tickers=legs)
    reg_series = load_regimes()

    strat = GemDualMomentumRotation()
    idx = prices_df.index
    start = max(strat.min_lookback, 400)
    HOLD_DAYS = 21  # ~1 month forward

    rows = []
    last_month = None
    for i in range(start, len(idx) - HOLD_DAYS):
        d = idx[i]
        month_key = (d.year, d.month)
        if month_key == last_month:
            continue  # one decision per calendar month, like the live cadence
        last_month = month_key

        xd = idx[i + HOLD_DAYS]
        prices_upto = prices_df.iloc[: i + 1]
        aux = load_aux_data(d.strftime('%Y-%m-%d'))

        prior_regimes = reg_series[reg_series.index <= d]
        rstate = str(prior_regimes.iloc[-1]) if not prior_regimes.empty else 'LOW_VOL'
        regime_dict = {'state': rstate}

        sigs = strat.generate_signals(prices_upto, regime_dict, legs, aux_data=aux)
        if not sigs:
            continue
        sig = sigs[0]

        leg = prices_df[sig.ticker].dropna()
        if d not in leg.index or xd not in leg.index:
            continue
        c0 = float(leg.loc[d])
        cx = float(leg.loc[xd])
        if c0 <= 0:
            continue
        pnl = (cx - c0) / c0
        stop_dist = max(abs(c0 - sig.stop_loss), 1e-6)

        rows.append({
            'strategy_id': STRATEGY_ID, 'signal_date': d, 'regime_state': rstate,
            'pnl': pnl, 'r_multiple': round(pnl * c0 / stop_dist, 4),
        })

    trades_df = pd.DataFrame(rows)
    print(f'[backtest] {len(trades_df)} trades', file=sys.stderr)

    from backtest.quick_backtest import run_backtest_with_regime_partition
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(_json.dumps(result, indent=2, default=str))
