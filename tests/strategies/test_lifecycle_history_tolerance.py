"""from_manifest must not be brickable by a single malformed history row.

Regression for the 2026-07-17 near-miss: a script wrote history entries as
{at, from, to} (wrong schema); TransitionEvent(**e) raised, aborting
from_manifest() — which the engine calls per-strategy via
instrument_class_for(), so a single bad row would have starved EVERY strategy
of signals. from_row now normalizes aliases and drops unsalvageable rows.
"""
from __future__ import annotations
import json
from pathlib import Path

from strategies.lifecycle import LifecycleStateMachine, TransitionEvent


def test_from_row_normalizes_aliases():
    ev = TransitionEvent.from_row(
        {"at": "2026-07-17T00:00:00Z", "from": "ejected", "to": "candidate",
         "actor": "op", "reason": "re-hearing"})
    assert ev is not None
    assert ev.timestamp == "2026-07-17T00:00:00Z"
    assert ev.from_state == "ejected"
    assert ev.to_state == "candidate"


def test_from_row_drops_unknown_keys_not_the_row():
    ev = TransitionEvent.from_row(
        {"from_state": "live", "to_state": "deprecated", "timestamp": "t",
         "actor": "a", "reason": "r", "bogus_key": 123})
    assert ev is not None and ev.from_state == "live"


def test_from_row_returns_none_for_junk():
    assert TransitionEvent.from_row("not a dict") is None
    assert TransitionEvent.from_row(None) is None


def test_manifest_with_malformed_history_still_loads(tmp_path):
    m = {
        "strategies": {
            "S_ok": {"state": "live", "state_since": "2026-01-01T00:00:00Z",
                     "history": [{"from_state": "candidate", "to_state": "live",
                                  "timestamp": "t", "actor": "a", "reason": "r"}],
                     "metadata": {}, "instrument_class": "equity"},
            "S_bad": {"state": "candidate", "state_since": "2026-07-17T00:00:00Z",
                      # legacy/alias schema that used to throw
                      "history": [{"at": "2026-07-17T00:00:00Z", "from": "ejected",
                                   "to": "candidate", "actor": "op", "reason": "x"}],
                      "metadata": {}, "instrument_class": "etp"},
        }
    }
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(m))
    sm = LifecycleStateMachine.from_manifest(str(p))   # must NOT raise
    # both strategies loaded; the aliased row was normalized, not dropped
    bad = sm._records["S_bad"] if hasattr(sm, "_records") else None
    assert bad is not None
    assert bad.history and bad.history[0].to_state == "candidate"


def test_live_manifest_loads_clean():
    # The real production manifest must always load (this is the gate the
    # near-miss bypassed).
    root = Path(__file__).resolve().parents[2]
    sm = LifecycleStateMachine.from_manifest(str(root / "src/strategies/manifest.json"))
    assert sm is not None
