"""tests/backtest/test_r1_assigners_benchmark_leg.py — task R1-assigners
(2026-08-25): the shared `benchmark_leg_passes` / `log_bench_gate_skip` /
`log_bench_gate_verdict` helpers in src/backtest/regime_qualification.py.

These are the "ONE source of truth" primitives the three python gate
consumers (backtest.activation_assigner, backtest.eligibility_assigner,
strategies.lifecycle's candidate->live guard) each AND onto their own
inline legacy sleeve gate — see:
  - tests/backtest/test_activation_assigner.py   (TestComputeEligibleBenchmarkLeg)
  - tests/backtest/test_eligibility_assigner.py  (TestComputeEligibleBenchmarkLeg)
  - tests/strategies/test_promotion_thresholds.py (TestCandidateToLiveBenchmarkLeg)
for the per-consumer wiring tests. This file tests the helpers themselves,
synthetically (no DB, no parquet) — ZZT-prefixed identifiers throughout,
matching this repo's synthetic-test-ticker/strategy-id convention (never a
real symbol/strategy_id).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest.regime_qualification import (  # noqa: E402
    benchmark_leg_passes, log_bench_gate_skip, log_bench_gate_verdict)
from strategies.lifecycle import min_excess_sharpe_vs_benchmark  # noqa: E402


# ── benchmark_leg_passes: fail-open on missing/non-finite benchmark ────────

def test_null_benchmark_skips_and_passes_open():
    assert benchmark_leg_passes(0.01, None, 'equity') == (True, 'skipped_null_benchmark')


def test_nan_benchmark_skips_and_passes_open():
    assert benchmark_leg_passes(0.01, float('nan'), 'equity') == (True, 'skipped_null_benchmark')


def test_inf_benchmark_skips_and_passes_open():
    assert benchmark_leg_passes(0.01, float('inf'), 'equity') == (True, 'skipped_null_benchmark')
    assert benchmark_leg_passes(0.01, float('-inf'), 'equity') == (True, 'skipped_null_benchmark')


def test_non_numeric_benchmark_skips_and_passes_open():
    # Defensive: a malformed persisted value (e.g. a stray string) must
    # never raise out of the gate -- treated identically to NULL.
    assert benchmark_leg_passes(0.01, 'not-a-number', 'equity') == (True, 'skipped_null_benchmark')


# ── benchmark_leg_passes: the real comparison ───────────────────────────────

def test_sharpe_exceeding_benchmark_passes():
    assert benchmark_leg_passes(1.2, 0.5, 'equity') == (True, 'pass')


def test_sharpe_at_or_below_benchmark_fails():
    assert benchmark_leg_passes(0.5, 0.5, 'equity') == (False, 'fail')   # tie: strict >
    assert benchmark_leg_passes(0.4, 0.5, 'equity') == (False, 'fail')


def test_none_sharpe_with_real_benchmark_fails_defensively():
    # No caller should ever reach this branch (their own legacy gate
    # already requires a non-None sharpe first) -- but never silently pass.
    assert benchmark_leg_passes(None, 0.5, 'equity') == (False, 'fail')


def test_min_excess_applied_on_top_of_the_raw_comparison():
    """Sanity-checks the helper actually uses
    min_excess_sharpe_vs_benchmark(instrument_class) rather than a bare `>`
    -- with the shared constant currently 0.0 for every class, a sharpe
    exactly equal to bench+0.0 must fail (redundant with the tie test
    above) and bench+epsilon must fail even though sharpe > bench alone."""
    excess = min_excess_sharpe_vs_benchmark('equity')
    sharpe = 0.50 + excess  # exactly at the boundary
    assert benchmark_leg_passes(sharpe, 0.50, 'equity') == (False, 'fail')
    assert benchmark_leg_passes(sharpe + 1e-9, 0.50, 'equity') == (True, 'pass')


def test_unknown_instrument_class_falls_back_to_equity():
    # Matches min_excess_sharpe_vs_benchmark's / _promotion_threshold's own
    # None/unknown -> equity fallback convention.
    assert benchmark_leg_passes(1.0, 0.5, None) == benchmark_leg_passes(1.0, 0.5, 'equity')
    assert benchmark_leg_passes(1.0, 0.5, 'not_a_real_class') == benchmark_leg_passes(1.0, 0.5, 'equity')


def test_value_synced_with_lifecycle_constant():
    """Tripwire: the helper must derive its threshold from
    strategies.lifecycle.min_excess_sharpe_vs_benchmark (imported, never a
    re-declared literal) for every class lifecycle.py defines."""
    for cls in ('equity', 'etp', 'option', 'crypto'):
        excess = min_excess_sharpe_vs_benchmark(cls)
        bench = 1.0
        # Exactly at the boundary must fail; one ULP over must pass.
        assert benchmark_leg_passes(bench + excess, bench, cls) == (False, 'fail')
        assert benchmark_leg_passes(bench + excess + 1e-9, bench, cls) == (True, 'pass')


# ── log_bench_gate_skip / log_bench_gate_verdict: exact line text ──────────

def test_skip_log_line_exact_text():
    lines = []
    log_bench_gate_skip(lines.append, 'S_ZZT_test', 'LOW_VOL')
    assert lines == ['[bench_gate] no benchmark for S_ZZT_test LOW_VOL; skipped']


def test_verdict_log_fires_only_on_a_flip():
    lines = []
    # legacy PASS, bench FAIL -> flips the outcome -> logged.
    log_bench_gate_verdict(lines.append, 'S_ZZT_test', 'LOW_VOL',
                           legacy_pass=True, bench_passes=False,
                           sharpe=0.4, benchmark_sharpe=0.5)
    assert lines == ['[bench_gate] S_ZZT_test LOW_VOL legacy=PASS bench=FAIL '
                     '(sharpe=0.4 bench=0.5)']


def test_verdict_log_silent_when_both_pass():
    lines = []
    log_bench_gate_verdict(lines.append, 'S_ZZT_test', 'LOW_VOL',
                           legacy_pass=True, bench_passes=True,
                           sharpe=1.2, benchmark_sharpe=0.5)
    assert lines == []


def test_verdict_log_silent_when_legacy_already_failed():
    """The benchmark leg can only TIGHTEN the gate -- legacy_pass=False,
    bench_passes=True is unreachable by construction at every call site
    (final = legacy_pass and bench_passes), so this must never log a
    'flip' -- there is nothing for the benchmark to have flipped."""
    lines = []
    log_bench_gate_verdict(lines.append, 'S_ZZT_test', 'LOW_VOL',
                           legacy_pass=False, bench_passes=True,
                           sharpe=5.0, benchmark_sharpe=-5.0)
    assert lines == []
    log_bench_gate_verdict(lines.append, 'S_ZZT_test', 'LOW_VOL',
                           legacy_pass=False, bench_passes=False,
                           sharpe=-1.0, benchmark_sharpe=0.5)
    assert lines == []


if __name__ == '__main__':
    import pytest
    raise SystemExit(pytest.main([__file__, '-v']))
