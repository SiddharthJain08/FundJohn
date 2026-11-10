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
(recorded in signal_params at signal time — run_strategies hands crypto-class
strategies the crypto regime, engine.py run_strategies `ic == 'crypto'`).
The LIVE hook (engine.update_pnl -> `_strat.should_exit(position, prices,
regime, aux_data)`) passes the cycle's EQUITY regime (engine.load_regime) to
every exit_hook strategy, equity or crypto. That contract is left untouched;
instead should_exit recognises the engine's equity dict (it alone carries
`vix_level`) and substitutes the live crypto regime
(regime.crypto_regime.load_crypto_regime_state, read once per update_pnl
call: cached on the identity of the regime object passed in). The backtest
payload (unified_backtest._regime_payload_full on the crypto partition) is
used as-is and never triggers a live-file read.

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

# The only key that distinguishes execution.engine.load_regime()'s EQUITY dict
# from both the backtest payload and load_crypto_regime_state()'s dict.
_EQUITY_LIVE_MARKER = 'vix_level'


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

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._live_regime_key = None     # the equity regime object last substituted
        self._live_crypto_state = None

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

    def _crypto_state(self, regime) -> str | None:
        """The crypto regime state for this hook call. Engine equity dict ->
        live crypto regime (one read per update_pnl batch); anything else ->
        its own 'state'. A read failure -> None (hold)."""
        if not isinstance(regime, dict):
            return None
        if _EQUITY_LIVE_MARKER not in regime:
            return regime.get('state')
        if self._live_regime_key is not regime:
            state = None
            try:
                from regime import crypto_regime as _cr
                state = (_cr.load_crypto_regime_state() or {}).get('state')
            except Exception as e:
                print(f'[S_beta_btc] crypto regime read failed ({type(e).__name__}: {e}); holding',
                      file=sys.stderr)
            self._live_regime_key = regime
            self._live_crypto_state = state
        return self._live_crypto_state

    def should_exit(self, position: dict, prices: pd.DataFrame,
                    regime: dict, aux_data: dict = None):
        """Flatten at today's close when the CRYPTO regime differs from the
        lot's entry regime. Missing / non-canonical state on either side =>
        hold (the hold cap still protects). No price reads."""
        state = self._crypto_state(regime)
        entry = ((position or {}).get('signal_params') or {}).get('regime')
        if state not in CANONICAL_REGIMES or entry not in CANONICAL_REGIMES:
            return None
        return 'regime_exit' if state != entry else None
