"""
Option Expiration Week Effect — QuantPedia port.

Holds OEF (iShares S&P 100 ETF) long during Mon–Thu of the standard monthly
options-expiration week (the week containing the 3rd Friday of each month).
Liquidates to cash on the expiration Friday and holds cash all other days.

The strategy exploits pinning and dealer delta-hedging activity that concentrates
buying pressure in the S&P 100 during expiration week.

Source: https://quantpedia.com/strategies/option-expiration-week-effect/
"""
from __future__ import annotations

import sys
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['OptionExpirationWeekEffect']

INSTRUMENT_CLASS = 'etp'
TICKER = 'OEF'   # iShares S&P 100 ETF


def _third_friday(year: int, month: int) -> pd.Timestamp:
    """Return the date of the 3rd Friday of the given year/month."""
    first = pd.Timestamp(year=year, month=month, day=1)
    # weekday(): Monday=0 … Friday=4
    days_to_first_friday = (4 - first.weekday()) % 7
    first_friday = first + pd.Timedelta(days=days_to_first_friday)
    return first_friday + pd.Timedelta(weeks=2)


def _in_expiration_window(today: pd.Timestamp) -> bool:
    """Return True if today is Mon–Thu of standard monthly options expiration week."""
    tf = _third_friday(today.year, today.month)
    expiry_monday = tf - pd.Timedelta(days=4)
    return expiry_monday <= today <= expiry_monday + pd.Timedelta(days=3)


def _is_expiration_friday(today: pd.Timestamp) -> bool:
    """Return True if today is the 3rd Friday (standard monthly expiration day)."""
    return today == _third_friday(today.year, today.month)


class OptionExpirationWeekEffect(BaseStrategy):
    """Long OEF (S&P 100 ETF) Mon–Thu of options-expiration week; cash otherwise.

    Source: https://quantpedia.com/strategies/option-expiration-week-effect/
    """

    id                = 'S_ast_option_expiration_week_effect'
    name              = 'Option Expiration Week Effect'
    description       = (
        'Long OEF (S&P 100 ETF) during Mon–Thu of monthly options-expiration week; '
        'cash on expiration Friday and all other days.'
    )
    tier              = 2
    signal_frequency  = 'daily'
    calendar_edge     = True   # window IS the signal; ports across regime flips (2026-08-13)
    min_lookback      = 1
    # Calendar-driven structural effect — dealer hedging occurs in all regimes.
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = 1

    def default_parameters(self) -> dict:
        return {
            'pos_size_frac': 0.90,   # base allocation to OEF during expiry window (pre-regime-scale)
        }

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print('[OptionExpirationWeekEffect] no price data — returning []', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print(f'[OptionExpirationWeekEffect] skipping regime {regime_state}', file=sys.stderr)
            return []

        if TICKER not in prices.columns:
            print(f'[OptionExpirationWeekEffect] {TICKER} not in prices — returning []', file=sys.stderr)
            return []

        try:
            today = pd.Timestamp(prices.index[-1])
        except (IndexError, TypeError):
            print('[OptionExpirationWeekEffect] cannot parse date index — returning []', file=sys.stderr)
            return []

        oef_series = prices[TICKER].dropna()
        if oef_series.empty:
            print('[OptionExpirationWeekEffect] OEF series empty — returning []', file=sys.stderr)
            return []

        current_price = float(oef_series.iloc[-1])
        if current_price <= 0:
            print(f'[OptionExpirationWeekEffect] invalid price {current_price} — returning []', file=sys.stderr)
            return []

        in_window = _in_expiration_window(today)
        is_expiry  = _is_expiration_friday(today)

        if in_window:
            # Monday through Thursday of expiry week — go long OEF.
            stops = self.compute_stops_and_targets(
                oef_series,
                direction='LONG',
                current_price=current_price,
                regime_state=regime_state,
            )
            scale    = self.position_scale(regime_state)
            pos_size = round(self.parameters.get('pos_size_frac', 0.90) * scale, 4)

            signal = Signal(
                ticker            = TICKER,
                direction         = 'LONG',
                entry_price       = current_price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = pos_size,
                confidence        = 'HIGH',
                signal_params     = {
                    'regime':            regime_state,
                    'scale':             scale,
                    'calendar_date':     str(today.date()),
                    'in_expiry_window':  True,
                    'expiry_friday':     str(_third_friday(today.year, today.month).date()),
                },
            )
            print(
                f'[OptionExpirationWeekEffect] LONG {TICKER} entry={current_price:.2f} '
                f'stop={stops["stop"]:.2f} pos={pos_size:.4f} '
                f'date={today.date()} regime={regime_state}',
                file=sys.stderr,
            )
            signals = [signal]
        else:
            # Expiration Friday or non-expiration week — hold cash.
            signal = Signal(
                ticker            = TICKER,
                direction         = 'FLAT',
                entry_price       = current_price,
                stop_loss         = current_price * 0.95,
                target_1          = current_price,
                target_2          = current_price,
                target_3          = current_price,
                position_size_pct = 0.0,
                confidence        = 'HIGH',
                signal_params     = {
                    'regime':            regime_state,
                    'calendar_date':     str(today.date()),
                    'in_expiry_window':  False,
                    'is_expiry_day':     is_expiry,
                },
            )
            print(
                f'[OptionExpirationWeekEffect] FLAT {TICKER} date={today.date()} '
                f'is_expiry={is_expiry} regime={regime_state}',
                file=sys.stderr,
            )
            signals = [signal]

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
