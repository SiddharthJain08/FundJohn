"""
Base class for all hardcoded strategies.
Every strategy in the registry inherits from this.

CRITICAL RULES for all strategy implementations:
1. generate_signals() must be pure Python — no API calls, no LLM calls
2. All data comes in as pre-loaded DataFrames
3. No randomness unless seeded for reproducibility
4. Handle missing data gracefully — return empty DataFrame, never raise
5. Must be deterministic: same inputs → same outputs always
"""

import pandas as pd
import numpy as np
from abc import ABC, abstractmethod
from typing import List, Optional
from dataclasses import dataclass, field


@dataclass
class OptionSpec:
    """Declarative option contract spec carried on a Signal (SP-4 Phase 0).

    The backtest engine (options_backtest.py) and a future live executor read
    the SAME spec to select a contract — parity by construction. Equity/crypto/
    etp signals leave Signal.option_spec=None and behave byte-identically.
    """
    underlying:    str                       # e.g. 'SPY'
    right:         str = 'call'              # 'call' | 'put' (per-leg; ignored for straddle/strangle)
    strike_rule:   str = 'target_delta'      # 'target_delta' | 'atm' | 'fixed_moneyness'
    target_delta:  float = 0.30              # used when strike_rule='target_delta'
    moneyness:     Optional[float] = None    # K/S, used when strike_rule='fixed_moneyness'
    dte_target:    int = 30                  # nearest monthly expiry >= this many calendar days
    structure:     str = 'single'            # 'single' | 'straddle' | 'strangle' | 'vertical' | 'credit_vertical' | 'iron_condor'
    spread_width_pct: float = 0.03           # vertical: far(short) leg strike offset = near ± pct*spot
    hedge:         str = 'none'              # 'none' | 'delta'
    hedge_cadence: str = 'daily'             # rehedge frequency when hedge='delta'
    roll_dte:      int = 7                   # roll when remaining DTE <= this
    hold_to_expiry: bool = False             # income legs may hold to expiry instead of rolling
    exercise:      str = 'american'          # 'american' | 'european' — US-listed equity/ETF options are American (spec 2026-09-06 B.4, G9)

    @classmethod
    def from_dict(cls, d):
        """Rebuild from a dict (e.g. signal_params['option_spec']). Fail-closed:
        returns None for None / non-dict / missing required `underlying`."""
        if not isinstance(d, dict):
            return None
        if not d.get('underlying'):
            return None
        import dataclasses as _dc
        allowed = {f.name for f in _dc.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in allowed})


@dataclass
class Signal:
    ticker:            str
    direction:         str          # LONG | SHORT | SELL_VOL | BUY_VOL | FLAT
    entry_price:       float
    stop_loss:         float
    target_1:          float
    target_2:          float
    target_3:          float
    position_size_pct: float
    confidence:        str          # HIGH | MED | LOW
    signal_params:     dict = field(default_factory=dict)
    # Phase 5 (2026-04-22): per-signal deterministic features that used to be
    # computed by research_report.py — HV, beta, momentum, EV/p_t1, any
    # strategy-specific context TradeJohn consumes. Strategies can populate
    # this directly if they want to carry strategy-specific features through
    # to the handoff; trade_handoff_builder.py otherwise fills it with the
    # standard feature set. Default empty keeps old strategies compatible.
    features:          dict = field(default_factory=dict)
    # SP-4 Phase 0: option contract spec for instrument_class='option' strategies.
    # None for equity/crypto/etp — keeps every existing strategy byte-identical.
    option_spec: Optional['OptionSpec'] = None


REGIME_POSITION_SCALE = {
    'LOW_VOL':       1.00,
    'TRANSITIONING': 0.55,
    'HIGH_VOL':      0.35,
    'CRISIS':        0.15,
}

# Canonical regime vocabulary — the HMM classifier only ever emits these four.
# Strategies that declare any other tag in `active_in_regimes` will never fire
# because `should_run()` does exact-string membership. See docs below.
CANONICAL_REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')

# Soft synonyms StrategyCoder sometimes emits from recent paper vocabularies.
# At class-definition time we expand synonyms into canonical tags so legacy
# strategies keep working. New strategies should use canonical directly.
REGIME_SYNONYMS = {
    'NEUTRAL':  ('LOW_VOL', 'TRANSITIONING'),   # calm-to-mildly-uncertain band
    'RISK_OFF': ('HIGH_VOL', 'CRISIS'),         # elevated-stress band
    'RISK_ON':  ('LOW_VOL', 'TRANSITIONING'),   # mirror of RISK_OFF
}

# Tighten ATR-based stops in high-vol regimes to preserve R:R geometry.
# Without this, 2× ATR stops balloon to 6-9% in TRANSITIONING/HIGH_VOL while
# targets remain fixed at 5-20%, collapsing R:R to <1x and making EV negative.
REGIME_ATR_SCALE = {
    'LOW_VOL':       1.00,
    'TRANSITIONING': 0.70,
    'HIGH_VOL':      0.55,
    'CRISIS':        0.35,
}


class BaseStrategy(ABC):
    """All hardcoded strategies inherit from this."""

    # Subclasses must define these
    id:               str = ''
    name:             str = ''
    description:      str = ''
    tier:             int = 3
    signal_frequency: str = 'daily'
    # Calendar-edge strategy: its calendar window IS the signal (turn-of-month,
    # expiration week, Monday effect, annual fundamentals windows, seasonality).
    # Two consequences (operator directives 2026-08-13): exempt from the
    # regime-flip cadence_reset bypass (it cannot re-mint off-window), and its
    # persisted signals PORT ACROSS regime flips for the rest of their cadence
    # window — but only into regimes the strategy is eligible for
    # (strategy_regime_params.eligible; enforced in the sizer's loaders).
    calendar_edge:    bool = False
    min_lookback:     int = 20
    active_in_regimes: List[str] = None
    # Safety cap: generate_signals should not return more than this many signals.
    # Prevents runaway signal counts at large universe sizes without slicing in each strategy.
    MAX_SIGNALS:      int = 50
    # Per-bar exit hook (spec docs/specs/2026-08-28-per-bar-exit-hook-spec.md §1).
    # Explicit opt-in: the backtest open-book path and (Phase 2) live
    # update_pnl call should_exit() ONLY when this is True. Overriding
    # should_exit without setting the flag is inert by design.
    exit_hook:        bool = False
    # Benchmark (beta) sleeve — spec docs/specs/2026-08-29-benchmark-relative-
    # sizing-spec.md §2.4. True marks a strategy whose conviction IS the
    # market's own regime Sharpe (e.g. S_beta_spy). The sizer reads the
    # mirrored strategy_registry.parameters.benchmark_sleeve to exempt the
    # sleeve's tickers from the acting-strategy gate, the S_adj − S_m hurdle
    # and both caps. Mirroring is done at registration (Task 15 runbook).
    benchmark_sleeve: bool = False

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.active_in_regimes is None:
            cls.active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
        # Preserve the author's original declaration so validate_strategy can
        # inspect it and reject bad tags at the candidate→paper gate.
        cls._raw_active_in_regimes = list(cls.active_in_regimes)
        # Normalize non-canonical tags at runtime so legacy/imported strategies
        # don't silently become inert. Synonyms expand; unknown tags are
        # dropped with a warning (they'd never match a HMM-emitted state
        # anyway; silently keeping them hid real bugs).
        normalized: list[str] = []
        seen: set[str] = set()
        for tag in cls.active_in_regimes:
            if tag in CANONICAL_REGIMES:
                if tag not in seen:
                    normalized.append(tag); seen.add(tag)
            elif tag in REGIME_SYNONYMS:
                import warnings
                warnings.warn(
                    f"{cls.__name__}: regime tag '{tag}' is a synonym — expanding to {REGIME_SYNONYMS[tag]}. "
                    f"Use canonical tags {CANONICAL_REGIMES} directly to avoid this warning.",
                    stacklevel=3,
                )
                for exp in REGIME_SYNONYMS[tag]:
                    if exp not in seen:
                        normalized.append(exp); seen.add(exp)
            else:
                import warnings
                warnings.warn(
                    f"{cls.__name__}: unknown regime tag '{tag}' dropped — not in {CANONICAL_REGIMES}.",
                    stacklevel=3,
                )
        cls.active_in_regimes = normalized or ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
        # exit_hook=True with the base should_exit is a silent no-op that
        # would masquerade as a tested exit — refuse at class definition.
        if cls.__dict__.get('exit_hook', False) or getattr(cls, 'exit_hook', False):
            if cls.should_exit is BaseStrategy.should_exit:
                raise TypeError(
                    f'{cls.__name__}: exit_hook=True requires overriding should_exit()')

    def __init__(self, parameters: dict = None):
        # Merge DB overrides on top of code defaults — replacing the dict
        # outright (the prior behavior) caused KeyErrors when a strategy's
        # default_parameters() defined a key the DB row didn't carry. The
        # 2026-04-29 cycle hit this on S15_iv_rv_arb (`min_option_vol`) and
        # S_sparse_basis_pursuit_sdf (`n_rff`) — both rows in
        # strategy_registry.parameters were Mastermind-curated subsets.
        defaults = self.default_parameters()
        if parameters:
            defaults.update(parameters)
        self.parameters = defaults

    def default_parameters(self) -> dict:
        return {}

    def should_run(self, regime_state: str) -> bool:
        """Check if this strategy should generate signals in current regime."""
        return regime_state in (self.active_in_regimes or [])

    def cadence_reset(self, regime) -> bool:
        """True on a regime-flip day. The engine stamps regime['cadence_reset']
        when the regime-of-record differs from the regime the last persisted
        signal set was built under (operator directive 2026-08-13): on a flip,
        every rebalance-cadence strategy emits its current book same-day and
        cadence persistence restarts from that day. Rebalance-calendar gates
        (month/week/fortnight boundary checks) must OR themselves with this.
        Timing-edge strategies (turn-of-month, expiration week, Monday effect,
        annual fundamentals windows, seasonality) must NOT consult it — their
        calendar IS the signal, not a rebalance schedule."""
        return bool(isinstance(regime, dict) and regime.get('cadence_reset'))

    def position_scale(self, regime_state: str) -> float:
        """Regime-adjusted position scale."""
        return REGIME_POSITION_SCALE.get(regime_state, 0.35)

    def should_exit(self, position: dict, prices: pd.DataFrame,
                    regime: dict, aux_data: dict = None):
        """Per-bar exit decision for ONE open position (spec §1).

        Called only when `exit_hook` is True, once per open position per bar,
        AFTER the intra-bar bracket check and BEFORE the time stop. Return a
        short snake_case reason token to flatten at TODAY's close, or None to
        keep holding. Must be a pure, look-ahead-safe function of its
        arguments: `prices` ends at the evaluation bar; `position` carries
        ticker, direction ('LONG'|'SHORT'), entry_price, entry_date,
        days_held, stop_loss, target_1 and the entry-time signal_params dict.
        Raising is caught by the caller and treated as None (hold).

        Regime contract: rely on regime['state'] ONLY. The backtest passes
        {'state','date','one_hot','transition_probs'}; live passes
        {'state','vix_level','vix_percentile','regime_data','updated_at'}."""
        return None

    @abstractmethod
    def generate_signals(
        self,
        prices:   pd.DataFrame,   # wide: date × ticker closes
        regime:   dict,           # regime JSON
        universe: List[str],      # tickers to consider
        aux_data: dict = None,    # optional: financials, options, etc.
    ) -> List[Signal]:
        """
        Generate signals for today. Uses only data passed in — no external calls.
        Returns list of Signal objects. Empty list = no signals today.
        """
        raise NotImplementedError

    def compute_stops_and_targets(
        self,
        prices_series:  pd.Series,
        direction:      str,
        current_price:  float,
        bull_target:    float = None,
        bear_target:    float = None,
        atr_multiplier: float = 2.0,
        regime_state:   str   = 'LOW_VOL',
    ) -> dict:
        """Standard stop/target computation. Reusable across strategies."""
        diff = prices_series.diff().abs()
        _atr_raw = diff.rolling(14).mean().iloc[-1] if len(diff) >= 14 else float('nan')
        # Treat ATR=0 (e.g. stale/ffilled constant series) same as NaN — use 2% fallback.
        atr  = float(_atr_raw) if (pd.notna(_atr_raw) and float(_atr_raw) > 0) else current_price * 0.02

        # Scale ATR multiplier by regime to preserve R:R geometry in high-vol environments.
        # High vol inflates ATR-based stops without expanding fixed-% targets, killing EV.
        effective_atr_mult = atr_multiplier * REGIME_ATR_SCALE.get(regime_state, 1.0)

        if direction == 'LONG':
            stop = current_price - atr * effective_atr_mult
            t1   = current_price * 1.05
            t2   = current_price * 1.10
            t3   = bull_target or current_price * 1.20
        else:
            stop = current_price + atr * effective_atr_mult
            t1   = current_price * 0.95
            t2   = current_price * 0.90
            t3   = bear_target or current_price * 0.80

        return {
            'stop': round(stop, 4),
            't1':   round(t1,   4),
            't2':   round(t2,   4),
            't3':   round(t3,   4),
        }
