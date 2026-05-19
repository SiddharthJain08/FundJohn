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
from unittest.mock import MagicMock, patch  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402


# ── Tests: Phase 2 redeploy spawn wiring ─────────────────────────────────────

class _RedeployHarness:
    """Common mocks for run_one_tick() tests that exercise the redeploy
    spawn branch. Pre-seeds 2 ticks of HIGH_VOL history so a third HIGH_VOL
    state is a confirmed LOW_VOL→HIGH_VOL transition (matches the
    n_required=3 hysteresis path)."""

    def __init__(self, *, cooldown_active=False, model_returns_high_vol=True):
        self.cooldown_active = cooldown_active
        self.model_returns_high_vol = model_returns_high_vol

        # Stub feature dict — must satisfy persist_state_row and the
        # HMM scoring guard (non-NaN vix_synth_30d so scoring proceeds).
        self.ts = pd.Timestamp('2026-05-19 14:00:00', tz='UTC')
        self.features = {
            'ts_utc': self.ts,
            'vix_synth_30d': 18.0,
            'vix_synth_90d': 17.0,
            'vix_term_slope': -0.05,
            'rr_25d': 0.01,
            'spy_realized_vol_30m': 0.012,
            'source_quality_flag': 'ok',
        }

        # Stub intraday module
        self.intraday_mod = MagicMock()
        self.intraday_mod.collect_intraday_features.return_value = self.features
        self.intraday_mod.append_features_row = MagicMock()

        # Stub postgres connection (must not blow up on persist)
        self.pg_conn = MagicMock()
        self.pg_conn.cursor.return_value = MagicMock()
        # History (newest first): 2 prior HIGH_VOL ticks above a LOW_VOL one
        # → confirmed LOW_VOL→HIGH_VOL transition on this tick.
        self.history = [
            {'state': 'HIGH_VOL'},
            {'state': 'HIGH_VOL'},
            {'state': 'LOW_VOL'},
            {'state': 'LOW_VOL'},
        ]

        # Stub redis. cooldown gate keyed on liquidate:cooldown:{date_str}.
        self.redis = MagicMock()
        if cooldown_active:
            self.redis.get.return_value = '1'
        else:
            self.redis.get.return_value = None

        # Captured arguments for the persist_state_row call
        self.persisted_kwargs = {}

        # Captured Popen calls (the redeploy spawn)
        self.popen_calls: list[dict] = []

    def fake_persist(self, conn, ts_utc, state, prior_state, confidence,
                    hysteresis_streak, fired_liquidation, transition_tag,
                    features_dict):
        self.persisted_kwargs = {
            'state': state, 'prior_state': prior_state,
            'confidence': confidence, 'streak': hysteresis_streak,
            'fired_liquidation': fired_liquidation,
            'transition_tag': transition_tag,
        }

    def fake_popen(self, cmd, **kwargs):
        self.popen_calls.append({'cmd': list(cmd), 'kwargs': kwargs})
        proc = MagicMock()
        proc.pid = 4242
        return proc


def _run_tick_with_harness(harness, *, live: bool, force_dry_run=False):
    """Execute detector.run_one_tick() under harness mocks. Patches every
    external dependency at the module-level."""
    env = {'OPENCLAW_INTRADAY_HMM_LIVE': '1' if live else '0'}
    with patch.dict(os.environ, env, clear=False), \
         patch.object(detector, '_load_intraday_features_module',
                      return_value=harness.intraday_mod), \
         patch.object(detector, '_connect_postgres',
                      return_value=harness.pg_conn), \
         patch.object(detector, '_last_n_states',
                      return_value=harness.history), \
         patch.object(detector, '_redis',
                      return_value=harness.redis), \
         patch.object(detector, '_persist_state_row',
                      side_effect=harness.fake_persist), \
         patch.object(detector, '_post_to_discord'), \
         patch.object(detector, 'MODEL_PATH', MagicMock(
             exists=MagicMock(return_value=False))), \
         patch.object(detector.subprocess, 'Popen',
                      side_effect=harness.fake_popen):
        # Force the state to HIGH_VOL by bypassing the (missing) model:
        # since MODEL_PATH.exists() returns False, state_name stays
        # 'UNKNOWN'. We need it to be HIGH_VOL so the confirmed-transition
        # path fires. Patch _state_from_hmm + MODEL_PATH.exists True.
        with patch.object(detector, '_state_from_hmm',
                          return_value=('HIGH_VOL', 0.92, np.array([0.04, 0.04, 0.92, 0.0]))), \
             patch.object(detector.MODEL_PATH, 'exists', return_value=True), \
             patch('builtins.open', MagicMock()), \
             patch.object(detector.pickle, 'load',
                          return_value=MagicMock()):
            return detector.run_one_tick(force_dry_run=force_dry_run)


class TestRedeploySpawn:
    def test_confirmed_transition_spawns_redeploy_live(self):
        h = _RedeployHarness(cooldown_active=False)
        result = _run_tick_with_harness(h, live=True)

        # Audit row should be marked as a redeploy transition
        assert h.persisted_kwargs['transition_tag'] == (
            'INTRADAY_HMM_REDEPLOY_LOW_VOL_HIGH_VOL'
        )
        assert h.persisted_kwargs['fired_liquidation'] is True

        # Popen called exactly once with redeploy script
        assert len(h.popen_calls) == 1
        cmd = h.popen_calls[0]['cmd']
        assert 'redeploy_pipeline.py' in ' '.join(cmd)
        # --reason must be the canonical from_to (no _REDEPLOY in the script's
        # --reason; the script-side tag is the same as the audit's <from>_<to>
        # body; the audit prepends INTRADAY_HMM_REDEPLOY_ for distinguishability).
        assert '--reason' in cmd
        idx = cmd.index('--reason')
        assert cmd[idx + 1] == 'INTRADAY_HMM_LOW_VOL_HIGH_VOL'
        # --dry-run NOT passed in LIVE mode
        assert '--dry-run' not in cmd
        # Detached spawn semantics
        kwargs = h.popen_calls[0]['kwargs']
        assert kwargs.get('start_new_session') is True

        assert result['state'] == 'HIGH_VOL'
        assert result['fired'] is True

    def test_confirmed_transition_spawns_dry_run_when_live_off(self):
        h = _RedeployHarness(cooldown_active=False)
        result = _run_tick_with_harness(h, live=False)

        assert h.persisted_kwargs['transition_tag'] == (
            'INTRADAY_HMM_REDEPLOY_LOW_VOL_HIGH_VOL'
        )
        assert h.persisted_kwargs['fired_liquidation'] is True

        assert len(h.popen_calls) == 1
        cmd = h.popen_calls[0]['cmd']
        # --dry-run IS passed in LIVE=0 mode
        assert '--dry-run' in cmd
        assert '--reason' in cmd
        idx = cmd.index('--reason')
        assert cmd[idx + 1] == 'INTRADAY_HMM_LOW_VOL_HIGH_VOL'

    def test_cooldown_blocks_spawn(self):
        h = _RedeployHarness(cooldown_active=True)
        _ = _run_tick_with_harness(h, live=True)

        # No Popen call when cooldown is active
        assert h.popen_calls == []
        # Audit tag carries _COOLDOWN suffix and fired_liquidation stays False
        assert h.persisted_kwargs['transition_tag'] == (
            'INTRADAY_HMM_LOW_VOL_HIGH_VOL_COOLDOWN'
        )
        assert h.persisted_kwargs['fired_liquidation'] is False
