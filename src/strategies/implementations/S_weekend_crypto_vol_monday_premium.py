"""
Weekend Crypto Volatility and Monday Market Risk — Ebadollahyvahed & Umeorah (2026).
Source: http://arxiv.org/abs/2610.35702v1

Weekend BTC/ETH/XRP realized variance (Friday close -> Monday close — the
only window crypto trades that equities don't) is a common factor with
predictive content for Monday equity variance, with a structural break
around late-2023 (spot-Bitcoin-ETF anticipation). When the weekend
crypto-vol factor is elevated (z-scored vs its own trailing history), the
forecast variance premium is captured by going long volatility on SPY (ATM
straddle) ahead of the Monday session.

Notes: `prices` is a business-day close panel with no separate weekend rows,
so each Monday row's one-bar return vs the prior (Friday) row already IS the
full weekend move. Common factor = cross-asset mean of each coin's weekend
squared return (univariate average, not a fitted covariance factor model).
Post-2024 gate: the paper's effect is only significant after the late-2023
structural break.
"""
from __future__ import annotations

import sys
from typing import List

import pandas as pd

from strategies.base import BaseStrategy, Signal, OptionSpec

__all__ = ['WeekendCryptoVolMondayPremium']

INSTRUMENT_CLASS = 'option'
STRATEGY_ID      = 'S_weekend_crypto_vol_monday_premium'

UNDERLYING     = 'SPY'
CRYPTO_TICKERS = ['BTC-USD', 'ETH-USD', 'XRP-USD']
BREAK_DATE     = pd.Timestamp('2024-01-01')   # paper's reported structural break
Z_WINDOW_MIN   = 52     # min trailing Mondays before a z-score is trusted (~1y)
Z_ENTRY_MIN    = 1.0    # weekend-vol factor z-score gate
DTE_TARGET     = 7      # nearest weekly — the edge is a single Monday session
ROLL_DTE       = 2
BASE_SIZE      = 0.02
MAX_SIZE       = 0.05


def _empty(msg: str) -> List[Signal]:
    print(msg, file=sys.stderr)
    print('[debug] signals=0', file=sys.stderr)
    return []


def _weekend_factor(prices: pd.DataFrame, coins: List[str]) -> pd.Series:
    """Common weekend realized-variance factor: mean of each coin's one-bar
    (Friday->Monday) squared return, restricted to Monday rows."""
    is_monday = prices.index.weekday == 0
    per_coin = [(prices[t].pct_change()[is_monday]) ** 2 for t in coins]
    return pd.concat(per_coin, axis=1).mean(axis=1, skipna=True).dropna()


class WeekendCryptoVolMondayPremium(BaseStrategy):
    id                = STRATEGY_ID
    name              = 'Weekend Crypto Vol -> Monday SPY Vol Premium'
    description       = (
        'BUY_VOL (SPY ATM straddle) ahead of Monday session when the weekend '
        'BTC/ETH/XRP realized-variance factor is elevated vs its trailing '
        'z-score (post-2024 structural break per Ebadollahyvahed & Umeorah 2026).'
    )
    tier              = 2
    signal_frequency  = 'weekly'
    min_lookback      = 504
    instrument_class  = INSTRUMENT_CLASS
    active_in_regimes = ['HIGH_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 1

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty or UNDERLYING not in prices.columns:
            return _empty('[debug] no SPY column')

        regime_state = regime.get('state', 'LOW_VOL') if isinstance(regime, dict) else 'LOW_VOL'
        if not self.should_run(regime_state):
            return _empty('[debug] regime gate')

        today = prices.index[-1]
        if today.weekday() != 0:
            return _empty('[debug] not Monday')
        if today < BREAK_DATE:
            return _empty('[debug] before post-2024 structural break')

        coins = [t for t in CRYPTO_TICKERS if t in prices.columns]
        if not coins:
            return _empty('[debug] no crypto tickers in panel')

        spy = prices[UNDERLYING].dropna()
        if len(spy) < self.min_lookback:
            return _empty(f'[debug] insufficient SPY history: {len(spy)}')

        factor = _weekend_factor(prices, coins)
        if factor.empty or factor.index[-1] != today:
            return _empty('[debug] no weekend factor reading for today')
        if len(factor) < Z_WINDOW_MIN + 1:
            return _empty(f'[debug] insufficient Monday history: {len(factor)}')

        history = factor.iloc[:-1]
        current = float(factor.iloc[-1])
        mu, sigma = float(history.mean()), float(history.std())
        if sigma <= 0:
            return _empty('[debug] zero factor variance')
        z = (current - mu) / sigma
        if z < Z_ENTRY_MIN:
            return _empty(f'[debug] z={z:.3f} < {Z_ENTRY_MIN}')

        current_price = float(spy.iloc[-1])
        if not (current_price == current_price and current_price > 0):
            return _empty('[debug] bad SPY price')
        scale = self.position_scale(regime_state)
        confidence = 'HIGH' if z >= 2.0 else ('MED' if z >= 1.5 else 'LOW')
        size = min(BASE_SIZE * scale * (1.0 + min(z / 2.0, 1.0)), MAX_SIZE)

        stops = self.compute_stops_and_targets(
            spy, 'LONG', current_price, atr_multiplier=2.0, regime_state=regime_state,
        )

        option_spec = OptionSpec(
            underlying     = UNDERLYING,
            right          = 'call',
            strike_rule    = 'atm',
            dte_target     = DTE_TARGET,
            structure      = 'straddle',
            hedge          = 'none',
            roll_dte       = ROLL_DTE,
            hold_to_expiry = False,
        )

        signals = [Signal(
            ticker            = UNDERLYING,
            direction         = 'BUY_VOL',
            entry_price       = round(current_price, 4),
            stop_loss         = stops['stop'],
            target_1          = stops['t1'],
            target_2          = stops['t2'],
            target_3          = stops['t3'],
            position_size_pct = round(size, 4),
            confidence        = confidence,
            signal_params     = {
                'weekend_vol_factor': round(current, 8),
                'z_score':            round(z, 3),
                'coins':              coins,
                'dte_target':         DTE_TARGET,
                'roll_dte':           ROLL_DTE,
            },
            option_spec       = option_spec,
        )]

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]


if __name__ == '__main__':
    import json as _json
    from backtest.unified_backtest import load_prices_panels, load_regimes
    from backtest.quick_backtest import run_backtest_with_regime_partition

    prices_df, _ = load_prices_panels(tickers=[UNDERLYING] + CRYPTO_TICKERS)
    reg_series = load_regimes()
    strat = WeekendCryptoVolMondayPremium()
    spy = prices_df[UNDERLYING].dropna()
    idx = spy.index

    rows = []
    i = strat.min_lookback
    while i < len(idx) - 1:
        d = idx[i]
        prior_regimes = reg_series[reg_series.index <= d]
        rstate = str(prior_regimes.iloc[-1]) if not prior_regimes.empty else 'LOW_VOL'
        sigs = strat.generate_signals(prices_df.iloc[: i + 1], {'state': rstate}, [UNDERLYING])
        if not sigs:
            i += 1
            continue
        entry_price = float(spy.loc[d])
        j = i + 1   # single Monday-session hold
        exit_price = float(spy.loc[idx[j]])
        realized_move = abs(exit_price - entry_price) / entry_price
        trailing_vol = spy.pct_change().iloc[max(0, i - 20): i].std() or 0.0
        pnl = realized_move - trailing_vol   # BUY_VOL profits when realized > priced baseline
        rows.append({'strategy_id': STRATEGY_ID, 'signal_date': d, 'regime_state': rstate,
                     'pnl': pnl, 'r_multiple': round(pnl / 0.02, 4)})
        i = j + 1

    trades_df = pd.DataFrame(rows)
    print(f'[backtest] {len(trades_df)} trades', file=sys.stderr)
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(_json.dumps(result, indent=2, default=str))
