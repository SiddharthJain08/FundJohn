"""Tests for src/execution/ic_gate.py — pure-function IC classifier.

Phase 2A — Renaissance IC Approval Gate.
Spec: docs/superpowers/plans/2026-05-15-fincept-imports-phase-2-master-plan.md
(tasks A2.1–A2.8, lines ~568–665).

6 tests per A2.3:
  1. live-eligible strategy             → AUTO_APPROVE
  2. staging-tier strategy              → IC_REQUIRED
  3. deprecated strategy                → VETOED with reason
  4. malformed signal (missing fields)  → VETOED reason=malformed_signal
  5. scale-down request clamps to 0..1  → scaled_size_pct in [0, 1]
  6. is_enabled() reflects OPENCLAW_IC_GATE env var
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import ic_gate  # noqa: E402


# ── Synthetic manifest fixture ───────────────────────────────────────────────

@pytest.fixture
def manifest():
    """Mirror src/strategies/manifest.json shape (subset). Only top-level
    keys touched by ic_gate.classify(): strategies[<id>].state."""
    return {
        "schema_version": "1.0",
        "strategies": {
            "S_live_strategy":       {"state": "live"},
            "S_monitoring_strategy": {"state": "monitoring"},
            "S_staging_strategy":    {"state": "staging"},
            "S_paper_strategy":      {"state": "paper"},
            "S_candidate_strategy":  {"state": "candidate"},
            "S_deprecated_strategy": {"state": "deprecated"},
            "S_archived_strategy":   {"state": "archived"},
        },
    }


# ── Test 1: live → AUTO_APPROVE ──────────────────────────────────────────────

def test_live_strategy_auto_approves(manifest):
    sig = {"strategy_id": "S_live_strategy", "ticker": "AAPL",
           "direction": "LONG", "entry_price": 175.0}
    out = ic_gate.classify(sig, manifest)
    assert out["classification"] == "AUTO_APPROVE"
    assert out["scaled_size_pct"] is None


# ── Test 2: staging → IC_REQUIRED ────────────────────────────────────────────

def test_staging_strategy_requires_ic(manifest):
    sig = {"strategy_id": "S_staging_strategy", "ticker": "MSFT",
           "direction": "LONG", "entry_price": 410.0}
    out = ic_gate.classify(sig, manifest)
    assert out["classification"] == "IC_REQUIRED"
    assert out["scaled_size_pct"] is None
    # Paper is also IC_REQUIRED per the spec.
    sig2 = {"strategy_id": "S_paper_strategy", "ticker": "MSFT",
            "direction": "LONG", "entry_price": 410.0}
    out2 = ic_gate.classify(sig2, manifest)
    assert out2["classification"] == "IC_REQUIRED"


# ── Test 3: deprecated/archived → VETOED ─────────────────────────────────────

def test_deprecated_strategy_vetoed(manifest):
    sig = {"strategy_id": "S_deprecated_strategy", "ticker": "TSLA",
           "direction": "LONG", "entry_price": 200.0}
    out = ic_gate.classify(sig, manifest)
    assert out["classification"] == "VETOED"
    assert out["reason"]
    assert "deprecated" in out["reason"].lower() or "lifecycle" in out["reason"].lower()
    # archived also vetoed
    sig2 = {"strategy_id": "S_archived_strategy", "ticker": "TSLA",
            "direction": "LONG", "entry_price": 200.0}
    out2 = ic_gate.classify(sig2, manifest)
    assert out2["classification"] == "VETOED"


# ── Test 4: malformed signal → VETOED with reason=malformed_signal ────────────

def test_malformed_signal_vetoed(manifest):
    # Missing strategy_id
    sig_no_strat = {"ticker": "AAPL", "direction": "LONG"}
    out = ic_gate.classify(sig_no_strat, manifest)
    assert out["classification"] == "VETOED"
    assert out["reason"] == "malformed_signal"
    # Missing ticker
    sig_no_tick = {"strategy_id": "S_live_strategy", "direction": "LONG"}
    out2 = ic_gate.classify(sig_no_tick, manifest)
    assert out2["classification"] == "VETOED"
    assert out2["reason"] == "malformed_signal"
    # Strategy not in manifest (unknown) → VETOED, distinct reason
    sig_unknown = {"strategy_id": "S_does_not_exist", "ticker": "AAPL"}
    out3 = ic_gate.classify(sig_unknown, manifest)
    assert out3["classification"] == "VETOED"
    assert out3["reason"]


# ── Test 5: scale-down clamps to [0, 1] ───────────────────────────────────────

def test_scale_clamp():
    """apply_scale() clamps requested size_pct into [0, 1]."""
    # Above 1.0 → clamps to 1.0
    assert ic_gate.apply_scale(1.5) == 1.0
    assert ic_gate.apply_scale(100) == 1.0
    # Below 0 → clamps to 0
    assert ic_gate.apply_scale(-0.2) == 0.0
    assert ic_gate.apply_scale(-50) == 0.0
    # In-range pass-through
    assert ic_gate.apply_scale(0.5) == 0.5
    assert ic_gate.apply_scale(0.0) == 0.0
    assert ic_gate.apply_scale(1.0) == 1.0


# ── Test 6: is_enabled() reflects env var ────────────────────────────────────

def test_is_enabled_env_gate(monkeypatch):
    monkeypatch.delenv("OPENCLAW_IC_GATE", raising=False)
    assert ic_gate.is_enabled() is False

    monkeypatch.setenv("OPENCLAW_IC_GATE", "0")
    assert ic_gate.is_enabled() is False

    monkeypatch.setenv("OPENCLAW_IC_GATE", "1")
    assert ic_gate.is_enabled() is True

    monkeypatch.setenv("OPENCLAW_IC_GATE", "true")
    assert ic_gate.is_enabled() is False  # only literal "1" enables
