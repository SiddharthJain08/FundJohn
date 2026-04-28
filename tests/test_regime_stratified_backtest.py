"""tests/test_regime_stratified_backtest.py

Verify the regime-stratified backtest invariants introduced 2026-04-27:

  1. A strategy declaring active_in_regimes=['LOW_VOL'] only runs windows
     whose label is LOW_VOL.
  2. A strategy declaring all four regimes runs windows for every regime
     that has a viable historical span.
  3. A strategy declaring CRISIS-only with no eligible historical window
     returns a clean response (no error, regime_breakdown notes the gap).
  4. The regime['state'] passed to generate_signals at each step matches
     the deterministic classifier's output for that date.

Tests use synthetic strategy classes that subclass BaseStrategy and record
what they were given. No mocking of prices/macro — the tests run against
the real master parquets so the integration is end-to-end.

Run:
    pytest tests/test_regime_stratified_backtest.py -v
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from strategies.auto_backtest import (  # noqa: E402
    regime_windows_for_strategy,
    run_backtest,
)
from strategies.historical_regimes import regime_for_date, find_regime_windows  # noqa: E402


# ── Synthetic strategy fixtures ─────────────────────────────────────────────
#
# Writing one-off strategy classes inline is awkward because auto_backtest
# imports by file path. We materialize each test strategy to a tempfile and
# pass the path to run_backtest. Files are written to a per-test temp dir
# that pytest cleans up automatically.

STRATEGY_TEMPLATE = '''
"""tests/_fixture_{slug}.py — synthetic strategy for regime-stratified backtest tests."""
from typing import List
from strategies.base import BaseStrategy, Signal

class TestStrategy(BaseStrategy):
    id   = '{sid}'
    name = '{sid}'
    description = 'fixture'
    tier = 3
    signal_frequency = 'daily'
    min_lookback = 20
    active_in_regimes = {regimes}

    def generate_signals(self, prices, regime, universe, aux_data=None):
        # Record every regime['state'] we receive into the side-channel file.
        try:
            with open({recorder!r}, 'a') as f:
                f.write(str(prices.index[-1].date()) + ',' + str(regime.get('state','?')) + '\\n')
        except Exception:
            pass
        return []
'''


def _write_strategy(tmpdir: Path, slug: str, regimes: list[str], recorder: Path) -> Path:
    sid = f'TEST_{slug}'
    path = tmpdir / f'_fixture_{slug}.py'
    path.write_text(STRATEGY_TEMPLATE.format(
        slug=slug, sid=sid, regimes=repr(regimes), recorder=str(recorder)
    ))
    # Also need a requirements file so validate_strategy doesn't reject it.
    # validate_strategy checks for active_in_regimes already; no extra file needed.
    return path


@pytest.fixture
def workdir(tmp_path):
    """Per-test temp dir for fixture files + recorder file."""
    return tmp_path


# ── Tests ───────────────────────────────────────────────────────────────────

def test_low_vol_only_strategy_runs_only_low_vol_windows(workdir):
    """A strategy declaring active_in_regimes=['LOW_VOL'] should produce
    backtest windows whose `regime` field is LOW_VOL — never the other
    three regimes."""
    recorder = workdir / 'states.csv'
    recorder.touch()
    impl = _write_strategy(workdir, 'lo', ['LOW_VOL'], recorder)
    result = run_backtest(str(impl))

    assert result['error'] is None, f'unexpected error: {result.get("error")}'
    assert result.get('method') == 'v2_regime_stratified'
    assert result['declared_regimes'] == ['LOW_VOL']

    # Every window's regime label must be LOW_VOL.
    for w in result.get('windows', []):
        assert w.get('regime') == 'LOW_VOL', \
            f'unexpected window regime {w.get("regime")} for LOW_VOL-only strategy'

    # regime_breakdown: LOW_VOL has metrics; the other three should be
    # marked not_declared.
    bd = result.get('regime_breakdown', {})
    assert 'LOW_VOL' in bd
    assert bd['LOW_VOL'].get('note') != 'not_declared', \
        'LOW_VOL is declared but breakdown says not_declared'
    for r in ('TRANSITIONING', 'HIGH_VOL', 'CRISIS'):
        assert bd.get(r, {}).get('note') == 'not_declared', \
            f'{r} should be marked not_declared but got {bd.get(r)}'


def test_all_four_regimes_strategy_covers_every_available_regime(workdir):
    """A strategy declaring all four regimes should pull windows for each
    regime that has historical coverage. CRISIS may yield only one window
    (limited COVID coverage); HIGH_VOL similarly limited. But declared
    regimes with windows must populate breakdown metrics."""
    recorder = workdir / 'states.csv'
    recorder.touch()
    impl = _write_strategy(workdir, 'all', ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'], recorder)
    result = run_backtest(str(impl))

    assert result['error'] is None
    assert set(result['declared_regimes']) == {'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}
    assert result.get('method') == 'v2_regime_stratified'

    # Compute which regimes actually have historical windows (≥60 days).
    # We use the same find_regime_windows the planner uses so this stays
    # in sync with reality — a regime with no windows shouldn't be expected
    # to populate metrics.
    available = {r for r in ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')
                 if find_regime_windows(r, min_days=60)}
    # CRISIS at minimum should be present (COVID).
    assert 'CRISIS' in available, 'CRISIS should have ≥1 historical window post-VIX-backfill'

    bd = result.get('regime_breakdown', {})
    for r in available:
        # Either has metrics or notes no_oos_window — both legal. What's
        # NOT legal for a declared+available regime is absence from breakdown.
        assert r in bd, f'declared+available regime {r} missing from breakdown'
        # If find_regime_windows returned at least one span, the planner
        # should have run the window — so we expect metrics, not no_oos.
        assert bd[r].get('note') != 'no_oos_window', \
            f'{r} has historical windows but breakdown reports no_oos_window'

    # Every actually-run window's regime must be in declared.
    for w in result.get('windows', []):
        assert w.get('regime') in result['declared_regimes']


def test_crisis_only_strategy_with_minimal_coverage(workdir):
    """A strategy declaring only CRISIS gets at most one window (the COVID
    span). Either way, the result should be clean (no error) and the three
    other regimes should be marked not_declared in the breakdown."""
    recorder = workdir / 'states.csv'
    recorder.touch()
    impl = _write_strategy(workdir, 'crisis', ['CRISIS'], recorder)
    result = run_backtest(str(impl))

    assert result['error'] is None, f'unexpected error: {result.get("error")}'
    assert result['declared_regimes'] == ['CRISIS']
    bd = result.get('regime_breakdown', {})
    for r in ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL'):
        assert bd.get(r, {}).get('note') == 'not_declared'

    # CRISIS bucket: either metrics (if a window ran) or no_oos_window (if
    # no historical CRISIS span met min_days). Whichever, the result must
    # carry the bucket.
    assert 'CRISIS' in bd


def test_regime_state_passed_to_generate_signals_matches_classifier(workdir):
    """For every step the strategy is called on, regime['state'] must equal
    regime_for_date(current_date) — the realisation of the time-varying
    regime promise. We use a strategy that records every (date, state) it
    receives and then check each pair against the classifier."""
    recorder = workdir / 'states.csv'
    recorder.touch()
    # Declare all regimes so the strategy is called on a wide variety of
    # dates spanning multiple regimes.
    impl = _write_strategy(workdir, 'rec', ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'], recorder)
    result = run_backtest(str(impl))
    assert result['error'] is None

    rows = [line.strip().split(',') for line in recorder.read_text().splitlines() if line.strip()]
    assert len(rows) > 0, 'generate_signals was never called — backtest produced no steps'

    # Spot-check at least 10 (date, state) pairs against the classifier.
    sample = rows[:: max(1, len(rows) // 20)]  # ≤20 evenly-spaced samples
    for d_str, observed in sample:
        expected = regime_for_date(d_str)
        # The classifier may return UNKNOWN for dates outside its cache;
        # the backtest skips those steps so the recorded state must NEVER
        # be UNKNOWN. If it's something else, it must match exactly.
        assert observed != 'UNKNOWN', \
            f'{d_str}: backtest passed UNKNOWN regime to generate_signals'
        assert observed == expected, \
            f'{d_str}: backtest passed {observed} but classifier says {expected}'


def test_regime_windows_for_strategy_caps_per_regime():
    """The planner must cap at WINDOWS_PER_REGIME windows per declared
    regime — so a strategy declaring all four can produce no more than
    4*2 = 8 planned windows."""
    plan = regime_windows_for_strategy(['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'])
    by_regime: dict[str, int] = {}
    for w in plan:
        by_regime[w['regime']] = by_regime.get(w['regime'], 0) + 1
    for r, n in by_regime.items():
        assert n <= 2, f'regime {r} got {n} windows; cap is 2'
    assert len(plan) <= 8, f'planner returned {len(plan)} windows (cap 8)'
