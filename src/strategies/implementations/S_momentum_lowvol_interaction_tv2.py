"""S_momentum_lowvol_interaction_tv2 — smooth momentum x low-vol rank interaction.

Source: Wang, Rachev & Fabozzi (2026), "The Logic of Factor Portfolios: Boolean
Rules, Interactions, and Trading Costs" (arXiv:2611.09834v1).

Hypothesis (unchanged from variant 1, and pinned by the paper's own
pseudocode): a smooth (product-of-ranks) interaction of momentum and low-vol
screens, not an independent-threshold ("Boolean AND") intersection, LONG the
top decile of
  rank_mom    = cross_sectional_rank(momentum_12m_skip_1m)
  rank_lowvol = cross_sectional_rank(-realized_vol_12m)
  interaction = rank_mom * rank_lowvol

Interpretation variant 2 of 2. The same three details variant 1 flagged as
genuinely underspecified are resolved the OPPOSITE way here:

  - Position sizing: variant 1 sized each name proportional to its own
    interaction score. That is a defensible reading of "size by interaction
    weight," but it is also just a re-normalization of the same selection
    signal that already determined decile membership — it does not add new
    information, only tilts within a set that is already homogeneous by
    construction (all members are in the top 10% of both ranks). This variant
    takes the plainer reading explicitly flagged as the alternative in
    variant 1's own docstring: FLAT EQUAL WEIGHT across the decile. Once a
    name has cleared the smooth-interaction screen, there is no principled
    live-computable Walsh coefficient to differentiate it further from its
    decile peers, so equal weight is the more honest expression of "we trust
    the screen, not a within-decile ranking of it."
  - Regime set: variant 1 reasoned the low-vol half of the interaction is a
    de-leveraging filter that buys momentum one band of protection into
    HIGH_VOL. This variant takes the opposite, more conservative read: the
    momentum-crash literature (Daniel & Moskowitz) shows the worst momentum
    crashes occur in the RECOVERY out of HIGH_VOL/CRISIS, i.e. precisely when
    a name's trailing 12m realized vol is still stale-elevated from the prior
    stress episode even as price has already rebounded — the low-vol half of
    THIS interaction is measured on the same lagging 12m window and so is
    least reliable exactly when HIGH_VOL has just been entered or exited.
    Active only in LOW_VOL and TRANSITIONING; HIGH_VOL and CRISIS both
    excluded.
  - Rebalance cadence: variant 1 explicitly rejected "a rolling N-trading-day
    clock" in favor of calendar-month-boundary reformation. This variant
    takes that rejected alternative: a rolling, calendar-agnostic cadence —
    every REBAL_EVERY (21) trading days counted from the start of the
    available price history, not anchored to month-end. The paper's
    persistence/turnover predictions are stated in trading-day half-life
    units, not calendar months, so a trading-day clock is the more literal
    match to the paper's own unit of measurement, and it avoids any
    turn-of-month seasonal contamination that a strict month-boundary
    reformation would inherit from the backtest's particular start date.

The formation window (12m momentum, skip 1m) and the vol window (12m
realized vol, uniform rolling std of daily log returns, annualized) are
pinned by the paper's own naming and are NOT re-interpreted here, matching
variant 1.
"""
from __future__ import annotations
import sys
import math
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal
from src.strategies.universe_default import sp500 as universe_filter

__all__ = ['MomentumLowVolInteractionTv2']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID = 'S_momentum_lowvol_interaction_tv2'

MOM_LOOKBACK   = 252   # 12 months, trading days
MOM_SKIP       = 21    # skip most recent month (reversal avoidance)
VOL_WINDOW     = 252   # 12-month realized-vol window
DECILE_FRAC    = 0.10  # top_decile(interaction_score)
MIN_UNIVERSE   = 100   # spec minimum_universe_size
TOTAL_BOOK     = 0.30  # aggregate long gross exposure before regime scaling
MAX_POS_SIZE   = 0.05  # per-name cap
REBAL_EVERY    = 21    # rolling trading-day rebalance clock (variant 2)


class MomentumLowVolInteractionTv2(BaseStrategy):
    """Smooth product-of-ranks momentum x low-vol interaction, LONG top decile,
    equal-weighted, rolling 21-trading-day rebalance (variant 2 of 2)."""

    id                = STRATEGY_ID
    name              = 'MomentumLowVolInteractionTv2'
    description       = (
        'Smooth (product-of-ranks) momentum x low-vol interaction — LONG top '
        'decile, equal-weighted, rolling 21-trading-day rebalance clock '
        '(Wang, Rachev & Fabozzi 2026, variant 2 of 2)'
    )
    tier              = 2
    signal_frequency  = 'periodic'
    min_lookback      = 504
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

    def default_parameters(self) -> dict:
        return {
            'mom_lookback': MOM_LOOKBACK,
            'mom_skip':     MOM_SKIP,
            'vol_window':   VOL_WINDOW,
            'decile_frac':  DECILE_FRAC,
            'min_universe': MIN_UNIVERSE,
            'total_book':   TOTAL_BOOK,
            'rebal_every':  REBAL_EVERY,
        }

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print('[debug] signals=0 (no price data)', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL') if isinstance(regime, dict) else 'LOW_VOL'
        if not self.should_run(regime_state):
            print('[debug] signals=0 (regime gate)', file=sys.stderr)
            return []

        p = self.parameters
        lookback = int(p.get('mom_lookback', MOM_LOOKBACK))
        skip     = int(p.get('mom_skip', MOM_SKIP))
        vwin     = int(p.get('vol_window', VOL_WINDOW))
        decile   = float(p.get('decile_frac', DECILE_FRAC))
        min_univ = int(p.get('min_universe', MIN_UNIVERSE))
        rebal_n  = int(p.get('rebal_every', REBAL_EVERY))

        if len(prices) < max(lookback + skip, vwin) + 5:
            print('[debug] signals=0 (insufficient history)', file=sys.stderr)
            return []

        # Rolling trading-day rebalance clock (variant 2): fire every
        # rebal_n bars counted from the start of available history, not on
        # calendar-month boundaries. Always allow a regime-flip to re-mint.
        if (len(prices) % rebal_n != 0) and not self.cadence_reset(regime):
            print('[debug] signals=0 (not the rolling rebalance bar)', file=sys.stderr)
            return []

        available = [t for t in universe if t in prices.columns]
        if len(available) < min_univ:
            print(f'[debug] signals=0 (universe too small: {len(available)})', file=sys.stderr)
            return []

        prices_u = prices[available]

        # Momentum: 12m-skip-1m return, evaluated as of the last row.
        p_end   = prices_u.iloc[-(skip + 1)]
        p_start = prices_u.iloc[-(skip + lookback)].replace(0, float('nan'))
        momentum = ((p_end - p_start) / p_start).dropna()

        # Realized vol: uniform rolling std of daily returns over the trailing
        # window, annualized (skipna so sparse-history names aren't penalized
        # by NaN gaps rather than true volatility).
        rets = prices_u[momentum.index].pct_change()
        vol = (rets.iloc[-vwin:].std() * math.sqrt(252)).dropna()
        vol = vol[vol > 0]

        common = momentum.index.intersection(vol.index)
        if len(common) < min_univ:
            print(f'[debug] signals=0 (common universe too small: {len(common)})', file=sys.stderr)
            return []

        mom_c = momentum.loc[common]
        vol_c = vol.loc[common]

        rank_mom    = mom_c.rank(pct=True)
        rank_lowvol = (-vol_c).rank(pct=True)
        interaction = rank_mom * rank_lowvol
        interaction_rank = interaction.rank(pct=True)

        thresh = interaction.quantile(1.0 - decile)
        selected = interaction[interaction >= thresh].sort_values(ascending=False)
        selected = selected.iloc[: self.MAX_SIGNALS]

        if selected.empty:
            print('[debug] signals=0 (empty decile)', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)
        n_selected = len(selected)
        current_prices = prices_u.iloc[-1]

        signals: List[Signal] = []
        for ticker, score in selected.items():
            raw_price = current_prices.get(ticker)
            if raw_price is None or raw_price != raw_price or raw_price <= 0:
                continue
            price = float(raw_price)
            ts = prices_u[ticker].dropna()
            if len(ts) < 20:
                continue
            stops = self.compute_stops_and_targets(ts, 'LONG', price, regime_state=regime_state)

            # Equal weight across the decile (variant 2) — the screen already
            # did the differentiation; no within-decile tilt.
            size = round(min((TOTAL_BOOK * scale) / n_selected, MAX_POS_SIZE), 4)
            if size <= 0:
                continue

            irank = float(interaction_rank.get(ticker, 0.5))
            confidence = 'HIGH' if irank >= 0.90 else ('MED' if irank >= 0.65 else 'LOW')

            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = size,
                confidence        = confidence,
                signal_params     = {
                    'momentum_12m_skip_1m': round(float(mom_c[ticker]), 4),
                    'realized_vol_12m':     round(float(vol_c[ticker]), 4),
                    'rank_mom':             round(float(rank_mom[ticker]), 4),
                    'rank_lowvol':          round(float(rank_lowvol[ticker]), 4),
                    'interaction_score':    round(float(score), 4),
                    'interaction_rank':     round(irank, 4),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


# ── Regime-partitioned backtest ───────────────────────────────────────────
if __name__ == '__main__':
    import os
    import json as _json

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

    idx = prices_df.index
    universe_all = list(prices_df.columns)
    strat = MomentumLowVolInteractionTv2()

    reb_idxs = [i for i in range(strat.min_lookback, len(idx)) if i % REBAL_EVERY == 0]

    rows = []
    for k in range(len(reb_idxs) - 1):
        i, j = reb_idxs[k], reb_idxs[k + 1]
        d = idx[i]
        prices_upto = prices_df.iloc[: i + 1]
        regime_dict = {'state': _regime_on(d)}

        sigs = strat.generate_signals(prices_upto, regime_dict, universe_all, aux_data={})
        if not sigs:
            continue

        d2 = idx[j]
        weights = {s.ticker: s.position_size_pct for s in sigs}
        wsum = sum(weights.values())
        if wsum <= 0:
            continue

        pnl = 0.0
        for ticker, w in weights.items():
            if ticker not in prices_df.columns:
                continue
            p0, p1 = prices_df.at[d, ticker], prices_df.at[d2, ticker]
            if pd.isna(p0) or pd.isna(p1) or p0 <= 0:
                continue
            pnl += (w / wsum) * ((float(p1) - float(p0)) / float(p0))

        rows.append({
            'strategy_id': STRATEGY_ID, 'signal_date': str(d.date()),
            'regime_state': regime_dict['state'], 'pnl': pnl, 'r_multiple': round(pnl / 0.05, 4),
        })

    trades_df = pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=['strategy_id', 'signal_date', 'regime_state', 'pnl', 'r_multiple'])
    print(f'[backtest] completed_trades={len(trades_df)}', file=sys.stderr)

    from backtest.quick_backtest import run_backtest_with_regime_partition
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(_json.dumps(result, indent=2, default=str))
