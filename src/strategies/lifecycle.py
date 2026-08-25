"""
Strategy Lifecycle State Machine  —  Deliverable B
FundJohn / OpenClaw v2.0

Formalises the candidate → paper → live → monitoring → deprecated → archived
strategy pipeline and consumes the decommissioned strategy archive created by R3.

Usage
-----
    from strategies.lifecycle import LifecycleStateMachine, StrategyState

    lsm = LifecycleStateMachine.from_manifest("/root/openclaw/src/strategies/manifest.json")

    # Promote a paper strategy to live (guards enforced)
    lsm.transition("S9_dual_momentum", StrategyState.LIVE,
                   actor="system", reason="Sharpe=0.72 DD=0.11",
                   metadata={"sharpe": 0.72, "max_drawdown": 0.11})
    lsm.save_manifest("/root/openclaw/src/strategies/manifest.json")
"""
from __future__ import annotations

import ast
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# SP-2 Phase D: canonical path to strategy implementation files.
# Hoist here so `register()` + `_read_strategy_requirements` both reference
# the same constant, and tests can monkeypatch it cleanly.
_IMPLEMENTATIONS_DIR = Path(__file__).resolve().parents[2] / "src" / "strategies" / "implementations"

# ─────────────────────────────────────────────────────────────────────────────
# 1.  States
# ─────────────────────────────────────────────────────────────────────────────

class StrategyState(str, Enum):
    """All lifecycle states a strategy can occupy."""
    STAGING    = "staging"     # awaiting data-pipeline setup before it can be a candidate
    CANDIDATE  = "candidate"   # proposed — no implementation required yet
    PAPER      = "paper"       # implemented — undergoing backtesting / paper trading
    LIVE       = "live"        # active in production execution
    MONITORING = "monitoring"  # live but under heightened observation
    DEPRECATED = "deprecated"  # removed from execution — pending archival review
    ARCHIVED   = "archived"    # permanently retired


# ─────────────────────────────────────────────────────────────────────────────
# 2.  Transition table
# ─────────────────────────────────────────────────────────────────────────────

# Keyed (from_state, to_state) → human description of the move.
#
# Lifecycle (post fused-staging-approval rewrite, 2026-04-27):
#   staging → candidate    fused worker: backfill + strategycoder + backtest
#   candidate → live       operator click; gated on sharpe/dd
#   PAPER is a frozen legacy state — no outbound transitions. Existing PAPER
#   manifest rows were migrated to CANDIDATE by scripts/migrate_paper_to_candidate.py.
VALID_TRANSITIONS: Dict[Tuple[StrategyState, StrategyState], str] = {
    (StrategyState.STAGING,    StrategyState.CANDIDATE):   "fused approval: backfill + strategycoder + backtest complete",
    (StrategyState.STAGING,    StrategyState.ARCHIVED):    "abandon before data is available",
    (StrategyState.CANDIDATE,  StrategyState.STAGING):     "regress — needs additional data sources",
    (StrategyState.CANDIDATE,  StrategyState.LIVE):        "promote to live after passing backtest guards",
    (StrategyState.CANDIDATE,  StrategyState.ARCHIVED):    "abandon without going live",
    # paper → archived: safety-valve for any legacy/orphan PAPER row that
    # missed the one-shot migration. No other outbound transitions from PAPER.
    (StrategyState.PAPER,      StrategyState.ARCHIVED):    "archive legacy paper row",
    (StrategyState.LIVE,       StrategyState.MONITORING):  "escalate to monitoring",
    (StrategyState.LIVE,       StrategyState.DEPRECATED):  "demote from live",
    # Auto-demote on universally-negative Sharpe (see auto_demote_negative_sharpe).
    # Bypasses the usual MONITORING intermediate — the engine demoted us
    # because every eligible regime is unprofitable; MasterMind weekly
    # review decides if the strategy can earn its way back.
    (StrategyState.LIVE,       StrategyState.CANDIDATE):   "auto-demote: negative Sharpe across all eligible regimes",
    (StrategyState.MONITORING, StrategyState.LIVE):        "restore confidence, back to live",
    (StrategyState.MONITORING, StrategyState.DEPRECATED):  "demote from monitoring",
    (StrategyState.MONITORING, StrategyState.CANDIDATE):   "auto-demote: negative Sharpe across all eligible regimes",
    (StrategyState.DEPRECATED, StrategyState.ARCHIVED):    "archive after review period",
}

# Backtest thresholds required for candidate → live promotion (formerly paper → live).
# Policy 2026-07-13 v2 (operator): Sharpe must STRICTLY EXCEED min_sharpe
# (0.0 → "positive Sharpe"); the authoritative gate is PER-REGIME-SLEEVE and
# lives in src/lib/promotion_service.js (sharpe>0 AND sleeve max-DD within the
# class ceiling AND sleeve trades >= 100, promoting into exactly the
# qualifying regimes). This python mirror judges caller-supplied totals
# defensively — keep the VALUES in sync with promotion_service.js.
CANDIDATE_TO_LIVE_MIN_SHARPE:   float = 0.0
CANDIDATE_TO_LIVE_MAX_DRAWDOWN: float = 0.20   # 20 %
CANDIDATE_TO_LIVE_MIN_TRADES:   int   = 100
# Back-compat aliases — old names still imported by some callers.
PAPER_TO_LIVE_MIN_SHARPE   = CANDIDATE_TO_LIVE_MIN_SHARPE
PAPER_TO_LIVE_MAX_DRAWDOWN = CANDIDATE_TO_LIVE_MAX_DRAWDOWN

# Calmar escape hatch on the DD leg (operator directive 2026-07-27): max
# drawdown is a running-max extreme that grows mechanically with backtest
# duration / trade count, so a flat ceiling systematically kills long-history
# high-breadth sleeves (momentum_12_1 LOW_VOL: Sharpe 2.62 on 4,759 trades,
# DD 26% — deactivated by the 20% ceiling). A sleeve whose Calmar (annualized
# return / max DD) clears MIN_CALMAR passes the DD leg up to the catastrophic
# hard cap. Both remain class-scaled; the hard cap keeps martingale-shaped
# curves (huge return, ruinous DD) out regardless of Calmar.
CANDIDATE_TO_LIVE_MIN_CALMAR:  float = 0.5   # DD no worse than 2x annualized return
CANDIDATE_TO_LIVE_DD_HARD_CAP: float = 0.50  # 50 % — catastrophic ceiling (equity)

# R1 (2026-08-24, five-repo-adoptions, benchmark-relative promotion
# criterion): a candidate/live sleeve must beat the regime-conditioned SPY
# baseline by at least this much excess Sharpe (sharpe > benchmark_sharpe +
# threshold) — applied ONLY when the sleeve carries a non-NULL
# benchmark_sharpe (fail-open on missing benchmark data; see
# backtest.benchmark_baseline / backtest.regime_qualification.qualifies_regime
# / src/lib/promotion_service.js judgeRegimeSleeve — all three must stay
# value-synced on this constant).
#
# Kept as its OWN class-keyed dict — mirroring PROMOTION_THRESHOLDS's
# per-class-override shape — rather than folded into PROMOTION_THRESHOLDS
# itself. Folding it in would change that dict's key set, which would break
# the exact-dict-equality assertions in
# tests/strategies/test_promotion_thresholds_per_class.py::test_thresholds_lookup
# and tests/execution/test_phase_d_crypto.py::test_crypto_promotion_threshold_present
# (both out of this task's file scope; verified failing against a
# PROMOTION_THRESHOLDS with an added key before choosing this shape). All
# four classes share the SAME 0.0 default for now; a class can be tuned
# independently later by editing its entry here (and the matching entry in
# promotion_service.js) without touching PROMOTION_THRESHOLDS at all.
MIN_EXCESS_SHARPE_VS_BENCHMARK: float = 0.0
MIN_EXCESS_SHARPE_VS_BENCHMARK_BY_CLASS: dict[str, float] = {
    "equity": MIN_EXCESS_SHARPE_VS_BENCHMARK,
    "etp":    MIN_EXCESS_SHARPE_VS_BENCHMARK,
    "option": MIN_EXCESS_SHARPE_VS_BENCHMARK,
    "crypto": MIN_EXCESS_SHARPE_VS_BENCHMARK,
}


def min_excess_sharpe_vs_benchmark(instrument_class: Optional[str]) -> float:
    """Per-class excess-Sharpe-over-benchmark floor for the R1 benchmark-
    relative promotion criterion. Unknown/None class falls back to equity
    (matches _promotion_threshold's own fallback convention)."""
    return MIN_EXCESS_SHARPE_VS_BENCHMARK_BY_CLASS.get(
        instrument_class or "equity", MIN_EXCESS_SHARPE_VS_BENCHMARK)


# SP-3/SP-4: per-instrument-class candidate→live thresholds. equity/etp keep the
# legacy values. option + crypto calibrated in SP-4 (2026-05-27), operator-signed-off.
PROMOTION_THRESHOLDS: dict[str, dict[str, float]] = {
    "equity": {"min_sharpe": CANDIDATE_TO_LIVE_MIN_SHARPE,
               "max_drawdown": CANDIDATE_TO_LIVE_MAX_DRAWDOWN,
               "min_trades": CANDIDATE_TO_LIVE_MIN_TRADES,
               "min_calmar": CANDIDATE_TO_LIVE_MIN_CALMAR,
               "dd_hard_cap": CANDIDATE_TO_LIVE_DD_HARD_CAP},
    "etp":    {"min_sharpe": CANDIDATE_TO_LIVE_MIN_SHARPE,
               "max_drawdown": CANDIDATE_TO_LIVE_MAX_DRAWDOWN,
               "min_trades": CANDIDATE_TO_LIVE_MIN_TRADES,
               "min_calmar": CANDIDATE_TO_LIVE_MIN_CALMAR,
               "dd_hard_cap": CANDIDATE_TO_LIVE_DD_HARD_CAP},
    # option/crypto keep their looser DD ceilings (VIX-anchored SYNTHETIC
    # options engine — SP-4 2026-05-27; BTC is a 60-80% DD asset — SP-3.1
    # 2026-05-26). The Sharpe floor is the shared strictly-positive 0.0 as of
    # policy 2026-07-13 v2; the option engine's uncertainty is carried by the
    # activation min-Sharpe slider, not the entry gate.
    "option": {"min_sharpe": 0.0,
               "max_drawdown": 0.30,
               "min_trades": CANDIDATE_TO_LIVE_MIN_TRADES,
               "min_calmar": CANDIDATE_TO_LIVE_MIN_CALMAR,
               "dd_hard_cap": 0.60},
    "crypto": {"min_sharpe": 0.0,
               "max_drawdown": 0.70,
               "min_trades": CANDIDATE_TO_LIVE_MIN_TRADES,
               "min_calmar": CANDIDATE_TO_LIVE_MIN_CALMAR,
               "dd_hard_cap": 0.85},
}


def _promotion_threshold(instrument_class: str) -> dict[str, float]:
    return PROMOTION_THRESHOLDS.get(
        instrument_class,
        {"min_sharpe": CANDIDATE_TO_LIVE_MIN_SHARPE,
         "max_drawdown": CANDIDATE_TO_LIVE_MAX_DRAWDOWN,
         "min_trades": CANDIDATE_TO_LIVE_MIN_TRADES,
         "min_calmar": CANDIDATE_TO_LIVE_MIN_CALMAR,
         "dd_hard_cap": CANDIDATE_TO_LIVE_DD_HARD_CAP})


# SP-3 instrument-class taxonomy. VALID = accepted by validation;
# ROUTED = has a live sizer/backtest handler. crypto was routed in SP-3.1
# Phase D; futures is reserved (valid) but unhandled.
VALID_INSTRUMENT_CLASSES  = frozenset({"equity", "option", "etp", "crypto", "futures"})
ROUTED_INSTRUMENT_CLASSES = frozenset({"equity", "option", "etp", "crypto"})


# ─────────────────────────────────────────────────────────────────────────────
# 3.  Data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TransitionEvent:
    from_state: str
    to_state:   str
    timestamp:  str
    actor:      str    # "system" | "manual" | agent-name
    reason:     str
    metadata:   dict = field(default_factory=dict)

    # Accepted aliases for hand-written / script-written history rows. A single
    # malformed row must NOT be able to brick from_manifest() — that throws
    # inside the engine's instrument_class_for() and silently starves EVERY
    # strategy (near-miss 2026-07-17). from_row normalizes known aliases,
    # drops unknown keys, and returns None for an unsalvageable row.
    _ALIASES = {'at': 'timestamp', 'from': 'from_state', 'to': 'to_state', 'ts': 'timestamp'}
    _FIELDS = {'from_state', 'to_state', 'timestamp', 'actor', 'reason', 'metadata'}

    @classmethod
    def from_row(cls, e: dict) -> Optional["TransitionEvent"]:
        if not isinstance(e, dict):
            logger.warning("dropping non-dict history row: %r", e)
            return None
        norm = {}
        for k, v in e.items():
            key = cls._ALIASES.get(k, k)
            if key in cls._FIELDS:
                norm[key] = v
        try:
            return cls(
                from_state=norm.get('from_state', ''),
                to_state=norm.get('to_state', ''),
                timestamp=norm.get('timestamp', ''),
                actor=norm.get('actor', ''),
                reason=norm.get('reason', ''),
                metadata=norm.get('metadata', {}) or {},
            )
        except Exception as exc:  # never let a history row abort a manifest load
            logger.warning("dropping unparseable history row %r: %s", e, exc)
            return None


@dataclass
class StrategyRecord:
    strategy_id: str
    state:       StrategyState
    state_since: str
    history:     List[TransitionEvent] = field(default_factory=list)
    metadata:    dict                  = field(default_factory=dict)
    # Phase 2C cleanup (2026-05-12): `eligible_regimes` was a top-level
    # manifest field carrying per-strategy regime-eligibility lists.
    # Phase 2A moved that data into strategy_regime_params (DB); the field
    # is no longer authoritative. The field is preserved as inert
    # round-trip carry-over below (read on load, written on save) so any
    # lingering manifest writes survive until the cleanup script
    # `scripts/cleanup_manifest_eligibility_field.py` is run. After that
    # script removes the field from manifest.json, both the dataclass
    # attribute and the from_manifest/to_dict plumbing can be deleted in
    # a follow-up commit.
    eligible_regimes: Optional[List[str]] = None
    universe_filter_ref: Optional[str] = None   # SP-2 Phase A — predicate import path "mod.path:attr"
    instrument_class: str = "equity"   # SP-3 — equity|option|etp (crypto|futures reserved)


# ─────────────────────────────────────────────────────────────────────────────
# 4.  Exceptions
# ─────────────────────────────────────────────────────────────────────────────

class LifecycleError(Exception):
    """Raised when a requested state transition is invalid or blocked by a guard."""


# ─────────────────────────────────────────────────────────────────────────────
# 5.  Sandbox predicate check (SP-2 Phase A)
# ─────────────────────────────────────────────────────────────────────────────

def _sandbox_check_predicate(predicate_ref: "str | None") -> None:
    """SP-2 Phase A: evaluate a candidate universe_filter under real clock
    and again under a frozen clock pinned to 2020-01-01; reject if outputs
    differ.

    Defends against predicates that read system clock / env vars (silent
    look-ahead bias). Linted at write time; sandbox-checked at transition.
    """
    if predicate_ref is None:
        return
    import importlib
    import datetime as _dt

    # Lazy import — try both import roots (src.strategies.* and strategies.*)
    try:
        from src.strategies.universe_meta import TickerMetadata
    except ImportError:
        from strategies.universe_meta import TickerMetadata  # type: ignore

    fixture = TickerMetadata(
        symbol="AAPL", asset_class="us_equity", exchange="NASDAQ",
        status="active", tradable=True, shortable=True,
        fractionable=True, easy_to_borrow=True,
        market_cap=3.5e12, adv_usd_20d=1.8e10,
        sector="IT", industry="CE",
        options_eligible=True, in_sp500=True, in_r1000=True, in_r3000=True,
        listed_date=_dt.date(1980, 12, 12), delisted_date=None,
    )

    mod_path, attr = predicate_ref.rsplit(":", 1)
    module = importlib.import_module(mod_path)
    fn = getattr(module, attr)
    as_of = _dt.date(2024, 1, 15)  # fixed historical date

    real = fn(fixture, as_of)

    FROZEN = _dt.datetime(2020, 1, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)

    class _FrozenDate(_dt.date):
        @classmethod
        def today(cls):
            return cls(2020, 1, 1)

    class _FrozenDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return FROZEN if tz is None else FROZEN.astimezone(tz)
        @classmethod
        def utcnow(cls):
            return FROZEN.replace(tzinfo=None)

    # Patch every module-level name that resolves to datetime.date or
    # datetime.datetime — this catches aliased imports like
    # `from datetime import date as _d` where the attr name differs.
    saved = {}
    for attr_name, attr_val in list(vars(module).items()):
        if attr_val is _dt.date and attr_val is not _dt.datetime:
            saved[attr_name] = attr_val
            setattr(module, attr_name, _FrozenDate)
        elif attr_val is _dt.datetime:
            saved[attr_name] = attr_val
            setattr(module, attr_name, _FrozenDateTime)
    try:
        frozen = fn(fixture, as_of)
    finally:
        for attr_name, val in saved.items():
            setattr(module, attr_name, val)

    if real != frozen:
        raise ValueError(
            f"predicate behavior differs between real ({real}) and frozen ({frozen}) clock — "
            f"likely reads today()/now()/env. Predicate: {predicate_ref}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 5b. SP-2 Phase D — AST-based predicate detection
# ─────────────────────────────────────────────────────────────────────────────

def _detect_module_predicate(file_path) -> "str | None":
    """Return the predicate name if the strategy file has a top-level
    ``from src.strategies.universe_default import <name> as universe_filter``
    (or the aliased ``strategies.universe_default`` form), else None.

    Returns None on FileNotFoundError, SyntaxError, or any OSError — so
    callers never need to guard against missing / unreadable files.
    """
    try:
        tree = ast.parse(Path(file_path).read_text())
    except (FileNotFoundError, SyntaxError, OSError):
        return None
    for node in tree.body:   # top-level only
        if isinstance(node, ast.ImportFrom) and node.module in (
            "src.strategies.universe_default",
            "strategies.universe_default",
        ):
            for alias in node.names:
                if alias.asname == "universe_filter":
                    return alias.name
    return None


def _detect_module_instrument_class(file_path) -> "str | None":
    """Return the instrument_class if the strategy file has a top-level
    ``INSTRUMENT_CLASS = "<class>"`` assignment whose value is in
    VALID_INSTRUMENT_CLASSES, else None. Mirrors _detect_module_predicate;
    returns None on FileNotFoundError / SyntaxError / OSError.
    """
    try:
        tree = ast.parse(Path(file_path).read_text())
    except (FileNotFoundError, SyntaxError, OSError):
        return None
    for node in tree.body:   # top-level only
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "INSTRUMENT_CLASS" in targets and isinstance(node.value, ast.Constant):
                val = node.value.value
                if isinstance(val, str) and val in VALID_INSTRUMENT_CLASSES:
                    return val
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 6.  State machine
# ─────────────────────────────────────────────────────────────────────────────

class LifecycleStateMachine:
    """
    Manages strategy lifecycle state transitions.

    All state changes are recorded in the per-strategy history.  The manifest
    can be serialised to / deserialised from JSON so state survives restarts.
    """

    def __init__(
        self,
        records:       Dict[str, StrategyRecord],
        decommissioned: Optional[Dict] = None,
    ) -> None:
        self._records: Dict[str, StrategyRecord] = records
        self.decommissioned: Dict = decommissioned or {}

    # ── factory ──────────────────────────────────────────────────────────────

    @classmethod
    def from_manifest(cls, path: str | Path) -> "LifecycleStateMachine":
        """Load state machine from a manifest JSON file.

        Self-healing guard: any entry under m['decommissioned'] whose
        state is actually one of {live, candidate, staging, monitoring}
        is treated as a misrouted active strategy and lifted into
        _records. Sat 2026-05-16 incident: 17 newly-coded strategies
        ended up under decommissioned with state='candidate', invisible
        to eligibility_assigner / strategy_weights / pipeline_orchestrator.
        Until the upstream writer is found, this guard prevents the
        invariant violation from sticking across a round-trip.
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Manifest not found: {p}")
        data = json.loads(p.read_text())
        records: Dict[str, StrategyRecord] = {}
        for sid, rec in data.get("strategies", {}).items():
            _ic = rec.get("instrument_class", "equity")
            if _ic not in VALID_INSTRUMENT_CLASSES:
                raise ValueError(
                    f"strategy {sid!r}: unknown instrument_class {_ic!r}; "
                    f"valid={sorted(VALID_INSTRUMENT_CLASSES)}")
            history = [ev for ev in (TransitionEvent.from_row(e) for e in rec.get("history", [])) if ev is not None]
            records[sid] = StrategyRecord(
                strategy_id=sid,
                state=StrategyState(rec["state"]),
                state_since=rec["state_since"],
                history=history,
                metadata=rec.get("metadata", {}),
                eligible_regimes=rec.get("eligible_regimes"),
                universe_filter_ref=rec.get("metadata", {}).get("universe_filter_ref"),
                instrument_class=_ic,
            )
        decom_raw = data.get("decommissioned", {}) or {}
        decom_clean: Dict = {}
        for sid, rec in decom_raw.items():
            state = (rec or {}).get('state', 'decommissioned')
            if state in {'live', 'candidate', 'staging', 'monitoring'} and sid not in records:
                # Misrouted active strategy — recover into _records.
                try:
                    history = [ev for ev in (TransitionEvent.from_row(e) for e in rec.get("history", [])) if ev is not None]
                    records[sid] = StrategyRecord(
                        strategy_id=sid,
                        state=StrategyState(state),
                        state_since=rec.get("state_since")
                                    or datetime.now(timezone.utc).isoformat(),
                        history=history,
                        metadata=rec.get("metadata", {}),
                        eligible_regimes=rec.get("eligible_regimes"),
                        universe_filter_ref=rec.get("metadata", {}).get("universe_filter_ref"),
                        instrument_class=rec.get("instrument_class", "equity"),
                    )
                    logger.warning(
                        "lifecycle: rescued misrouted active strategy %s "
                        "(state=%s) from decommissioned -> strategies",
                        sid, state,
                    )
                    continue
                except Exception:
                    # If reconstruction fails for any reason, keep the entry
                    # under decommissioned so we don't lose data.
                    pass
            decom_clean[sid] = rec
        return cls(records, decom_clean)

    @classmethod
    def new_empty(cls) -> "LifecycleStateMachine":
        """Create a blank state machine with no strategies registered."""
        return cls({})

    # ── querying ─────────────────────────────────────────────────────────────

    def get_state(self, strategy_id: str) -> StrategyState:
        rec = self._records.get(strategy_id)
        if rec is None:
            raise KeyError(f"Unknown strategy: {strategy_id!r}")
        return rec.state

    def get_record(self, strategy_id: str) -> StrategyRecord:
        rec = self._records.get(strategy_id)
        if rec is None:
            raise KeyError(f"Unknown strategy: {strategy_id!r}")
        return rec

    def list_in_state(self, state: StrategyState) -> List[str]:
        """Return all strategy IDs currently in *state*."""
        return [sid for sid, rec in self._records.items() if rec.state == state]

    def all_states(self) -> Dict[str, str]:
        """Return {strategy_id: state_value} for every registered strategy."""
        return {sid: rec.state.value for sid, rec in self._records.items()}

    def summary(self) -> Dict[str, List[str]]:
        """Return {state_value: [strategy_ids]} grouped by state."""
        out: Dict[str, List[str]] = {s.value: [] for s in StrategyState}
        for sid, rec in self._records.items():
            out[rec.state.value].append(sid)
        return out

    def is_registered(self, strategy_id: str) -> bool:
        return strategy_id in self._records

    # ── transitions ──────────────────────────────────────────────────────────

    def validate_regime_eligibility_present(self, strategy_id: str) -> Tuple[bool, str]:
        """Block candidate→staging if eligible_regimes is missing or empty.

        Auto-derives from backtest_results.eligible_regimes_proposed if available;
        otherwise raises requires_regime_qualification.

        Spec: docs/archive/superpowers/specs/2026-05-11-regime-blended-position-sizing-design.md
        §"Strategy creation pipeline changes – A. PaperHunter → StrategyCoder → Promotion"
        """
        record = self._records.get(strategy_id)
        if not record:
            return False, f'unknown strategy {strategy_id}'

        metadata = record.metadata or {}
        eligible = metadata.get('eligible_regimes')
        if eligible:  # Non-empty list
            return True, ''

        # No explicit eligible_regimes — try auto-derive from backtest_results
        backtest_results = metadata.get('backtest_results')
        if not backtest_results:
            return False, 'requires_regime_qualification: no backtest_results in metadata'

        proposed = backtest_results.get('eligible_regimes_proposed', [])
        if not proposed:
            return False, 'requires_regime_qualification: no regime qualifies under thresholds'

        # Mutate metadata to write the derived eligibility
        record.metadata['eligible_regimes'] = proposed
        return True, ''

    def can_transition(
        self,
        strategy_id: str,
        to_state:    StrategyState,
        metadata:    Optional[dict] = None,
    ) -> Tuple[bool, str]:
        """
        Check whether a transition is valid without executing it.

        Returns
        -------
        (ok, message)
            ok=True means the transition is allowed; message describes why or
            why not.
        """
        rec = self._records.get(strategy_id)
        if rec is None:
            return False, f"Unknown strategy: {strategy_id!r}"

        key = (rec.state, to_state)
        if key not in VALID_TRANSITIONS:
            allowed = [t.value for (f, t) in VALID_TRANSITIONS if f == rec.state]
            return False, (
                f"No valid path from '{rec.state.value}' to '{to_state.value}'. "
                f"Valid destinations from '{rec.state.value}': {allowed}"
            )

        # Guard: candidate → live requires backtest thresholds
        if key == (StrategyState.CANDIDATE, StrategyState.LIVE):
            md = metadata or {}
            sharpe   = md.get("sharpe")
            drawdown = md.get("max_drawdown")
            if sharpe is None or drawdown is None:
                return False, (
                    "candidate→live requires metadata keys 'sharpe' and 'max_drawdown'"
                )
            thr = _promotion_threshold(rec.instrument_class)
            # Policy 2026-07-13 v2: sharpe must STRICTLY EXCEED min_sharpe
            # (0.0 → positive-Sharpe gate). The authoritative per-regime gate
            # lives in promotion_service.js; this guard judges the totals the
            # caller supplies.
            if sharpe <= thr["min_sharpe"]:
                return False, (
                    f"candidate→live blocked: sharpe {sharpe:.2f} <= "
                    f"minimum {thr['min_sharpe']} (instrument_class={rec.instrument_class})")
            if drawdown > thr["max_drawdown"]:
                return False, (
                    f"candidate→live blocked: max_drawdown {drawdown:.2%} > "
                    f"limit {thr['max_drawdown']:.0%} (instrument_class={rec.instrument_class})")
            trades = md.get("trades")
            if trades is not None and trades < thr["min_trades"]:
                return False, (
                    f"candidate→live blocked: trades {trades} < "
                    f"minimum {thr['min_trades']} (instrument_class={rec.instrument_class})")

            # R1-assigners (2026-08-25): benchmark-relative leg, ANDed on top
            # of the legacy checks above -- every one of them already passed
            # if execution reaches this line, so it can only ever TIGHTEN
            # this guard, never loosen it. This guard judges CALLER-SUPPLIED
            # AGGREGATE totals (no per-regime sleeve here, unlike
            # promotion_service.js's judgeRegimeSleeve / activation_assigner
            # / eligibility_assigner), so there is no real 'regime' to name
            # in the [bench_gate] log line -- 'aggregate' names this call
            # site instead. benchmark_sharpe is an OPTIONAL metadata key; no
            # existing caller/test supplies one, so this fails open
            # (skipped) for all of them today, matching every other R1
            # consumer's fail-open contract on missing benchmark data.
            # Deferred import: regime_qualification.py imports FROM this
            # module at its own top level (_promotion_threshold,
            # min_excess_sharpe_vs_benchmark) -- a module-level import here
            # would be circular depending on which module a caller imports
            # first, so it's resolved lazily at call time instead, by which
            # point both modules are always already fully loaded.
            from backtest.regime_qualification import (
                benchmark_leg_passes, log_bench_gate_skip, log_bench_gate_verdict)
            bench_sharpe = md.get("benchmark_sharpe")
            bench_pass, bench_reason = benchmark_leg_passes(
                sharpe, bench_sharpe, rec.instrument_class)
            if bench_reason == 'skipped_null_benchmark':
                log_bench_gate_skip(logger.info, strategy_id, 'aggregate')
            log_bench_gate_verdict(logger.info, strategy_id, 'aggregate',
                                   True, bench_pass, sharpe, bench_sharpe)
            if not bench_pass:
                return False, (
                    f"candidate→live blocked: sharpe {sharpe:.2f} does not exceed "
                    f"benchmark_sharpe {bench_sharpe} + minimum excess "
                    f"{min_excess_sharpe_vs_benchmark(rec.instrument_class)} "
                    f"(instrument_class={rec.instrument_class})")

        # Guard: candidate → staging requires regime eligibility
        # Spec: docs/archive/superpowers/specs/2026-05-11-regime-blended-position-sizing-design.md
        # §"Strategy creation pipeline changes"
        if key == (StrategyState.CANDIDATE, StrategyState.STAGING):
            ok, reason = self.validate_regime_eligibility_present(strategy_id)
            if not ok:
                return False, reason

        return True, VALID_TRANSITIONS[key]

    def transition(
        self,
        strategy_id: str,
        to_state:    StrategyState,
        actor:       str = "system",
        reason:      str = "",
        metadata:    Optional[dict] = None,
    ) -> StrategyRecord:
        """
        Execute a lifecycle transition.

        Raises LifecycleError when the move is invalid or blocked by a guard.
        Raises ValueError when a candidate universe_filter_ref predicate reads
        the system clock (look-ahead bias detection via sandbox check).
        Returns the updated StrategyRecord.
        """
        # SP-2 Phase A: sandbox-check any new universe_filter_ref BEFORE running
        # can_transition / mutating any record state. Sandbox check raises
        # ValueError on clock-dependent predicates.
        new_ref = (metadata or {}).get("universe_filter_ref")
        if new_ref is not None:
            # Only check if the ref is actually changing; idempotent set is fine
            current = self._records.get(strategy_id)
            current_ref = current.universe_filter_ref if current else None
            if new_ref != current_ref:
                _sandbox_check_predicate(new_ref)

        ok, msg = self.can_transition(strategy_id, to_state, metadata)
        if not ok:
            raise LifecycleError(msg)

        rec = self._records[strategy_id]
        now = datetime.now(timezone.utc).isoformat()
        event = TransitionEvent(
            from_state=rec.state.value,
            to_state=to_state.value,
            timestamp=now,
            actor=actor,
            reason=reason or msg,
            metadata=metadata or {},
        )
        rec.history.append(event)
        rec.state       = to_state
        rec.state_since = now

        # SP-2 Phase A: apply the new universe_filter_ref after transition succeeds
        if new_ref is not None:
            rec.universe_filter_ref = new_ref
            # Mirror into rec.metadata so to_dict round-trips it
            rec.metadata["universe_filter_ref"] = new_ref

        logger.info(
            "lifecycle[%s]: %s → %s  actor=%s",
            strategy_id, event.from_state, to_state.value, actor,
        )
        self._persist_lifecycle_event(strategy_id, event, metadata)

        # 2026-04-28: orphan-column removal removed. The master database is
        # append-only — strategies that get deprecated/archived no longer
        # trigger column drops in data_columns or schema_registry. Past
        # data stays forever; future strategies can opt into any column
        # already collected. See CLAUDE.md "NEVER DELETE FROM THE MASTER
        # DATABASE" core invariant.

        return rec

    def _persist_lifecycle_event(
        self,
        strategy_id: str,
        event: "TransitionEvent",
        metadata: Optional[dict],
    ) -> None:
        """Write to lifecycle_events Postgres table; fail silently if unavailable."""
        import os
        postgres_uri = os.environ.get("POSTGRES_URI")
        if not postgres_uri:
            return
        try:
            import psycopg2
            import json as _json
            conn = psycopg2.connect(postgres_uri)
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO lifecycle_events
                           (strategy_id, from_state, to_state, actor, reason, metadata)
                           VALUES (%s, %s, %s, %s, %s, %s)""",
                        (
                            strategy_id,
                            event.from_state,
                            event.to_state,
                            event.actor,
                            event.reason,
                            _json.dumps(metadata or {}),
                        ),
                    )
            conn.close()
        except Exception as exc:
            logger.debug("lifecycle_events insert skipped: %s", exc)

    # ── unstack helper ───────────────────────────────────────────────────────

    # Strategy states that count as "still consuming data". A column is
    # orphaned (and gets removed inline on unstack) only when no remaining
    # strategy in any of these states declares it as a requirement.
    #
    # NB: PAPER is intentionally absent. After the fused-approval rewrite
    # (2026-04-27), PAPER is a frozen legacy state — every former PAPER row
    # was migrated to CANDIDATE. Treating PAPER as a live consumer here
    # would block legitimate cleanups if any orphan PAPER row slips through.
    _ACTIVE_KEEP_STATES = (
        StrategyState.LIVE,
        StrategyState.MONITORING,
        StrategyState.CANDIDATE,
        StrategyState.STAGING,
    )

    def _read_strategy_requirements(self, sid: str) -> set[str]:
        """Return the union of required + optional columns from
        ``<canonical_file>.requirements.json`` for *sid*. Empty set if the
        strategy has no requirements.json on disk.
        """
        import json as _json
        req_dir = _IMPLEMENTATIONS_DIR
        rec = self._records.get(sid)
        canonical = None
        if rec and rec.metadata:
            canonical = rec.metadata.get("canonical_file")
        base = (canonical or f"{sid.lower()}.py").replace(".py", "")
        p = req_dir / f"{base}.requirements.json"
        if not p.exists():
            return set()
        try:
            j = _json.loads(p.read_text())
        except Exception:
            return set()
        return set(j.get("required") or []) | set(j.get("optional") or [])

    # ── registration ─────────────────────────────────────────────────────────

    def register(
        self,
        strategy_id:   str,
        initial_state: StrategyState = StrategyState.CANDIDATE,
        metadata:      Optional[dict] = None,
    ) -> StrategyRecord:
        """Add a new strategy to the lifecycle at *initial_state*."""
        if strategy_id in self._records:
            raise LifecycleError(f"Strategy {strategy_id!r} is already registered")
        now = datetime.now(timezone.utc).isoformat()
        rec = StrategyRecord(
            strategy_id=strategy_id,
            state=initial_state,
            state_since=now,
            metadata=metadata or {},
        )
        # SP-2 Phase D: detect and persist the universe predicate from the
        # strategy's implementation file, gated by OPENCLAW_PHASE_D_PREDICATE_AT_MINT=1.
        # The caller's existing save_manifest() call then persists it into
        # metadata.universe_filter_ref via to_dict (no second save needed here).
        if os.environ.get("OPENCLAW_PHASE_D_PREDICATE_AT_MINT") == "1":
            canonical = (metadata or {}).get("canonical_file")
            if canonical:
                try:
                    from src.strategies.universe_default import CANDIDATE_PREDICATES as _CP
                except ImportError:
                    from strategies.universe_default import CANDIDATE_PREDICATES as _CP  # type: ignore
                impl_path = _IMPLEMENTATIONS_DIR / canonical
                detected = _detect_module_predicate(impl_path)
                if detected and detected in _CP:
                    rec.universe_filter_ref = f"src.strategies.universe_default:{detected}"
        # SP-4: detect and persist instrument_class from the impl file, gated
        # by OPENCLAW_SP4_INSTRUMENT_CLASS_AT_MINT=1. Covers the rare
        # register()-creates-the-record path (StrategyCoder normally writes the
        # manifest entry directly, which from_manifest already reads).
        if os.environ.get("OPENCLAW_SP4_INSTRUMENT_CLASS_AT_MINT") == "1":
            canonical = (metadata or {}).get("canonical_file")
            if canonical:
                detected_ic = _detect_module_instrument_class(_IMPLEMENTATIONS_DIR / canonical)
                if detected_ic:
                    rec.instrument_class = detected_ic
        self._records[strategy_id] = rec
        return rec

    def register_strategy_predicate(
        self,
        strategy_id:    str,
        predicate_name: "str | None",
        path:           "str | Path",
    ) -> None:
        """Set (or clear) the universe predicate for an already-registered strategy.

        Parameters
        ----------
        strategy_id:
            Must already be in the manifest.
        predicate_name:
            A name from ``CANDIDATE_PREDICATES`` (e.g. ``"large_cap"``), or
            ``None`` to clear the predicate.
        path:
            Manifest file path.  Saved via the blessed lock+merge path so
            concurrent JS writers are not clobbered.

        Raises
        ------
        ValueError
            If *predicate_name* is not in ``CANDIDATE_PREDICATES`` (unless None).
        ValueError
            If *strategy_id* is not in the manifest.
        """
        try:
            from src.strategies.universe_default import CANDIDATE_PREDICATES
        except ImportError:
            from strategies.universe_default import CANDIDATE_PREDICATES  # type: ignore

        if predicate_name is not None and predicate_name not in CANDIDATE_PREDICATES:
            raise ValueError(
                f"predicate '{predicate_name}' not in candidate set; "
                f"valid names: {sorted(CANDIDATE_PREDICATES)}"
            )

        record = self._records.get(strategy_id)
        if record is None:
            raise ValueError(f"strategy {strategy_id} not in manifest")

        if predicate_name is not None:
            record.universe_filter_ref = f"src.strategies.universe_default:{predicate_name}"
        else:
            record.universe_filter_ref = None

        self.save_manifest(path)

    # ── persistence ──────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        strategies = {}
        for sid, rec in self._records.items():
            metadata_out = dict(rec.metadata)  # shallow copy
            if rec.universe_filter_ref is not None:
                metadata_out["universe_filter_ref"] = rec.universe_filter_ref
            else:
                metadata_out.pop("universe_filter_ref", None)
            entry = {
                "state":       rec.state.value,
                "state_since": rec.state_since,
                "metadata":    metadata_out,
                "history": [
                    {
                        "from_state": e.from_state,
                        "to_state":   e.to_state,
                        "timestamp":  e.timestamp,
                        "actor":      e.actor,
                        "reason":     e.reason,
                        "metadata":   e.metadata,
                    }
                    for e in rec.history
                ],
            }
            # Preserve top-level eligible_regimes across the round-trip. If
            # we omit None entries the round-trip is a no-op for legacy
            # strategies that never had the field.
            if rec.eligible_regimes is not None:
                entry["eligible_regimes"] = rec.eligible_regimes
            # SP-3: always-emit (default 'equity') so legacy records backfill
            # on first write. Unlike eligible_regimes, this field is never omitted.
            entry["instrument_class"] = rec.instrument_class
            strategies[sid] = entry
        return {
            "schema_version":  "1.0",
            "updated_at":      datetime.now(timezone.utc).isoformat(),
            "strategies":      strategies,
            "decommissioned":  self.decommissioned,
        }

    def save_manifest(self, path: str | Path) -> None:
        """Save under cross-process lock + atomic rename.

        The lock prevents interleaved writes when this method runs in
        parallel with the JS writers (saturday_brain.js, finisher,
        approvals). The merge-with-disk strategy prevents the lost-update
        problem: if another writer added a strategy between our
        from_manifest() read and this save() call, we keep their entry —
        our snapshot's strategies override only the entries we explicitly
        modified.

        Last-writer-wins on the same strategy_id (mirrors SQL UPDATE
        semantics).
        """
        # Lazy import so a missing _manifest_lock module doesn't break
        # callers that don't actually save (e.g. read-only inspection).
        try:
            from . import _manifest_lock as _ml
        except ImportError:
            import importlib.util, sys
            spec = importlib.util.spec_from_file_location(
                "_manifest_lock",
                str(Path(__file__).parent / "_manifest_lock.py"),
            )
            _ml = importlib.util.module_from_spec(spec)  # type: ignore
            spec.loader.exec_module(_ml)  # type: ignore

        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        my_view = self.to_dict()

        def _merge(disk: dict) -> dict:
            disk_strategies = (disk or {}).get("strategies") or {}
            disk_decom      = (disk or {}).get("decommissioned") or {}
            # Start from disk; our entries override on conflict. Strategies
            # only-on-disk are preserved (concurrent writer additions).
            merged_strategies = {**disk_strategies, **my_view["strategies"]}
            merged_decom      = {**disk_decom, **my_view["decommissioned"]}
            return {
                "schema_version":  my_view.get("schema_version", "1.0"),
                "updated_at":      my_view["updated_at"],
                "strategies":      merged_strategies,
                "decommissioned":  merged_decom,
            }

        _ml.with_manifest_lock(p, _merge, actor="lifecycle.save_manifest")
        logger.info("Manifest saved to %s", p)


# ─────────────────────────────────────────────────────────────────────────────
# Module-level helper: auto-demote strategies that are negative across every
# eligible regime. Called by execution.strategy_weights.rebuild() after a
# fresh weights computation. Demotion target: candidate (back to research).
# ─────────────────────────────────────────────────────────────────────────────
def _sync_registry_demotion(sid: str, status: str = "pending_approval") -> None:
    """Close the engine's trade gate for a demoted strategy.

    The engine trades on strategy_registry.status='approved', NOT on the
    manifest state. Promotion is registry-first (promotion_service.js →
    registry_sync.js writes the registry, then the manifest); until 2026-08-18
    this Python demote path wrote ONLY the manifest, so every auto-demotion of
    a promoted strategy stranded a stale 'approved' row — 15 accumulated by
    08-18, each one an open trade gate waiting on an eligibility re-arm.

    'pending_approval' mirrors REGISTRY_STATUS_FOR[candidate] in
    src/lib/promotion_service.js — keep the two in sync. A missing registry
    row is fine (UPDATE matches 0 rows): there is no gate to close.
    """
    import psycopg2
    with psycopg2.connect(os.environ["POSTGRES_URI"]) as conn, conn.cursor() as cur:
        cur.execute("UPDATE strategy_registry SET status = %s WHERE id = %s",
                    (status, sid))


def auto_demote_negative_sharpe(
    reason: str = "auto_demote_negative_sharpe",
    actor:  str = "strategy_weights_engine",
    manifest_path: str | None = None,
) -> list[str]:
    """Demote any live/monitoring strategy whose effective_sharpe ≤ 0 across
    EVERY regime in its eligible_regimes list. Returns the list of demoted
    strategy_ids. A strategy with at least one positive-Sharpe eligible
    regime stays live (excluded from the bad regimes via weight=0 rows).

    Registry-first (2026-08-18): each demotion closes the registry trade gate
    BEFORE touching the manifest, mirroring promotion_service.js. If the
    registry sync fails the manifest demotion for that sid is SKIPPED (it
    retries at the next weights rebuild) — never the half-state where the
    dashboard shows candidate while the engine's gate is still open. The
    inverse half-state (registry closed, manifest still live) is the safe
    drift direction and surfaces as the ⚠️ "not trading" badge.
    """
    # Lazy import to avoid a cycle at module load (strategy_weights imports
    # from this module too).
    import sys as _sys
    _THIS_FILE = Path(__file__).resolve()
    _SRC = _THIS_FILE.parents[1]
    if str(_SRC) not in _sys.path:
        _sys.path.insert(0, str(_SRC))
    from execution.strategy_weights import find_negative_across_all_eligible

    targets = find_negative_across_all_eligible()
    if not targets:
        return []

    path = manifest_path or str(_THIS_FILE.parent / "manifest.json")
    lsm = LifecycleStateMachine.from_manifest(path)
    demoted: list[str] = []
    for sid in targets:
        try:
            _sync_registry_demotion(sid)
        except Exception as e:
            logger.warning("auto_demote: %s — registry sync FAILED, manifest "
                           "demotion skipped this pass (retries next rebuild): %s",
                           sid, e)
            continue
        try:
            lsm.transition(sid, StrategyState.CANDIDATE, actor=actor, reason=reason)
            demoted.append(sid)
        except LifecycleError as e:
            logger.warning("auto_demote: %s — lifecycle blocked (registry gate "
                           "already closed; shows as 'not trading' badge): %s", sid, e)
        except Exception as e:
            logger.warning("auto_demote: %s — error (registry gate already "
                           "closed; shows as 'not trading' badge): %s", sid, e)
    if demoted:
        lsm.save_manifest(path)
        logger.info("auto_demote: demoted %d strategies: %s", len(demoted), demoted)
    return demoted
