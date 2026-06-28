"""
Annual cross-country CAPE value rotation across 25 country iShares MSCI ETFs.

Source: https://quantpedia.com/strategies/value-factor-effect-within-countries/
At the end of December each year, filter to country ETFs with Shiller CAPE < 15,
then invest equally in the cheapest tercile (lowest CAPE). Holds cash when fewer
than 3 countries qualify. Falls back to a price/10yr-average proxy when real
CAPE data is unavailable via aux_data['cape_by_country_external'].
"""
from __future__ import annotations

import sys
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['AstValueFactorEffectWithinCountries']

INSTRUMENT_CLASS = 'etp'

# Fixed universe: 25 country iShares MSCI ETFs
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

CAPE_THRESHOLD = 15.0   # only countries with CAPE below this qualify
MIN_QUALIFIED  = 3      # minimum to form a tercile
LOOKBACK_10Y   = 2520   # ~10 trading years for price proxy


class AstValueFactorEffectWithinCountries(BaseStrategy):
    """Annual CAPE value rotation across 25 country ETFs; equally weights cheapest tercile.

    Rebalances once per year on the first December bar. Requires CAPE data via
    aux_data['cape_by_country_external'] (dict mapping country name → CAPE float).
    Falls back to a price/10yr-average proxy when real CAPE data is absent.
    """

    id                = 'S_ast_value_factor_effect_within_countries'
    name              = 'Value Factor CAPE Effect Within Countries'
    description       = (
        'Annual cross-country CAPE value rotation: invest equally in the bottom tercile '
        'of country ETFs with Shiller CAPE below 15, rebalanced every December.'
    )
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = 252
    # Value premium works across regimes; CRISIS scaling via position_scale handles stress
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = 10

    def default_parameters(self) -> dict:
        return {
            'cape_threshold': CAPE_THRESHOLD,
            'quantile':       3,     # tercile split
            'pos_size_frac':  0.08,  # per-ETF base allocation
            'lookback_10y':   LOOKBACK_10Y,
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

        # Fire only on the first December bar each year (month-flip detection)
        if not hasattr(prices.index, 'month'):
            print('[debug] signals=0', file=sys.stderr)
            return []

        current_date = prices.index[-1]
        if current_date.month != 12:
            print('[debug] signals=0', file=sys.stderr)
            return []

        if len(prices) >= 2 and prices.index[-2].month == 12:
            # Already processed this December — skip until next year
            print('[debug] signals=0', file=sys.stderr)
            return []

        available = [t for t in COUNTRY_ETFS.values() if t in prices.columns]
        if not available:
            print('[debug] signals=0 (no country ETFs in price panel)', file=sys.stderr)
            return []

        cape_threshold = float(self.parameters.get('cape_threshold', CAPE_THRESHOLD))
        quantile       = int(self.parameters.get('quantile', 3))

        cape_scores = self._get_cape_scores(prices, available, aux_data)
        cape_source = (
            'real' if (aux_data and isinstance(aux_data.get('cape_by_country_external'), dict))
            else 'proxy'
        )

        # Filter to countries with CAPE below threshold
        qualified = {t: c for t, c in cape_scores.items() if c < cape_threshold}

        if len(qualified) < quantile:
            print(
                f'[debug] signals=0 ({len(qualified)} countries qualify CAPE<{cape_threshold}, need {quantile})',
                file=sys.stderr,
            )
            return []

        # Sort ascending by CAPE; take bottom tercile (cheapest countries)
        sorted_by_cape = sorted(qualified.items(), key=lambda x: x[1])
        tercile_size   = max(1, len(sorted_by_cape) // quantile)
        long_tickers   = [t for t, _ in sorted_by_cape[:tercile_size]]

        scale         = self.position_scale(regime_state)
        pos_size_base = float(self.parameters.get('pos_size_frac', 0.08))
        pos_size      = round(pos_size_base * scale, 4)

        signals: List[Signal] = []
        for ticker in long_tickers:
            series = prices[ticker].dropna()
            if len(series) < 14:
                continue
            current_price = float(series.iloc[-1])
            cape_val      = float(cape_scores.get(ticker, 0.0))

            stops = self.compute_stops_and_targets(
                series,
                direction='LONG',
                current_price=current_price,
                regime_state=regime_state,
            )

            confidence = 'HIGH' if cape_val < 10.0 else ('MED' if cape_val < 12.5 else 'LOW')

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
                    'cape_value':      round(cape_val, 2),
                    'rebalance_year':  int(current_date.year),
                    'long_basket':     long_tickers,
                    'qualified_count': len(qualified),
                    'regime':          regime_state,
                    'scale':           scale,
                    'cape_source':     cape_source,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]

    def _get_cape_scores(
        self,
        prices:   pd.DataFrame,
        available: list,
        aux_data:  dict | None,
    ) -> dict:
        """Return CAPE-like score per ticker.

        Prefers real CAPE from aux_data['cape_by_country_external']
        (dict: country name → float). Falls back to price/10yr-avg proxy
        scaled so that P10=1.0 maps to synthetic CAPE=15 (long-run median).
        """
        ticker_to_country = {v: k for k, v in COUNTRY_ETFS.items()}

        if aux_data and isinstance(aux_data.get('cape_by_country_external'), dict):
            cape_raw = aux_data['cape_by_country_external']
            result = {}
            for t in available:
                country = ticker_to_country.get(t, '')
                val = cape_raw.get(country)
                if val is not None and float(val) > 0:
                    result[t] = float(val)
            if result:
                return result
            # Real CAPE dict was empty — fall through to proxy

        # Price-based proxy: P10 = current / 10yr-avg price, scaled to CAPE range.
        # P10=1.0 → synthetic CAPE 15 (neutral); lower P10 → cheaper country.
        lookback = int(self.parameters.get('lookback_10y', LOOKBACK_10Y))
        result = {}
        for t in available:
            series = prices[t].dropna()
            if len(series) < 252:
                continue
            current_price = float(series.iloc[-1])
            hist_window   = series.iloc[-min(len(series), lookback):]
            ten_yr_avg    = float(hist_window.mean())
            if ten_yr_avg <= 0:
                continue
            p10_ratio       = current_price / ten_yr_avg
            synthetic_cape  = p10_ratio * 15.0  # P10=1 → 15, P10=0.5 → 7.5
            result[t] = round(synthetic_cape, 2)
        return result
