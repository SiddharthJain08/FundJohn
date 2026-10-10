"""
Day-After-Big-Move Contrarian Fade.

Source: Krüger, T. (2026). "The Day After a Big US Day: The Rebound After
Down Days Is a Lottery Driven by a Few Panic Days."
https://kruegeralgorithms.com/en/research/day-after-large-us-moves-rebound-lottery

Unambiguous rule from the abstract: when a major US cash index moves at
least 1x ATR(14) in a single session, fade DOWN days only (go LONG the next
session, betting on a rebound); UP days showed no replicable effect
in-sample (t=0.62) or in holdout (t=0.15) and are explicitly excluded.
The authors' own conclusion is that the in-sample down-day edge (+14.8
bps/day, t=2.17) evaporates out-of-sample (t=0.15) and is attributable to a
handful of panic-day outliers rather than a persistent mechanism — this is
a FALSIFICATION study, not a validated anomaly. Implemented anyway so the
promotion gate can confirm/deny on our own data rather than relying on the
paper's verdict.

Interpretation choices (variant 1 of 2 — the abstract gives the trigger
formula and direction unambiguously but leaves ATR construction, position
sizing, and hold horizon open):

  * Index proxy: no cash-index data in the ledger, so DOW/NQ/SPX map to
    their most liquid ETP trackers — DIA/QQQ/SPY — each is its own
    independent trigger (not a single blended index).
  * ATR construction: Wilder-smoothed (EWM, alpha=1/14) absolute daily
    close-to-close change, NOT a flat rolling-mean proxy. "ATR(14)" is
    conventionally the Wilder average; a straight SMA of |diff| (the
    choice a colleague building variant 2 would more likely reach for,
    by analogy to other strategies in this codebase) over-weights the
    oldest bar in the window and under-reacts to a recent volatility
    regime shift — exactly the kind of regime context this trigger needs.
  * Position sizing: magnitude-weighted, NOT flat-percent. The paper's own
    framing — "a lottery driven by a few panic days" — says the (fragile)
    edge, if any, is concentrated in the most extreme moves. Size scales
    with how many ATRs the trigger day's move was (capped), rather than
    giving every trigger the same flat allocation.
  * Hold horizon: short, 3-session informational hold for the backtest
    (the "rebound" is a next-day effect per the abstract; 3 sessions gives
    the bounce a little room without turning this into a multi-week swing
    trade a colleague might default to).
  * Confidence tiers keyed off panic magnitude (ATR multiple), not a fixed
    value — reflects the paper's claim that effect strength (such as it
    is) tracks move severity.

Reported metrics: in-sample (2015-2022, n=241) down-day fade +14.8 bps/day,
t=2.17; holdout (2023-2026, n=110) t=0.15 (null). Overfitting flags:
no_transaction_costs, short_backtest.
"""
from __future__ import annotations

import sys
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal

__all__ = ['DayAfterBigMoveContrarianFade']

INSTRUMENT_CLASS = 'etp'

STRATEGY_ID        = 'S_day_after_big_move_contrarian_fade'
ETP_TICKERS        = ['SPY', 'QQQ', 'DIA']   # proxies for SPX, NQ, DOW
ATR_PERIOD         = 14
MOVE_MULTIPLIER    = 1.0     # trigger: |close_change| >= MOVE_MULTIPLIER * ATR(14)
MAX_HOLD_DAYS      = 3       # informational — backtest exit horizon
BASE_POSITION_PCT  = 0.04    # per-signal floor
MAX_POSITION_PCT   = 0.12    # per-signal cap after magnitude scaling
PANIC_CAP_ATR      = 3.0     # magnitude (in ATR units) at which sizing saturates


def _wilder_atr(series: pd.Series, window: int) -> pd.Series:
    """Wilder-smoothed average of the absolute daily close-to-close change."""
    daily_move = series.diff().abs()
    return daily_move.ewm(alpha=1.0 / window, min_periods=window, adjust=False).mean()


class DayAfterBigMoveContrarianFade(BaseStrategy):
    """Fade a >=1x-ATR(14) down-day on SPY/QQQ/DIA with a next-session LONG,
    sized by panic magnitude. UP-day moves are explicitly excluded — the
    source paper found no replicable effect there in-sample or in holdout.
    """

    id                 = STRATEGY_ID
    name               = 'Day-After-Big-Move Contrarian Fade'
    description        = (
        'LONG-only: fade a >=1x ATR(14) down-day close-to-close move on '
        'SPY/QQQ/DIA with a next-session rebound bet, sized by panic '
        'magnitude (ATR multiple). Up-day moves generate no signal — the '
        'source paper found no replicable effect there.'
    )
    tier               = 3
    signal_frequency   = 'daily'
    min_lookback       = 252
    active_in_regimes  = ['HIGH_VOL', 'TRANSITIONING']
    MAX_SIGNALS        = 3   # one per ETP proxy

    def default_parameters(self) -> dict:
        return {
            'atr_period':      ATR_PERIOD,
            'move_multiplier': MOVE_MULTIPLIER,
            'max_hold_days':   MAX_HOLD_DAYS,
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

        p             = self.parameters
        atr_p         = int(p.get('atr_period', ATR_PERIOD))
        move_mult     = float(p.get('move_multiplier', MOVE_MULTIPLIER))
        tickers       = p.get('tickers', ETP_TICKERS)
        scale         = self.position_scale(regime_state)

        min_bars = atr_p + 5
        signals: List[Signal] = []

        for ticker in tickers:
            if ticker not in prices.columns:
                print(f'[{STRATEGY_ID}] {ticker} not in price panel', file=sys.stderr)
                continue

            series = prices[ticker].dropna()
            if len(series) < min_bars:
                print(f'[{STRATEGY_ID}] {ticker}: insufficient bars {len(series)} < {min_bars}', file=sys.stderr)
                continue

            c0 = float(series.iloc[-1])   # today's close (the candidate "big move" day)
            c1 = float(series.iloc[-2])   # prior session close
            change = c0 - c1

            atr_series = _wilder_atr(series, atr_p)
            atr_val = atr_series.iloc[-1]
            if pd.isna(atr_val) or float(atr_val) <= 0:
                continue
            atr_val = float(atr_val)

            panic_magnitude = abs(change) / atr_val
            triggered = panic_magnitude >= move_mult
            if not triggered:
                continue
            if change >= 0:
                # UP-day move: paper found no replicable effect in-sample or
                # holdout — explicitly no signal.
                continue

            # DOWN-day trigger: fade it, LONG next session.
            stops = self.compute_stops_and_targets(
                series, direction='LONG', current_price=c0, regime_state=regime_state,
            )

            capped_magnitude = min(panic_magnitude, PANIC_CAP_ATR)
            size_frac = BASE_POSITION_PCT + (MAX_POSITION_PCT - BASE_POSITION_PCT) * (
                (capped_magnitude - move_mult) / max(PANIC_CAP_ATR - move_mult, 1e-9)
            )
            pos_size = float(round(max(BASE_POSITION_PCT, min(MAX_POSITION_PCT, size_frac)) * scale, 4))

            if panic_magnitude >= 2.5:
                confidence = 'HIGH'
            elif panic_magnitude >= 1.5:
                confidence = 'MED'
            else:
                confidence = 'LOW'

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
                    'panic_magnitude_atr': round(panic_magnitude, 4),
                    'atr_14':              round(atr_val, 4),
                    'close_t0':            round(c0, 4),
                    'close_t1':            round(c1, 4),
                    'move_multiplier':     move_mult,
                    'regime':              regime_state,
                    'source':              'kruegeralgorithms.com:day-after-large-us-moves-rebound-lottery',
                },
                features          = {'panic_magnitude_atr': round(panic_magnitude, 4), 'atr_14': round(atr_val, 4)},
            ))
            print(
                f'[{STRATEGY_ID}] LONG {ticker} entry={c0:.2f} stop={stops["stop"]:.2f} '
                f'panic_mag={panic_magnitude:.2f}x regime={regime_state}',
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
    atr_full = panel.apply(lambda col: _wilder_atr(col, ATR_PERIOD))

    rows = []
    idx = panel.index
    start = max(ATR_PERIOD + 5, 252)
    for i in range(start, len(idx) - MAX_HOLD_DAYS):
        d  = idx[i]
        d1 = idx[i - 1]
        xd = idx[i + MAX_HOLD_DAYS]

        close_t0 = panel.loc[d]
        close_t1 = panel.loc[d1]
        close_exit = panel.loc[xd]
        atr_t0 = atr_full.loc[d]

        change = close_t0 - close_t1
        valid = close_t0.notna() & close_t1.notna() & close_exit.notna() & atr_t0.notna() & (atr_t0 > 0)
        panic_mag = change.abs() / atr_t0
        down_trigger = valid & (panic_mag >= MOVE_MULTIPLIER) & (change < 0)
        hits = down_trigger[down_trigger].index
        if len(hits) == 0:
            continue

        prior_regimes = reg_series[reg_series.index <= d]
        rstate = str(prior_regimes.iloc[-1]) if not prior_regimes.empty else 'LOW_VOL'

        for ticker in hits:
            ep = float(close_t0[ticker])
            xp = float(close_exit[ticker])
            if ep <= 0:
                continue
            pnl = (xp - ep) / ep
            rows.append({
                'strategy_id': STRATEGY_ID, 'signal_date': d, 'regime_state': rstate,
                'pnl': pnl, 'r_multiple': round(pnl / 0.02, 4),
            })

    trades_df = pd.DataFrame(rows)
    print(f'[backtest] {len(trades_df)} trades', file=sys.stderr)

    from backtest.quick_backtest import run_backtest_with_regime_partition
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(_json.dumps(result, indent=2, default=str))
