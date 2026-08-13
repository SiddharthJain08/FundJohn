"""
Value Factor Effect Within Countries — annual CAPE-based country ETF rotation.

Source: https://quantpedia.com/strategies/value-factor-effect-within-countries/

At year-end (December) filter 25 country MSCI ETFs to those with Shiller CAPE < 15,
rank by CAPE ascending, and hold the cheapest bottom tercile equally weighted.
When CAPE data is unavailable, uses trailing 1-year return as a cheapness proxy.
"""
from __future__ import annotations

import sys
import pandas as pd
import numpy as np
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['ValueFactorEffectWithinCountries']

INSTRUMENT_CLASS = 'etp'

COUNTRY_ETFS = {
    'Australia':    'EWA',
    'Brazil':       'EWZ',
    'Canada':       'EWC',
    'Switzerland':  'EWL',
    'China':        'FXI',
    'France':       'EWQ',
    'Germany':      'EWG',
    'Hong Kong':    'EWH',
    'Italy':        'EWI',
    'Japan':        'EWJ',
    'Korea':        'EWY',
    'Mexico':       'EWW',
    'Netherlands':  'EWN',
    'South Africa': 'EZA',
    'Singapore':    'EWS',
    'Spain':        'EWP',
    'Sweden':       'EWD',
    'Taiwan':       'EWT',
    'UK':           'EWU',
    'USA':          'SPY',
    'Russia':       'ERUS',
    'Israel':       'EIS',
    'India':        'INDA',
    'Poland':       'EPOL',
    'Turkey':       'TUR',
}

UNIVERSE = list(COUNTRY_ETFS.values())
CAPE_THRESHOLD = 15.0
QUANTILE = 3          # tercile
MIN_TERCILE = 3       # minimum ETFs needed to form a tercile
LOOKBACK_DAYS = 252   # 1-year proxy lookback when CAPE unavailable


def _extract_cape_row(cape_data: object, as_of_date) -> dict:
    """Return {country: cape_value} for the closest available date."""
    if cape_data is None:
        return {}
    try:
        if isinstance(cape_data, pd.DataFrame):
            cape_data.index = pd.to_datetime(cape_data.index)
            past = cape_data[cape_data.index <= pd.Timestamp(as_of_date)]
            if past.empty:
                return {}
            row = past.iloc[-1]
            return row.to_dict()
        if isinstance(cape_data, dict):
            return cape_data
    except Exception:
        pass
    return {}


class ValueFactorEffectWithinCountries(BaseStrategy):
    """Annual December rotation into cheapest-tercile country ETFs by Shiller CAPE.

    Filters ETFs with country CAPE < 15, sorts ascending by CAPE, holds the
    bottom 33% equally weighted. Falls back to inverse 1-year return proxy when
    CAPE data is absent. Rebalances once per year on first December bar.
    """

    id               = 'S_ast_value_factor_effect_within_countries'
    name             = 'Value Factor Effect Within Countries'
    description      = (
        'Countries with the lowest Shiller CAPE (< 15, bottom tercile) outperform; '
        'rotate annually into cheapest one-third of country ETFs.'
    )
    tier             = 2
    signal_frequency = 'daily'
    calendar_edge    = True   # window IS the signal; ports across regime flips (2026-08-13)
    min_lookback     = LOOKBACK_DAYS + 1
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS      = len(UNIVERSE)

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []

        # Annual December rebalance — only fire on first December bar.
        if len(prices.index) < 2:
            return []
        current_date = prices.index[-1]
        prev_date    = prices.index[-2]
        if current_date.month != 12:
            return []
        if prev_date.month == 12:
            return []   # already past first December bar this year

        scale = self.position_scale(regime_state)

        # Determine valuations: CAPE if available, else inverse 1yr return proxy.
        cape_row = _extract_cape_row(
            (aux_data or {}).get('cape_by_country_external'),
            current_date,
        )
        etf_to_country = {v: k for k, v in COUNTRY_ETFS.items()}

        valuations: dict[str, float] = {}
        for ticker in UNIVERSE:
            if ticker not in prices.columns:
                continue
            series = prices[ticker].dropna()
            if series.empty:
                continue
            country = etf_to_country.get(ticker, ticker)
            cape_val = cape_row.get(country)
            if cape_val is not None and float(cape_val) > 0:
                valuations[ticker] = float(cape_val)
            else:
                # Fallback: use inverse 1-year price return (low return ≈ cheap)
                if len(series) >= LOOKBACK_DAYS:
                    ret_1yr = float(series.iloc[-1] / series.iloc[-LOOKBACK_DAYS] - 1)
                    # Invert so lower CAPE analogue = lower score
                    valuations[ticker] = ret_1yr
                # If fewer than LOOKBACK_DAYS rows, skip this ticker

        use_cape = bool(cape_row)
        if use_cape:
            # Hard filter: CAPE < 15
            eligible = {t: v for t, v in valuations.items() if v < CAPE_THRESHOLD}
        else:
            # No hard threshold without real CAPE; keep all with data
            eligible = dict(valuations)

        if len(eligible) < MIN_TERCILE:
            print(
                f'[ValueFactorEffectWithinCountries] {current_date.date()}: only '
                f'{len(eligible)} ETFs eligible (< {MIN_TERCILE}) — holding cash',
                file=sys.stderr,
            )
            return []

        # Bottom-tercile by valuation (ascending = cheapest first)
        sorted_etfs = sorted(eligible.items(), key=lambda x: x[1])
        tercile_n   = max(1, len(sorted_etfs) // QUANTILE)
        long_etfs   = [t for t, _ in sorted_etfs[:tercile_n]]

        weight = round((1.0 / len(long_etfs)) * scale, 4)

        signals: List[Signal] = []
        for ticker in long_etfs:
            series = prices[ticker].dropna()
            current_price = float(series.iloc[-1])
            stops = self.compute_stops_and_targets(
                series, direction='LONG',
                current_price=current_price,
                regime_state=regime_state,
            )
            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = current_price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = weight,
                confidence        = 'HIGH' if use_cape else 'MED',
                signal_params     = {
                    'valuation':    round(eligible[ticker], 4),
                    'use_cape':     use_cape,
                    'tercile_n':    tercile_n,
                    'eligible_n':   len(eligible),
                    'scale':        scale,
                    'regime':       regime_state,
                },
            ))

        print(
            f'[debug] signals={len(signals)} regime={regime_state} '
            f'date={current_date.date()} use_cape={use_cape} tercile_n={tercile_n}',
            file=sys.stderr,
        )
        return signals


if __name__ == '__main__':
    import os
    import json
    PARQUET_ROOT = os.environ.get('OPENCLAW_PARQUET_ROOT', '/root/openclaw/data/master')
    sys.path.insert(0, '/root/openclaw/src')

    from backtest.quick_backtest import run_backtest_with_regime_partition

    prices_path  = os.path.join(PARQUET_ROOT, 'prices.parquet')
    regimes_path = os.path.join(PARQUET_ROOT, 'historical_regimes.parquet')
    prices  = pd.read_parquet(prices_path, columns=[t for t in UNIVERSE
                              if t in pd.read_parquet(prices_path, columns=[]).columns
                              ] or UNIVERSE[:1])
    regimes = pd.read_parquet(regimes_path)[['date', 'regime_state']].rename(
        columns={'date': 'signal_date'}
    )
    regimes['signal_date'] = pd.to_datetime(regimes['signal_date'])

    strat = ValueFactorEffectWithinCountries()
    rows = []
    prices.index = pd.to_datetime(prices.index)
    for i in range(LOOKBACK_DAYS + 1, len(prices)):
        window = prices.iloc[:i + 1]
        cur = window.index[-1]
        if cur.year < 2017 or cur.year > 2025:
            continue
        regime_row = regimes[regimes['signal_date'] <= cur]
        regime_state = regime_row['regime_state'].iloc[-1] if not regime_row.empty else 'LOW_VOL'
        sigs = strat.generate_signals(window, {'state': regime_state}, UNIVERSE)
        for sig in sigs:
            s = prices[sig.ticker].dropna()
            entry_idx = s.index.get_loc(cur) if cur in s.index else None
            if entry_idx is None or entry_idx + 252 >= len(s):
                continue
            exit_price = float(s.iloc[entry_idx + 252])
            pnl = (exit_price / sig.entry_price - 1.0)
            r = pnl / max(abs(sig.entry_price - sig.stop_loss) / sig.entry_price, 1e-6)
            rows.append({
                'strategy_id': 'S_ast_value_factor_effect_within_countries',
                'signal_date': str(cur.date()),
                'regime_state': regime_state,
                'pnl': float(pnl),
                'r_multiple': float(r),
            })

    if not rows:
        print('[backtest] no trades generated — check data coverage', file=sys.stderr)
        sys.exit(1)

    trades_df = pd.DataFrame(rows)
    result = run_backtest_with_regime_partition(
        trades_df,
        strategy_id='S_ast_value_factor_effect_within_countries',
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(json.dumps(result, indent=2, default=str))
