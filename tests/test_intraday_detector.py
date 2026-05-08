"""tests/test_intraday_detector.py

Tests for scripts/run_intraday_market_state.py — the live 5-min tick.
Focuses on hysteresis, confidence override, cooldown, and the
fired-liquidation cross-cycle interaction.

Run:
    pytest tests/test_intraday_detector.py -v
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Load the script directly via importlib (it's a script, not a package).
_spec = importlib.util.spec_from_file_location(
    'run_intraday_market_state',
    ROOT / 'scripts' / 'run_intraday_market_state.py',
)
detector = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(detector)


# ── Tests: hysteresis logic ──────────────────────────────────────────────────

class TestHysteresis:
    def test_first_observation_no_transition(self):
        """First time we see a state — no prior history → no transition."""
        history = []
        streak = detector._hysteresis_streak(history, 'HIGH_VOL')
        fired, prior = detector._confirmed_transition(
            history, 'HIGH_VOL', streak, n_required=3,
        )
        assert streak == 1
        assert not fired
        assert prior is None

    def test_two_ticks_in_new_state_below_threshold(self):
        """LOW_VOL...LOW_VOL → HIGH_VOL ... HIGH_VOL (just 2 in row)."""
        # newest-first
        history = [
            {'state': 'HIGH_VOL'},
            {'state': 'LOW_VOL'},
            {'state': 'LOW_VOL'},
            {'state': 'LOW_VOL'},
        ]
        streak = detector._hysteresis_streak(history, 'HIGH_VOL')
        fired, prior = detector._confirmed_transition(
            history, 'HIGH_VOL', streak, n_required=3,
        )
        # streak: this tick (1) + 1 matching prior = 2
        assert streak == 2
        assert not fired

    def test_three_ticks_in_new_state_fires(self):
        """LOW_VOL...LOW_VOL → HIGH_VOL × 2 + new HIGH_VOL → 3 streak → fire."""
        history = [
            {'state': 'HIGH_VOL'},
            {'state': 'HIGH_VOL'},
            {'state': 'LOW_VOL'},
            {'state': 'LOW_VOL'},
        ]
        streak = detector._hysteresis_streak(history, 'HIGH_VOL')
        fired, prior = detector._confirmed_transition(
            history, 'HIGH_VOL', streak, n_required=3,
        )
        assert streak == 3
        assert fired
        assert prior == 'LOW_VOL'

    def test_intermittent_states_break_streak(self):
        """LOW_VOL → HIGH_VOL → LOW_VOL → HIGH_VOL → HIGH_VOL → HIGH_VOL.
        Streak only counts consecutive matches from the newest end."""
        # Newest first; current tick is HIGH_VOL
        history = [
            {'state': 'HIGH_VOL'},
            {'state': 'HIGH_VOL'},
            {'state': 'LOW_VOL'},   # streak breaker
            {'state': 'HIGH_VOL'},
            {'state': 'LOW_VOL'},
        ]
        streak = detector._hysteresis_streak(history, 'HIGH_VOL')
        # current(1) + 2 matching = 3
        assert streak == 3
        fired, prior = detector._confirmed_transition(
            history, 'HIGH_VOL', streak, n_required=3,
        )
        assert fired
        assert prior == 'LOW_VOL'

    def test_no_prior_state_no_fire(self):
        """If history is exhausted with all matches and no prior-state
        marker, we don't have a transition."""
        history = [
            {'state': 'LOW_VOL'},
            {'state': 'LOW_VOL'},
        ]
        streak = detector._hysteresis_streak(history, 'LOW_VOL')
        # current + 2 matches = 3
        assert streak == 3
        fired, prior = detector._confirmed_transition(
            history, 'LOW_VOL', streak, n_required=3,
        )
        # No different state in history → no prior → not a transition.
        assert not fired
        assert prior is None


# ── Tests: confidence override ───────────────────────────────────────────────

class TestConfidenceOverride:
    def test_below_floor_forces_transitioning(self):
        result = detector._maybe_apply_confidence_floor('HIGH_VOL', 0.55)
        assert result == 'TRANSITIONING'

    def test_above_floor_keeps_state(self):
        result = detector._maybe_apply_confidence_floor('HIGH_VOL', 0.75)
        assert result == 'HIGH_VOL'

    def test_at_floor_keeps_state(self):
        # CONFIDENCE_FLOOR is strict <
        result = detector._maybe_apply_confidence_floor(
            'CRISIS', detector.CONFIDENCE_FLOOR,
        )
        assert result == 'CRISIS'


# ── Tests: env-flag gates ────────────────────────────────────────────────────

class TestLiveGate:
    def setup_method(self):
        self._prior = os.environ.pop('OPENCLAW_INTRADAY_HMM_LIVE', None) \
            if 'OPENCLAW_INTRADAY_HMM_LIVE' in os.environ else None

    def teardown_method(self):
        os.environ.pop('OPENCLAW_INTRADAY_HMM_LIVE', None)
        if self._prior is not None:
            os.environ['OPENCLAW_INTRADAY_HMM_LIVE'] = self._prior

    def test_default_off(self):
        os.environ.pop('OPENCLAW_INTRADAY_HMM_LIVE', None)
        assert detector._is_live_intraday() is False

    def test_explicit_one_enables(self):
        os.environ['OPENCLAW_INTRADAY_HMM_LIVE'] = '1'
        assert detector._is_live_intraday() is True

    def test_other_values_disabled(self):
        for v in ('0', 'true', 'yes', 'TRUE', '2'):
            os.environ['OPENCLAW_INTRADAY_HMM_LIVE'] = v
            assert detector._is_live_intraday() is False, f'expected False for {v!r}'


# Imports late so module-load isn't blocked by missing env.
import os  # noqa: E402
