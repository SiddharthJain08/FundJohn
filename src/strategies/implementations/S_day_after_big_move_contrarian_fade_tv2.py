"""
Day-After-Big-Move Contrarian Fade (variant 2 of 2).

Source: Krüger, T. (2026). "The Day After a Big US Day: The Rebound After
Down Days Is a Lottery Driven by a Few Panic Days."
https://kruegeralgorithms.com/en/research/day-after-large-us-moves-rebound-lottery

Unambiguous rule from the abstract (unchanged from variant 1): when a major
US cash index moves at least 1x ATR(14) in a single session, fade DOWN days
only (go LONG the next session, betting on a rebound); UP days showed no
replicable effect in-sample (t=0.62) or in holdout (t=0.15) and are
explicitly excluded. This is a FALSIFICATION study (in-sample +14.8 bps/day,
t=2.17, decaying to t=0.15 in holdout) — implemented anyway so the
promotion gate judges it on our own data.

Interpretation choices (variant 2 of 2 — deliberately different from
variant 1 on every axis the abstract leaves open; the trigger direction and
ATR(14)/1x threshold are unambiguous and untouched):

  * Index proxy: a SINGLE blended composite built from SPY/QQQ/DIA
    (equal-weighted average of daily returns), standing in for "a major
    US cash index" read literally as ONE index, not three independent
    triggers. The trigger fires once per day off the composite; when it
    fires, all three ETPs are traded together as one basket (variant 1
    treats each ETP as its own independent trigger — explicitly the
    alternative this variant rejects).
  * ATR construction: a flat rolling SIMPLE MOVING AVERAGE (SMA) of the
    absolute daily change, window=14 — the plain-reading "average true
    range" a colleague would reach for by default, not a Wilder EWM
    smoother.
  * Position sizing: FLAT per-leg percent, not magnitude-weighted. The
    paper's own verdict is that the in-sample edge is illusory noise from
    a handful of panic days; leaning further into position size as a
    function of move severity would double down on exactly the artifact
    the authors warn about. Every trigger gets the same base allocation,
    scaled only by regime.
  * Hold horizon: a strict single-session hold (enter next session's
    open proxy = that session's close per our end-of-day panel, exit the
    session after) — the literal "day after" framing in the title, not
    the multi-day swing variant 1 allows.
  * Confidence: keyed off REGIME STATE, not move magnitude — HIGH_VOL
    gets 'MED' (the regime the paper's panic days cluster in),
    TRANSITIONING gets 'LOW' (edge is murkier when the regime itself is
    unsettled). This reflects uncertainty about the *regime* the
    falsification applies to, rather than the (already-disclaimed) size
    of the triggering move.

Reported metrics: in-sample (2015-2022, n=241) down-day fade +14.8 bps/day,
t=2.17; holdout (2023-2026, n=110) t=0.15 (null). Overfitting flags:
no_transaction_costs, short_backtest.
"""
from __future__ import annotations

import sys
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal

__all__ = ['DayAfterBigMoveContrarianFadeTV2']

INSTRUMENT_CLASS = 'etp'

STRATEGY_ID        = 'S_day_after_big_move_contrarian_fade'
ETP_TICKERS        = ['SPY', 'QQQ', 'DIA']   # composite legs; proxies for SPX, NQ, DOW
ATR_PERIOD         = 14
MOVE_MULTIPLIER    = 1.0     # trigger: |composite_return| >= MOVE_MULTIPLIER * ATR(14)
HOLD_DAYS          = 1       # informational — strict single-session backtest exit
FLAT_POSITION_PCT  = 0.06    # same for every leg, every trigger


def _sma_atr(series: pd.Series, window: int) -> pd.Series:
    """Flat simple-moving-average of the absolute daily close-to-close change."""
    daily_move = series.diff().abs()
    return daily_move.rolling(window=window, min_periods=window).mean()


class DayAfterBigMoveContrarianFadeTV2(BaseStrategy):
    """Fade a >=1x-SMA-ATR(14) down-day on a SPY/QQQ/DIA equal-weighted
    composite with a strict next-session LONG basket, flat-sized. UP-day
    moves are explicitly excluded — the source paper found no replicable
    effect there in-sample or in holdout.
    """

    id                 = STRATEGY_ID
    name               = 'Day-After-Big-Move Contrarian Fade (TV2)'
    description        = (
        'LONG-only: fade a >=1x SMA-ATR(14) down-day move on an equal- '
        'weighted SPY/QQQ/DIA composite with a strict single-session '
        'rebound bet across all three legs, flat-sized. Up-day moves '
        'generate no signal — the source paper found no replicable '
        'effect there.'
    )
    tier               = 3
    signal_frequency   = 'daily'
    min_lookback       = 252
    active_in_regimes  = ['HIGH_VOL', 'TRANSITIONING']
    MAX_SIGNALS        = 3   # one per composite leg

    def default_parameters(self) -> dict:
        return {
            'atr_period':      ATR_PERIOD,
            'move_multiplier': MOVE_MULTIPLIER,
            'hold_days':       HOLD_DAYS,
            'tickers':         ETP_TICKERS,
        }

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

        p         = self.parameters
        atr_p     = int(p.get('atr_period', ATR_PERIOD))
        move_mult = float(p.get('move_multiplier', MOVE_MULTIPLIER))
        tickers   = [t for t in p.get('tickers', ETP_TICKERS) if t in prices.columns]
        scale     = self.position_scale(regime_state)

        min_bars = atr_p + 5
        if len(tickers) < 2:
            print(f'[{STRATEGY_ID}] tv2: fewer than 2 legs available, skip', file=sys.stderr)
            print('[debug] signals=0', file=sys.stderr)
            return []

        panel = prices[tickers].dropna()
        if len(panel) < min_bars:
            print(f'[{STRATEGY_ID}] tv2: insufficient bars {len(panel)} < {min_bars}', file=sys.stderr)
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Single blended composite: equal-weighted average of daily simple
        # returns across the legs, read as ONE "major US index" proxy.
        leg_returns = panel.pct_change()
        composite_ret = leg_returns.mean(axis=1)
        composite_atr = _sma_atr(composite_ret * 100.0, atr_p)  # bps-scale for a stable SMA

        today_ret = float(composite_ret.iloc[-1])
        atr_val = composite_atr.iloc[-1]
        if pd.isna(atr_val) or float(atr_val) <= 0:
            print('[debug] signals=0', file=sys.stderr)
            return []
        atr_val = float(atr_val)

        panic_magnitude = abs(today_ret * 100.0) / atr_val
        triggered = panic_magnitude >= move_mult
        if not triggered or today_ret >= 0:
            # No trigger, or an UP-day move: paper found no replicable
            # effect there in-sample or holdout — explicitly no signal.
            print('[debug] signals=0', file=sys.stderr)
            return []

        confidence = 'MED' if regime_state == 'HIGH_VOL' else 'LOW'
        signals: List[Signal] = []

        for ticker in tickers:
            series = panel[ticker]
            c0 = float(series.iloc[-1])

            stops = self.compute_stops_and_targets(
                series, direction='LONG', current_price=c0, regime_state=regime_state,
            )
            pos_size = float(round(FLAT_POSITION_PCT * scale, 4))

            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = float(round(c0, 4)),
                stop_loss         = float(round(stops['stop'], 4)),
                target_1          = float(round(stops['t1'], 4)),
                target_2          = float(round(stops['t2'], 4)),
                target_3          = float(round(stops['t3'], 4)),
                position_size_pct = pos_size,
                confidence        = confidence,
                signal_params     = {
                    'composite_panic_magnitude_atr': round(panic_magnitude, 4),
                    'composite_return_pct':          round(today_ret * 100.0, 4),
                    'composite_atr_bps':              round(atr_val, 4),
                    'close_t0':                       round(c0, 4),
                    'move_multiplier':                move_mult,
                    'regime':                          regime_state,
                    'variant':                         'tv2_blended_composite_flat_size',
                    'source':                          'kruegeralgorithms.com:day-after-large-us-moves-rebound-lottery',
                },
                features          = {'composite_panic_magnitude_atr': round(panic_magnitude, 4)},
            ))
            print(
                f'[{STRATEGY_ID}] tv2 LONG {ticker} entry={c0:.2f} stop={stops["stop"]:.2f} '
                f'composite_panic_mag={panic_magnitude:.2f}x regime={regime_state}',
                file=sys.stderr,
            )

        n = len(signals)
        print(f'[debug] signals={n}', file=sys.stderr)
        return signals


# ---------------------------------------------------------------------------
# Regime-partitioned backtest
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    import json as _json
    from backtest.unified_backtest import load_prices_panels, load_regimes

    prices_df, _bars = load_prices_panels(tickers=ETP_TICKERS)
    reg_series = load_regimes()

    cols = [t for t in ETP_TICKERS if t in prices_df.columns]
    panel = prices_df[cols]
    leg_returns = panel.pct_change()
    composite_ret = leg_returns.mean(axis=1)
    composite_atr = _sma_atr(composite_ret * 100.0, ATR_PERIOD)

    rows = []
    idx = panel.index
    start = max(ATR_PERIOD + 5, 252)
    for i in range(start, len(idx) - HOLD_DAYS):
        d  = idx[i]
        xd = idx[i + HOLD_DAYS]

        ret_t0 = composite_ret.iloc[i]
        atr_t0 = composite_atr.iloc[i]
        if pd.isna(ret_t0) or pd.isna(atr_t0) or atr_t0 <= 0:
            continue

        panic_mag = abs(ret_t0 * 100.0) / atr_t0
        down_trigger = (panic_mag >= MOVE_MULTIPLIER) and (ret_t0 < 0)
        if not down_trigger:
            continue

        prior_regimes = reg_series[reg_series.index <= d]
        rstate = str(prior_regimes.iloc[-1]) if not prior_regimes.empty else 'LOW_VOL'

        close_t0 = panel.loc[d]
        close_exit = panel.loc[xd]
        for ticker in cols:
            ep = close_t0.get(ticker)
            xp = close_exit.get(ticker)
            if ep is None or xp is None or pd.isna(ep) or pd.isna(xp) or ep <= 0:
                continue
            pnl = (float(xp) - float(ep)) / float(ep)
            rows.append({
                'strategy_id': STRATEGY_ID, 'signal_date': d, 'regime_state': rstate,
                'pnl': pnl, 'r_multiple': round(pnl / 0.02, 4),
            })

    trades_df = pd.DataFrame(rows)
    print(f'[backtest] tv2 {len(trades_df)} trades', file=sys.stderr)

    from backtest.quick_backtest import run_backtest_with_regime_partition
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(_json.dumps(result, indent=2, default=str))
