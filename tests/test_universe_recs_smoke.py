"""SP-2 Phase C smoke tests — universe-recs pipeline.

FAST and deterministic only. No real backtests, no LLM calls, no DB writes.
Must all pass in well under one minute total.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATH = REPO_ROOT / "src" / "agent" / "prompts" / "subagents" / "universe-recommender.md"


# ---------------------------------------------------------------------------
# 1. Gate-off: --mode universe-recs with OPENCLAW_UNIVERSE_RECS=0 exits 0 +
#    stdout contains "skipped".
# ---------------------------------------------------------------------------

def test_dispatcher_gate_off_skips():
    """OPENCLAW_UNIVERSE_RECS=0 → process exits 0 and stdout contains "skipped"."""
    env = {**os.environ, "OPENCLAW_UNIVERSE_RECS": "0"}
    result = subprocess.run(
        ["node", "src/agent/curators/run_mastermind.js", "--mode", "universe-recs"],
        env=env,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"Expected returncode 0, got {result.returncode}.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    assert "skipped" in result.stdout, (
        f'"skipped" not found in stdout: {result.stdout!r}'
    )


# ---------------------------------------------------------------------------
# 2. CANDIDATE_PREDICATES dict is present and has ≥ 10 entries.
# ---------------------------------------------------------------------------

def test_candidate_predicates_present():
    """universe_default.CANDIDATE_PREDICATES must be a dict with at least 10 entries."""
    from src.strategies.universe_default import CANDIDATE_PREDICATES  # noqa: PLC0415

    assert isinstance(CANDIDATE_PREDICATES, dict), (
        f"Expected dict, got {type(CANDIDATE_PREDICATES)}"
    )
    assert len(CANDIDATE_PREDICATES) >= 10, (
        f"Expected ≥ 10 candidates, got {len(CANDIDATE_PREDICATES)}: "
        f"{list(CANDIDATE_PREDICATES.keys())}"
    )


# ---------------------------------------------------------------------------
# 3. Prompt template contains all required placeholder tokens.
# ---------------------------------------------------------------------------

_REQUIRED_PLACEHOLDERS = [
    "{{strategy_id}}",
    "{{strategy_name}}",
    "{{thesis}}",
    "{{source_code}}",
    "{{current_predicate}}",
    "{{candidate_names}}",
    # grid metric tokens
    "{{sharpe}}",
    "{{max_dd_pct}}",
    "{{win_rate}}",
    "{{trades_n}}",
    "{{sortino}}",
    "{{calmar}}",
    "{{mean_holding_days}}",
    "{{mean_universe_size}}",
]


def test_prompt_template_has_placeholders():
    """universe-recommender.md must contain every required placeholder token."""
    assert PROMPT_PATH.exists(), f"Prompt template not found: {PROMPT_PATH}"
    content = PROMPT_PATH.read_text()
    missing = [p for p in _REQUIRED_PLACEHOLDERS if p not in content]
    assert not missing, (
        f"Prompt template is missing placeholders: {missing}"
    )


# ---------------------------------------------------------------------------
# 4. list subcommand exits 0 (requires POSTGRES_URI; skipped if unset).
# ---------------------------------------------------------------------------

def test_list_pending_no_crash():
    """lifecycle_universe_adoption list → returncode 0 (skipped if no POSTGRES_URI)."""
    if not os.environ.get("POSTGRES_URI"):
        pytest.skip("POSTGRES_URI not set — skipping DB-touching smoke")

    result = subprocess.run(
        [sys.executable, "-m", "src.strategies.lifecycle_universe_adoption", "list"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"Expected returncode 0, got {result.returncode}.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# 5. _ref_for_candidate returns the resolver-loadable "module:attr" form.
# ---------------------------------------------------------------------------

def test_adoption_writes_module_ref_form():
    """_ref_for_candidate('large_cap') must return 'src.strategies.universe_default:large_cap'."""
    from src.strategies.lifecycle_universe_adoption import _ref_for_candidate  # noqa: PLC0415

    result = _ref_for_candidate("large_cap")
    assert result == "src.strategies.universe_default:large_cap", (
        f"Expected 'src.strategies.universe_default:large_cap', got {result!r}"
    )
