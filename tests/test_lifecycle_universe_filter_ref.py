import json
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from strategies.lifecycle import LifecycleStateMachine, StrategyRecord, StrategyState

MANIFEST = """
{
  "schema_version": "1.0",
  "strategies": {
    "S_test": {
      "state": "live",
      "state_since": "2026-01-01T00:00:00+00:00",
      "metadata": {
        "canonical_file": "S_test.py",
        "class": "Test",
        "description": "Probe",
        "universe_filter_ref": "src.strategies.universe_default:options_eligible_only"
      },
      "history": []
    }
  }
}
"""

def test_loads_universe_filter_ref(tmp_path):
    p = tmp_path / "manifest.json"
    p.write_text(MANIFEST)
    lsm = LifecycleStateMachine.from_manifest(p)
    rec = lsm._records["S_test"]
    assert rec.universe_filter_ref == "src.strategies.universe_default:options_eligible_only"

def test_roundtrips_universe_filter_ref(tmp_path):
    p = tmp_path / "manifest.json"
    p.write_text(MANIFEST)
    lsm = LifecycleStateMachine.from_manifest(p)
    lsm.save_manifest(p)
    payload = json.loads(p.read_text())
    assert (payload["strategies"]["S_test"]["metadata"]["universe_filter_ref"]
            == "src.strategies.universe_default:options_eligible_only")

def test_default_when_missing(tmp_path):
    payload = json.loads(MANIFEST)
    del payload["strategies"]["S_test"]["metadata"]["universe_filter_ref"]
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(payload))
    lsm = LifecycleStateMachine.from_manifest(p)
    assert lsm._records["S_test"].universe_filter_ref is None


def test_transition_rejects_clock_dependent_predicate(tmp_path):
    # Add the fixtures dir to sys.path so import "clock_dependent_predicate"
    # resolves to the bare-module fixture.
    sys.path.insert(0, str(Path(__file__).parent / "fixtures" / "sp2"))
    payload = json.loads(MANIFEST)
    payload["strategies"]["S_test"]["state"] = "candidate"
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(payload))
    lsm = LifecycleStateMachine.from_manifest(p)
    with pytest.raises(ValueError, match="predicate behavior differs"):
        lsm.transition("S_test", StrategyState.LIVE,
                       actor="system", reason="test",
                       metadata={"universe_filter_ref": "clock_dependent_predicate:universe_filter",
                                 "sharpe": 0.7, "max_drawdown": 0.10})
