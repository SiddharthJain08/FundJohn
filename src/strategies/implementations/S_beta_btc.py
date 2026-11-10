"""
Beta sleeve — long BTC-USD, always (crypto engine Phase 3, Task 3).

Spec: docs/specs/2026-11-06-crypto-engine-whale-flow-design.md (Phase 3,
pooled sizing: S_beta_btc / S_m_crypto); crypto twin of S_beta_spy.

No alpha. Its backtest sleeves ARE (up to honest costs and entry-regime
tagging) BTC-USD's CRYPTO-regime Sharpes; it is eligible in every regime
regardless of the activation slider (activation_assigner
`benchmark_sleeve_always_on`, which holds for every benchmark-sleeve id) and
is weighted on S_m for BTC-USD (strategy_weights resolves each sleeve's
benchmark from registry parameters).

Signal shape: ONE LONG BTC-USD per bar, hold_days = 21 (ruling: same as SPY;
the sizer re-targets daily anyway). Stop/targets never bind.

Exit hook: flatten when the CRYPTO regime differs from the lot's entry regime
(recorded in signal_params at signal time). Both engine paths hand a
crypto-class strategy the crypto regime: run_strategies (`ic == 'crypto'`)
for generate_signals, and update_pnl's exit-hook glue for should_exit
(load_crypto_regime_state, once per update_pnl call). The backtest passes the
crypto-partition payload (unified_backtest.load_regimes('crypto')).

benchmark_sleeve / instrument_class / benchmark_ticker are mirrored into
strategy_registry.parameters by scripts/register_s_beta_btc.py (the sizer
reads the registry, never this class); instrument_class is also the
manifest's top-level field (what instrument_class_for routes on).
"""
from __future__ import annotations
import sys
from typing import List

import pandas as pd

from strategies.base import BaseStrategy, Signal, CANONICAL_REGIMES

__all__ = ['BetaBtc']

STRATEGY_ID      = 'S_beta_btc'
INSTRUMENT_CLASS = 'crypto'
BENCHMARK        = 'BTC-USD'
HOLD_DAYS        = 21
STOP_FRAC        = 0.60    # stop 40% below entry: never binds inside a 21-day lot
TARGET_FRACS     = (5.0, 6.0, 7.0)

class BetaBtc(BaseStrategy):
    id                = STRATEGY_ID
    name              = 'Beta sleeve — long BTC-USD'
    description       = ('Crypto benchmark sleeve: long BTC-USD every bar, hold 21, exits on '
                         'crypto-regime flip; eligible in all regimes.')
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = 2
    instrument_class  = INSTRUMENT_CLASS
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = 1
    benchmark_sleeve  = True
    exit_hook         = True
    BENCHMARK         = BENCHMARK

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty or BENCHMARK not in prices.columns:
            print('[debug] signals=0 (no BTC-USD column)', file=sys.stderr)
            return []
        px = prices[BENCHMARK].iloc[-1]
        try:
            px = float(px)
        except (TypeError, ValueError):
            print('[debug] signals=0 (BTC-USD last close not numeric)', file=sys.stderr)
            return []
        if not (px == px and px > 0):
            print('[debug] signals=0 (BTC-USD last close missing)', file=sys.stderr)
            return []
        state = regime.get('state') if isinstance(regime, dict) else None
        return [Signal(
            ticker            = BENCHMARK,
            direction         = 'LONG',
            entry_price       = px,
            stop_loss         = round(px * STOP_FRAC, 4),
            target_1          = round(px * TARGET_FRACS[0], 4),
            target_2          = round(px * TARGET_FRACS[1], 4),
            target_3          = round(px * TARGET_FRACS[2], 4),
            position_size_pct = 0.10,
            confidence        = 'HIGH',
            signal_params     = {'hold_days': HOLD_DAYS, 'benchmark_sleeve': True, 'regime': state},
        )]

    def should_exit(self, position: dict, prices: pd.DataFrame,
                    regime: dict, aux_data: dict = None):
        """Flatten at today's close when the CRYPTO regime differs from the
        lot's entry regime. Missing / non-canonical state on either side =>
        hold (the hold cap still protects). No price reads."""
        state = regime.get('state') if isinstance(regime, dict) else None
        entry = ((position or {}).get('signal_params') or {}).get('regime')
        if state not in CANONICAL_REGIMES or entry not in CANONICAL_REGIMES:
            return None
        return 'regime_exit' if state != entry else None
