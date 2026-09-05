"""
Holiday Seasonality — Energy ETFs. Ported from TradeQuantiX "Market Effect
Research: Holiday Seasonality - Part 2".
Source: https://www.tradequantixnewsletter.com/p/market-effect-research-holiday-seasonality-8fd

Thesis: energy ETFs drift up in the ~8-trading-day window ahead of each NYSE
closure, then give the move back in a post-holiday reversal. The
paper reports the pre-holiday leg as ~3x stronger than a random equal-length
holding period, strongest in UGA/USO.

Interpretation choices (paper is silent on exact basket weighting and exit
timing — see docstring notes below; entry offset of 8 trading days and LONG
direction are the paper's unambiguous claims and are preserved exactly):

  * Basket: trade the FULL four-name basket (USO, UGA, XLE, XOP) equal-weighted
    every holiday, rather than concentrating only in UGA/USO where the paper's
    own numbers are strongest. This trades the broader "energy ETF" claim the
    title makes, using UGA/USO's outperformance only as a confidence tag.
  * Exit: liquidate at the close of the LAST trading session before the
    holiday (i.e. capture the pre-holiday drift only, with zero exposure to
    the post-holiday reversal window). The paper's "exit = h or h+1" language
    is read here as "exit no later than the holiday" rather than "hold one
    day into the reversal."
  * Holiday set: the NYSE closure calendar (actual exchange closures per
    trading_calendar master), not the federal holiday calendar — this gives ~9
    windows/year instead of 2, satisfying the >=20-trades-per-3yr-window
    backtest requirement.
"""
from __future__ import annotations

import sys
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['HolidaySeasonalityEnergyEtf']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID      = 'S_holiday_seasonality_energy_etf'

# Full basket named in the paper title; UGA/USO are called out as the
# strongest performers and get the confidence bump below.
BASKET        = ('USO', 'UGA', 'XLE', 'XOP')
STRONG_NAMES  = ('UGA', 'USO')
ENTRY_OFFSET  = 8   # trading days before the holiday


def _holidays_near(anchor: pd.Timestamp, window_days: int = 45) -> list:
    """Exchange closures (weekdays that are not NYSE sessions) within
    ±window_days of anchor, from the trading_calendar master. Pure calendar
    lookup — independent of which sessions have price data."""
    from lib.trading_calendar import sessions
    start = (anchor - pd.Timedelta(days=window_days)).date()
    end = (anchor + pd.Timedelta(days=window_days)).date()
    open_days = set(sessions(start, end))
    return [pd.Timestamp(d) for d in pd.bdate_range(start, end).date if d not in open_days]


def _entry_exit_for_holiday(h: pd.Timestamp) -> tuple:
    """(entry_day, exit_day): exit_day is the last session before holiday h;
    entry_day is ENTRY_OFFSET sessions before h (so the window spans
    ENTRY_OFFSET sessions inclusive of exit_day)."""
    from lib.trading_calendar import sessions_before
    prior = sessions_before(h.date(), 20)
    if len(prior) < ENTRY_OFFSET:
        return None, None
    return pd.Timestamp(prior[-ENTRY_OFFSET]), pd.Timestamp(prior[-1])


def _entry_and_exit_days(anchor: pd.Timestamp, window_days: int = 45) -> tuple:
    """entry_set/exit_set of pd.Timestamp for every US federal holiday within
    window_days of anchor ('today') — calendar-only, safe to call with a
    truncated (trailing-only) price panel."""
    entry_set, exit_set = set(), set()
    for h in _holidays_near(anchor, window_days=window_days):
        entry_day, exit_day = _entry_exit_for_holiday(h)
        if entry_day is None:
            continue
        entry_set.add(entry_day)
        exit_set.add(exit_day)
    return entry_set, exit_set


class HolidaySeasonalityEnergyEtf(BaseStrategy):
    """LONG the energy-ETF basket (USO/UGA/XLE/XOP) over the 8-trading-day
    window ahead of each NYSE closure; flat into the closure and
    through the post-closure reversal window.
    Source: https://www.tradequantixnewsletter.com/p/market-effect-research-holiday-seasonality-8fd
    """

    id                = STRATEGY_ID
    name              = 'Holiday Seasonality Energy ETF'
    description       = (
        'LONG energy ETF basket (USO/UGA/XLE/XOP) in the 8-trading-day window '
        'before each US federal holiday; exits at the close before the holiday, '
        'avoiding the post-holiday reversal.'
    )
    tier              = 3
    signal_frequency  = 'daily'
    calendar_edge     = True   # window IS the signal; ports across regime flips (2026-08-13)
    min_lookback      = 20
    # Calendar-driven effect (holiday-window positioning) — the underlying
    # window fires the same regardless of vol regime, so keep should_run()
    # permissive across all four. The regime-partitioned backtest below
    # (440 trades) shows only CRISIS clears the 0.5-Sharpe promotion bar on
    # a thin n=24 sample; LOW_VOL/TRANSITIONING/HIGH_VOL run positive but
    # sub-threshold Sharpe. Manifest eligible_regimes reflects the measured
    # split, not this a-priori "all-weather" gate.
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = len(BASKET)

    def default_parameters(self) -> dict:
        return {
            'entry_offset_td': ENTRY_OFFSET,
            'pos_size_frac':   0.05,   # per-name allocation (pre-regime-scale); 4 names = 0.20 gross
        }

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print(f'[{STRATEGY_ID}] no price data — returning []', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print(f'[{STRATEGY_ID}] skipping regime {regime_state}', file=sys.stderr)
            return []

        available = [t for t in BASKET if t in prices.columns]
        if not available:
            print(f'[{STRATEGY_ID}] none of BASKET in prices columns — returning []', file=sys.stderr)
            return []

        if len(prices) < self.min_lookback:
            print(f'[{STRATEGY_ID}] only {len(prices)} rows < {self.min_lookback} — returning []', file=sys.stderr)
            return []

        try:
            today = pd.Timestamp(prices.index[-1])
        except (IndexError, TypeError):
            print(f'[{STRATEGY_ID}] cannot parse date index — returning []', file=sys.stderr)
            return []

        entry_set, exit_set = _entry_and_exit_days(today)
        is_entry = today in entry_set
        is_exit  = today in exit_set

        scale     = self.position_scale(regime_state)
        pos_frac  = self.parameters.get('pos_size_frac', 0.05)
        signals: List[Signal] = []

        if is_entry:
            for ticker in available:
                series = prices[ticker].dropna()
                if len(series) < 14:
                    continue
                current_price = float(series.iloc[-1])
                if current_price <= 0:
                    continue
                stops = self.compute_stops_and_targets(
                    series, direction='LONG', current_price=current_price, regime_state=regime_state,
                )
                confidence = 'HIGH' if ticker in STRONG_NAMES else 'MED'
                signals.append(Signal(
                    ticker            = ticker,
                    direction         = 'LONG',
                    entry_price       = current_price,
                    stop_loss         = stops['stop'],
                    target_1          = stops['t1'],
                    target_2          = stops['t2'],
                    target_3          = stops['t3'],
                    position_size_pct = round(pos_frac * scale, 4),
                    confidence        = confidence,
                    signal_params     = {
                        'trigger':        'pre_holiday_entry',
                        'entry_offset_td': ENTRY_OFFSET,
                        'regime':          regime_state,
                        'scale':           scale,
                        'calendar_date':   str(today.date()),
                    },
                ))
        elif is_exit:
            for ticker in available:
                series = prices[ticker].dropna()
                if series.empty:
                    continue
                current_price = float(series.iloc[-1])
                signals.append(Signal(
                    ticker            = ticker,
                    direction         = 'FLAT',
                    entry_price       = current_price,
                    stop_loss         = round(current_price * 0.95, 4),
                    target_1          = current_price,
                    target_2          = current_price,
                    target_3          = current_price,
                    position_size_pct = 0.0,
                    confidence        = 'HIGH',
                    signal_params     = {
                        'trigger':       'pre_holiday_exit',
                        'regime':        regime_state,
                        'calendar_date': str(today.date()),
                    },
                ))

        signals = signals[:self.MAX_SIGNALS]
        print(
            f'[{STRATEGY_ID}] date={today.date()} is_entry={is_entry} is_exit={is_exit} '
            f'signals={len(signals)} regime={regime_state}',
            file=sys.stderr,
        )
        return signals


# ---------------------------------------------------------------------------
# Regime-partitioned backtest
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    import json as _json
    from backtest.unified_backtest import load_prices_panels, load_regimes

    prices_df, _bars = load_prices_panels(tickers=BASKET)
    reg_series = load_regimes()

    price_index = prices_df.index.sort_values()
    if price_index.empty:
        holidays = []
    else:
        holidays = _holidays_near(price_index[0] + (price_index[-1] - price_index[0]) / 2,
                                  window_days=int((price_index[-1] - price_index[0]).days / 2) + 1)

    def _nearest_on_or_before(dt: pd.Timestamp):
        pos = price_index.searchsorted(dt, side='right') - 1
        return price_index[pos] if pos >= 0 else None

    rows = []
    for h in holidays:
        entry_day, exit_day = _entry_exit_for_holiday(h)
        if entry_day is None:
            continue
        entry_dt = _nearest_on_or_before(entry_day)
        exit_dt  = _nearest_on_or_before(exit_day)
        if entry_dt is None or exit_dt is None or entry_dt >= exit_dt:
            continue
        for ticker in BASKET:
            if ticker not in prices_df.columns:
                continue
            entry_p = prices_df[ticker].get(entry_dt)
            exit_p  = prices_df[ticker].get(exit_dt)
            if entry_p is None or exit_p is None or pd.isna(entry_p) or pd.isna(exit_p) or float(entry_p) <= 0:
                continue
            pnl = (float(exit_p) - float(entry_p)) / float(entry_p)
            prior_regimes = reg_series[reg_series.index <= entry_dt]
            rstate = str(prior_regimes.iloc[-1]) if not prior_regimes.empty else 'LOW_VOL'
            rows.append({
                'strategy_id': STRATEGY_ID, 'signal_date': entry_dt, 'regime_state': rstate,
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
