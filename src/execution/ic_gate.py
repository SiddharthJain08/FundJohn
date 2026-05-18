"""Phase 2A — Renaissance IC Approval Gate: pure-function classifier.

Maps each candidate signal to one of:
  * AUTO_APPROVE — strategy is live; flows through without operator action
  * IC_REQUIRED  — strategy is in staging/paper; operator must approve in Discord
  * VETOED       — strategy is deprecated/archived OR signal is malformed

This module has NO I/O. The orchestrator step (`ic_gate_runner.py`) loads the
manifest + signal list and hands them to `classify_signals(...)`.

Default-OFF gate per Phase 2 master plan: enabled only when
OPENCLAW_IC_GATE=1. The runner consults `is_enabled()` and early-exits if
unset, so importing this module never touches DB / Discord / env state.

State→classification map (matches src/strategies/lifecycle.py StrategyState):
  live, monitoring           → AUTO_APPROVE
  staging, paper, candidate  → IC_REQUIRED
  deprecated, archived       → VETOED
  (unknown / missing)        → VETOED with reason="unknown_strategy" /
                                                 "missing_state"

Spec: docs/superpowers/plans/2026-05-15-fincept-imports-phase-2-master-plan.md
"""
from __future__ import annotations

import os
from typing import Any, Optional

# ── Classification constants (mirror migration 103 CHECK constraint) ─────────
AUTO_APPROVE = "AUTO_APPROVE"
IC_REQUIRED  = "IC_REQUIRED"
VETOED       = "VETOED"
APPROVED     = "APPROVED"   # operator path (set by Discord handler)
SCALED       = "SCALED"     # operator path (set by Discord handler)
TIMED_OUT    = "TIMED_OUT"  # runner path on poll-timeout

# ── State buckets ────────────────────────────────────────────────────────────
# `monitoring` rides with `live` (still in production execution per the
# lifecycle module). `candidate` rides with `staging`/`paper` — strategy
# exists but is not yet promoted to live, so the operator should sign off
# until the regular live-promotion path adds it to the live set.
LIVE_STATES       = frozenset({"live", "monitoring"})
IC_REQUIRED_STATES = frozenset({"staging", "paper", "candidate"})
VETOED_STATES     = frozenset({"deprecated", "archived"})

ENV_GATE = "OPENCLAW_IC_GATE"


def is_enabled() -> bool:
    """True iff OPENCLAW_IC_GATE == "1".

    Default-OFF: any other value (unset, "0", "true", "yes", anything) keeps
    the gate disabled. The runner consults this BEFORE any DB / Discord I/O
    so production behavior is byte-identical when unset.
    """
    return os.environ.get(ENV_GATE) == "1"


def apply_scale(size_pct: float) -> float:
    """Clamp an operator-requested scale fraction into [0.0, 1.0].

    Used by the Discord handler when the operator types `scale N PCT`; PCT
    is normalized to a 0..1 fraction here. Out-of-range values are clamped
    (NOT rejected) — operator intent is preserved at the boundary.
    """
    try:
        v = float(size_pct)
    except (TypeError, ValueError):
        return 0.0
    if v != v:  # NaN
        return 0.0
    if v > 1.0:
        return 1.0
    if v < 0.0:
        return 0.0
    return v


def _lookup_state(strategy_id: str, manifest: dict) -> Optional[str]:
    """Read manifest.strategies[<id>].state. Returns None on miss."""
    strategies = (manifest or {}).get("strategies") or {}
    entry = strategies.get(strategy_id)
    if not isinstance(entry, dict):
        return None
    state = entry.get("state")
    if not isinstance(state, str):
        return None
    return state.strip().lower()


def classify(signal: dict, manifest: dict) -> dict[str, Any]:
    """Classify a single signal dict.

    Returns:
        {
          "classification": "AUTO_APPROVE" | "IC_REQUIRED" | "VETOED",
          "reason":         str | None,
          "scaled_size_pct": float | None,
        }

    Pure function — no I/O, no mutation of inputs.
    """
    if not isinstance(signal, dict):
        return {"classification": VETOED,
                "reason": "malformed_signal",
                "scaled_size_pct": None}

    strategy_id = signal.get("strategy_id")
    ticker      = signal.get("ticker")

    # Hard malformed-signal veto: missing required identification fields.
    if not strategy_id or not isinstance(strategy_id, str):
        return {"classification": VETOED,
                "reason": "malformed_signal",
                "scaled_size_pct": None}
    if not ticker or not isinstance(ticker, str):
        return {"classification": VETOED,
                "reason": "malformed_signal",
                "scaled_size_pct": None}

    state = _lookup_state(strategy_id, manifest)
    if state is None:
        return {"classification": VETOED,
                "reason": f"unknown_strategy:{strategy_id}",
                "scaled_size_pct": None}

    if state in LIVE_STATES:
        return {"classification":   AUTO_APPROVE,
                "reason":           f"lifecycle={state}",
                "scaled_size_pct":  None}
    if state in IC_REQUIRED_STATES:
        return {"classification":   IC_REQUIRED,
                "reason":           f"lifecycle={state} requires operator approval",
                "scaled_size_pct":  None}
    if state in VETOED_STATES:
        return {"classification":   VETOED,
                "reason":           f"lifecycle={state} not eligible for execution",
                "scaled_size_pct":  None}

    # Unknown state value — fail safe to VETO.
    return {"classification":   VETOED,
            "reason":           f"unknown_lifecycle_state:{state}",
            "scaled_size_pct":  None}


def classify_signals(signals: list[dict], manifest: dict) -> list[dict]:
    """Apply `classify()` to a list. Returns a list of {signal, decision} dicts.

    Used by the runner — keeps the per-signal pairing explicit so the runner
    can post a tabular Discord summary and persist one row per signal to
    ic_decisions.
    """
    out: list[dict] = []
    for sig in signals or []:
        decision = classify(sig, manifest)
        out.append({"signal": sig, "decision": decision})
    return out
