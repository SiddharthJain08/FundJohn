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

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# 1.  States
# ─────────────────────────────────────────────────────────────────────────────

class StrategyState(str, Enum):
    """All lifecycle states a strategy can occupy."""
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
VALID_TRANSITIONS: Dict[Tuple[StrategyState, StrategyState], str] = {
    (StrategyState.CANDIDATE,  StrategyState.PAPER):       "begin backtesting",
    (StrategyState.CANDIDATE,  StrategyState.ARCHIVED):    "abandon before implementation",
    (StrategyState.PAPER,      StrategyState.LIVE):        "promote to live after passing backtest guards",
    (StrategyState.PAPER,      StrategyState.CANDIDATE):   "regress — failed backtest",
    (StrategyState.PAPER,      StrategyState.ARCHIVED):    "archive without going live",
    (StrategyState.LIVE,       StrategyState.MONITORING):  "escalate to monitoring",
    (StrategyState.LIVE,       StrategyState.DEPRECATED):  "demote from live",
    (StrategyState.MONITORING, StrategyState.LIVE):        "restore confidence, back to live",
    (StrategyState.MONITORING, StrategyState.DEPRECATED):  "demote from monitoring",
    (StrategyState.DEPRECATED, StrategyState.ARCHIVED):    "archive after review period",
}

# Backtest thresholds required for paper → live promotion.
PAPER_TO_LIVE_MIN_SHARPE:   float = 0.5
PAPER_TO_LIVE_MAX_DRAWDOWN: float = 0.20   # 20 %


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


@dataclass
class StrategyRecord:
    strategy_id: str
    state:       StrategyState
    state_since: str
    history:     List[TransitionEvent] = field(default_factory=list)
    metadata:    dict                  = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# 4.  Exceptions
# ─────────────────────────────────────────────────────────────────────────────

class LifecycleError(Exception):
    """Raised when a requested state transition is invalid or blocked by a guard."""


# ─────────────────────────────────────────────────────────────────────────────
# 5.  State machine
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
        """Load state machine from a manifest JSON file."""
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Manifest not found: {p}")
        data = json.loads(p.read_text())
        records: Dict[str, StrategyRecord] = {}
        for sid, rec in data.get("strategies", {}).items():
            history = [TransitionEvent(**e) for e in rec.get("history", [])]
            records[sid] = StrategyRecord(
                strategy_id=sid,
                state=StrategyState(rec["state"]),
                state_since=rec["state_since"],
                history=history,
                metadata=rec.get("metadata", {}),
            )
        return cls(records, data.get("decommissioned", {}))

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

        # Guard: paper → live requires backtest thresholds
        if key == (StrategyState.PAPER, StrategyState.LIVE):
            md = metadata or {}
            sharpe   = md.get("sharpe")
            drawdown = md.get("max_drawdown")
            if sharpe is None or drawdown is None:
                return False, (
                    "paper→live requires metadata keys 'sharpe' and 'max_drawdown'"
                )
            if sharpe < PAPER_TO_LIVE_MIN_SHARPE:
                return False, (
                    f"paper→live blocked: sharpe {sharpe:.2f} < "
                    f"minimum {PAPER_TO_LIVE_MIN_SHARPE}"
                )
            if drawdown > PAPER_TO_LIVE_MAX_DRAWDOWN:
                return False, (
                    f"paper→live blocked: max_drawdown {drawdown:.2%} > "
                    f"limit {PAPER_TO_LIVE_MAX_DRAWDOWN:.0%}"
                )

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
        Returns the updated StrategyRecord.
        """
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
        logger.info(
            "lifecycle[%s]: %s → %s  actor=%s",
            strategy_id, event.from_state, to_state.value, actor,
        )
        self._persist_lifecycle_event(strategy_id, event, metadata)
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
        self._records[strategy_id] = rec
        return rec

    # ── persistence ──────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        strategies = {}
        for sid, rec in self._records.items():
            strategies[sid] = {
                "state":       rec.state.value,
                "state_since": rec.state_since,
                "metadata":    rec.metadata,
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
        return {
            "schema_version":  "1.0",
            "updated_at":      datetime.now(timezone.utc).isoformat(),
            "strategies":      strategies,
            "decommissioned":  self.decommissioned,
        }

    def save_manifest(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2))
        logger.info("Manifest saved to %s", p)
