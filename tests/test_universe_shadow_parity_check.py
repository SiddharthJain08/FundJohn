"""SP-7 Phase C Task 8 — shadow-parity system check severity contract."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_check_registered_and_skips_when_gate_off(monkeypatch):
    monkeypatch.delenv("OPENCLAW_LIVE_UNIVERSE_SHADOW", raising=False)
    from src.system_checks.checks.universe_shadow_parity import _universe_shadow_parity
    from src.system_checks.types import Status
    status, detail = _universe_shadow_parity()
    assert status == Status.SKIP
    assert "gate off" in detail


def test_check_appears_in_registry():
    out = subprocess.run(
        ["python3", "-m", "src.system_checks", "--check", "universe_shadow_parity", "--json"],
        capture_output=True, text=True, timeout=60, cwd=str(ROOT))
    assert "universe_shadow_parity" in out.stdout
