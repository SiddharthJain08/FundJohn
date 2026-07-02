"""
Pairs Trading with Country ETFs — ported from Quantpedia / QuantConnect.

Source: https://quantpedia.com/strategies/pairs-trading-with-country-etfs/

Hypothesis: Pairs of co-moving international country ETFs revert to their
historical spread after divergence; going long the underperformer and short
the outperformer when spread exceeds 0.5 std dev captures mean-reversion alpha.

Universe: 23 iShares MSCI country ETFs + SPY (fixed basket, no SP-2 filter).
Formation: 120-day rolling window; normalize each series to $1 at window start;
select top-5 closest pairs by sum of squared deviations.
Signal: enter long/short when |spread - mean| > 0.5 * std; FLAT on convergence.
Sizing: 1/5 per pair (equal weight), regime-scaled.
"""
from __future__ import annotations

import sys
import itertools
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['AstPairsTradingWithCountryEtfs']

INSTRUMENT_CLASS = 'etp'

BASKET = [
    'EWA', 'EWO', 'EWK', 'EWZ', 'EWC', 'FXI', 'EWQ', 'EWG', 'EWH',
    'EWI', 'EWJ', 'EWM', 'EWW', 'EWN', 'EWS', 'EZA', 'EWY', 'EWP',
    'EWD', 'EWL', 'EWT', 'THD', 'EWU', 'SPY',
]

FORMATION = 120
MAX_PAIRS = 5
ENTRY_THRESHOLD = 0.5   # std-devs for entry


class AstPairsTradingWithCountryEtfs(BaseStrategy):
    """Pairs mean-reversion across 23 international country ETFs + SPY.

    Each day: compute 120-day normalized price series, find 5 closest pairs by
    sum-of-squared-deviations, enter long/short when spread diverges > 0.5 std.
    Exit signals are emitted when spread reverts to within the threshold.
    """

    id                = 'S_ast_pairs_trading_with_country_etfs'
    name              = 'Pairs Trading with Country ETFs'
    description       = (
        'Mean-reversion pairs trading across 23 iShares MSCI country ETFs + SPY; '
        'long undervalued / short overvalued leg when spread exceeds 0.5 std dev.'
    )
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = FORMATION
    # Mean-reversion works well in stable and mildly stressed regimes; exclude
    # CRISIS where pair correlations break down under forced deleveraging.
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    MAX_SIGNALS = 10   # 5 pairs × 2 legs

    def default_parameters(self) -> dict:
        return {
            'formation':       FORMATION,
            'max_pairs':       MAX_PAIRS,
            'entry_threshold': ENTRY_THRESHOLD,
            'pos_size_frac':   0.20,   # 1/max_pairs per pair leg
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

        formation       = int(self.parameters.get('formation', FORMATION))
        max_pairs       = int(self.parameters.get('max_pairs', MAX_PAIRS))
        entry_threshold = float(self.parameters.get('entry_threshold', ENTRY_THRESHOLD))
        pos_frac        = float(self.parameters.get('pos_size_frac', 0.20))

        # Intersect basket with available price columns.
        available = [t for t in BASKET if t in prices.columns]
        if len(available) < 2:
            print('[debug] signals=0', file=sys.stderr)
            return []

        if len(prices) < formation:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Rolling formation window; drop any ticker with NaN in that window.
        window = prices[available].iloc[-formation:].copy()
        window = window.dropna(axis=1)
        tickers = list(window.columns)
        if len(tickers) < 2:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Normalize each series to $1 at the start of the window.
        first_row = window.iloc[0].replace(0, float('nan'))
        norm = window.div(first_row, axis=1).dropna(axis=1)
        tickers = list(norm.columns)
        if len(tickers) < 2:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Compute pairwise distance = sum of squared deviations between normalized series.
        distances: dict[tuple, float] = {}
        for a, b in itertools.combinations(tickers, 2):
            diff = norm[a] - norm[b]
            distances[(a, b)] = float((diff ** 2).sum())

        # Select top-max_pairs closest pairs.
        top_pairs = sorted(distances, key=distances.__getitem__)[:max_pairs]

        scale    = self.position_scale(regime_state)
        pos_size = round(pos_frac * scale, 4)

        signals: List[Signal] = []

        for a, b in top_pairs:
            spread = norm[a] - norm[b]
            mean   = float(spread.mean())
            std    = float(spread.std())
            if std < 1e-10:
                continue

            current_spread = float(spread.iloc[-1])
            z = (current_spread - mean) / std

            if abs(z) <= entry_threshold:
                # Spread has converged — emit FLAT to close any open legs.
                for ticker in (a, b):
                    if ticker not in prices.columns:
                        continue
                    price = float(prices[ticker].dropna().iloc[-1])
                    signals.append(Signal(
                        ticker            = ticker,
                        direction         = 'FLAT',
                        entry_price       = price,
                        stop_loss         = round(price * 0.95, 4),
                        target_1          = round(price * 1.02, 4),
                        target_2          = round(price * 1.04, 4),
                        target_3          = round(price * 1.06, 4),
                        position_size_pct = 0.0,
                        confidence        = 'LOW',
                        signal_params     = {
                            'pair': f'{a}-{b}',
                            'action': 'convergence_exit',
                            'z': round(z, 3),
                            'regime': regime_state,
                        },
                    ))
                continue

            # Divergence: a overvalued when z > 0 → long b, short a; else vice-versa.
            long_t  = b if z > 0 else a
            short_t = a if z > 0 else b

            for ticker, direction in ((long_t, 'LONG'), (short_t, 'SHORT')):
                if ticker not in prices.columns:
                    continue
                series = prices[ticker].dropna()
                if len(series) < 14:
                    continue
                price = float(series.iloc[-1])
                stops = self.compute_stops_and_targets(
                    series, direction, price, regime_state=regime_state,
                )
                confidence = 'HIGH' if abs(z) > 1.0 else 'MED'
                signals.append(Signal(
                    ticker            = ticker,
                    direction         = direction,
                    entry_price       = price,
                    stop_loss         = stops['stop'],
                    target_1          = stops['t1'],
                    target_2          = stops['t2'],
                    target_3          = stops['t3'],
                    position_size_pct = pos_size,
                    confidence        = confidence,
                    signal_params     = {
                        'pair':     f'{a}-{b}',
                        'z':        round(z, 3),
                        'distance': round(distances[(a, b)], 4),
                        'regime':   regime_state,
                    },
                ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
