"""
Return-Stacking Gold + Managed-Futures Overlay — layer leveraged diversifier
ETPs on top of an existing equity allocation without selling equity or
opening a margin/futures account.

Source: BestFolio (2026), "How to Add Gold and Managed Futures Without
Giving Up Your Equity Upside" (SmartStack(TM); referencing a Quantica
Capital paper, March 2026).

Hypothesis: a 100% TAA-equity book forgoes gold's and managed-futures'
uncorrelated diversification beta because adding a traditional (1x)
allocation to either would mean SELLING equity notional to fund it. A
LEVERAGED ETP (one that gets >1x or embedded-futures notional exposure per
dollar of NAV, funded internally by the fund rather than by the investor's
own margin account) lets a FRACTION of the book buy a full diversifier
slug on top of the existing 100% equity allocation — "capital-stacking"
instead of "capital-reallocating." This strategy is only the diversifier
side of that stack; it emits LONG, buy-and-hold-with-periodic-rebalance
signals on the two legs and relies on the rest of the book (other
strategies) to supply the equity leg — it never sells or sizes down
equity itself.

Variant 1/2 — only an abstract is available (no appendix with tickers,
weights or a cadence), so every numeric/ticker choice below is this
implementation's defensible reading, deliberately different from a
colleague's plausible reading:
  - leveraged gold leg = UGL (ProShares Ultra Gold, 2x daily gold futures
    exposure) — a colleague could as defensibly pick DGP (DB 2x gold) or
    plain GLD at a larger fractional weight; UGL is chosen because its 2x
    embedded leverage most literally matches "leveraged_gold_etf" and it
    has the deepest, longest (2016-) history of the 2x gold names.
  - leveraged managed-futures leg = DBMF (iMGP DBi Managed Futures
    Strategy ETF) — a colleague could instead pick KMLM or CTA (Simplify).
    DBMF is chosen because it replicates full-notional CTA/trend-following
    futures returns using only a fraction of NAV as margin internally (the
    rest sits in T-bills), which is exactly the "embedded leverage, no
    incremental cash outlay, no investor futures account" mechanism the
    source describes — and it has the longest history (2019-05-) of the
    three candidates.
  - fractional weights: 12% of book notional to EACH leg (24% combined),
    rebalanced back to target MONTHLY (first trading day of each calendar
    month) — a colleague could just as defensibly choose a smaller/larger
    split or a slower (quarterly/annual) cadence; this variant treats
    "stacking" as a meaningfully-sized (not token) overlay rebalanced
    often enough to keep the two legs near their target weight without
    turning this into a timing signal (direction_vocab is LONG-only — the
    monthly cadence re-strikes size, it never decides whether to hold).
The paper's unambiguous spec items are kept as-is: direction is LONG-only
(no SHORT/timing logic — this is a static diversification overlay, not a
momentum/rotation call), min_lookback_required=756 trading days (~3y),
instrument_class='etp'. Data: close-price panel only (engine-supplied
`prices`) — no financials/options/macro needed to size a fixed two-ticker
overlay.
"""
from __future__ import annotations

import sys
from typing import List

import pandas as pd

from strategies.base import BaseStrategy, Signal

__all__ = ['ReturnStackingGoldManagedFutures']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID      = 'S_return_stacking_gold_managed_futures'

GOLD_TICKER = 'UGL'    # ProShares Ultra Gold, 2x leveraged gold futures exposure
MF_TICKER   = 'DBMF'   # iMGP DBi Managed Futures Strategy ETF, embedded-leverage CTA replication


class ReturnStackingGoldManagedFutures(BaseStrategy):
    """Static LONG overlay: UGL (2x gold) + DBMF (managed futures) at a
    fixed fractional weight each, rebalanced monthly, stacked on top of
    whatever equity allocation the rest of the book already holds."""

    id                = STRATEGY_ID
    name              = 'Return-Stacking Gold + Managed-Futures Overlay'
    description       = ('LONG-only diversifier overlay: UGL (2x gold) + DBMF (managed futures), '
                         '12% each, rebalanced monthly on top of existing equity exposure; no '
                         'margin/futures account needed since the leverage is embedded in the ETPs.')
    tier              = 3
    signal_frequency  = 'monthly'
    min_lookback      = 756          # paper's min_lookback_required (3y of daily obs)
    instrument_class  = INSTRUMENT_CLASS
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']   # all-weather diversifier
    MAX_SIGNALS       = 2

    WEIGHT_GOLD = 0.12
    WEIGHT_MF   = 0.12
    HOLD_DAYS   = 21   # ~1 trading month, matches the rebalance cadence

    def default_parameters(self) -> dict:
        return {
            'weight_gold': self.WEIGHT_GOLD,
            'weight_mf':   self.WEIGHT_MF,
        }

    def _month_boundary(self, prices: pd.DataFrame) -> bool:
        """True on the first trading day of a new calendar month (monthly rebalance gate)."""
        if len(prices) < 2:
            return False
        d1 = pd.Timestamp(prices.index[-1])
        d0 = pd.Timestamp(prices.index[-2])
        return d1.month != d0.month

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

        legs = (
            (GOLD_TICKER, self.parameters.get('weight_gold', self.WEIGHT_GOLD), 'gold'),
            (MF_TICKER,   self.parameters.get('weight_mf',   self.WEIGHT_MF),   'managed_futures'),
        )

        scale = self.position_scale(regime_state)
        signals: List[Signal] = []
        for ticker, base_weight, leg_name in legs:
            if ticker not in prices.columns:
                continue
            series = prices[ticker].dropna()
            if len(series) < 14:
                continue
            price = float(series.iloc[-1])
            if not (price == price and price > 0):
                continue
            stops = self.compute_stops_and_targets(series, 'LONG', price, regime_state=regime_state)
            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=price,
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=round(base_weight * scale, 6),
                confidence='MED',
                signal_params={
                    'hold_days':   self.HOLD_DAYS,
                    'regime':      regime_state,
                    'leg':         leg_name,
                    'base_weight': base_weight,
                },
            ))
            if len(signals) >= self.MAX_SIGNALS:
                break

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

    strategy  = ReturnStackingGoldManagedFutures()
    universe  = list(prices_df.columns)
    hold_days = strategy.HOLD_DAYS

    rows = []
    for i in range(strategy.min_lookback, len(prices_df) - hold_days):
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
            ret  = (float(exit_px) - s.entry_price) / s.entry_price
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
