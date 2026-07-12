"""tests/test_intraday_detector.py

Tests for scripts/run_intraday_market_state.py — the live 5-min tick.
Focuses on hysteresis, confidence override, cooldown, and the
fired-liquidation cross-cycle interaction.

Run:
    pytest tests/test_intraday_detector.py -v
"""
from __future__ import annotations

import importlib.util
import json
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


# ── Test isolation (2026-07-12) ──────────────────────────────────────────────
# A live-tree pytest run of this file CLOBBERED the real regime-of-record
# (.agents/market-state/regime_latest.json) with harness fixture state: paths
# through run_one_tick/_carry_forward_tick call _refresh_regime_file, which
# writes detector.MODEL_DIR — the LIVE directory unless redirected. Redirect
# it for EVERY test; tests that patch MODEL_DIR themselves simply override.

@pytest.fixture(autouse=True)
def _isolate_model_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(detector, 'MODEL_DIR', tmp_path / 'model-dir-isolated')


# ── Tests: hysteresis logic ──────────────────────────────────────────────────

class TestHysteresis:
    """Tiered per-transition thresholds:
       CRISIS=1tick/0.90conf, HIGH_VOL=2tick/0.80conf,
       LOW_VOL & TRANSITIONING=3tick/0.70conf.
       Downward transitions always 3tick/0.70conf."""

    def test_first_observation_no_transition(self):
        """First time we see a state — no prior history → no transition."""
        history = []
        streak = detector._hysteresis_streak(history, 'HIGH_VOL')
        fired, prior = detector._confirmed_transition(
            history, 'HIGH_VOL', streak, current_confidence=0.95,
        )
        assert streak == 1
        assert not fired
        assert prior is None

    def test_low_to_transitioning_2_ticks_insufficient(self):
        """LOW_VOL→TRANSITIONING uses 3-tick tier — 2 ticks must not fire."""
        history = [
            {'state': 'TRANSITIONING', 'hysteresis_streak': 1},  # 1 prior, unconfirmed (need 3)
            {'state': 'LOW_VOL',       'hysteresis_streak': 5},
            {'state': 'LOW_VOL',       'hysteresis_streak': 4},
            {'state': 'LOW_VOL',       'hysteresis_streak': 3},  # confirmed
        ]
        streak = detector._hysteresis_streak(history, 'TRANSITIONING')
        fired, prior = detector._confirmed_transition(
            history, 'TRANSITIONING', streak, current_confidence=0.95,
        )
        assert streak == 2
        assert not fired

    def test_low_to_transitioning_3_ticks_fires(self):
        """LOW_VOL (confirmed) → TRANSITIONING × 2 + new = 3 streak → fire."""
        history = [
            {'state': 'TRANSITIONING', 'hysteresis_streak': 2},
            {'state': 'TRANSITIONING', 'hysteresis_streak': 1},
            {'state': 'LOW_VOL',       'hysteresis_streak': 7},  # confirmed prior
            {'state': 'LOW_VOL',       'hysteresis_streak': 6},
        ]
        streak = detector._hysteresis_streak(history, 'TRANSITIONING')
        fired, prior = detector._confirmed_transition(
            history, 'TRANSITIONING', streak, current_confidence=0.75,
        )
        assert streak == 3
        assert fired
        assert prior == 'LOW_VOL'

    def test_high_vol_fires_on_2_ticks_with_sufficient_conf(self):
        """HIGH_VOL tier: 2 ticks + conf >= 0.80 = fire (faster than current default)."""
        history = [
            {'state': 'HIGH_VOL', 'hysteresis_streak': 1},  # prior tick, unconfirmed (HIGH_VOL needs 2)
            {'state': 'LOW_VOL',  'hysteresis_streak': 10}, # confirmed prior regime
            {'state': 'LOW_VOL',  'hysteresis_streak': 9},
        ]
        streak = detector._hysteresis_streak(history, 'HIGH_VOL')
        assert streak == 2
        fired, prior = detector._confirmed_transition(
            history, 'HIGH_VOL', streak, current_confidence=0.82,
        )
        assert fired
        assert prior == 'LOW_VOL'

    def test_high_vol_does_not_fire_on_single_tick(self):
        """HIGH_VOL needs 2 ticks even at very high confidence."""
        history = [
            {'state': 'LOW_VOL', 'hysteresis_streak': 10},
        ]
        streak = detector._hysteresis_streak(history, 'HIGH_VOL')
        assert streak == 1
        fired, prior = detector._confirmed_transition(
            history, 'HIGH_VOL', streak, current_confidence=0.99,
        )
        assert not fired

    def test_high_vol_does_not_fire_below_confidence_floor(self):
        """HIGH_VOL 2-tick streak but conf below 0.80 = no fire."""
        history = [
            {'state': 'HIGH_VOL', 'hysteresis_streak': 1},
            {'state': 'LOW_VOL',  'hysteresis_streak': 10},
        ]
        streak = detector._hysteresis_streak(history, 'HIGH_VOL')
        fired, _ = detector._confirmed_transition(
            history, 'HIGH_VOL', streak, current_confidence=0.75,
        )
        assert not fired

    def test_crisis_fires_on_single_tick_when_high_conf(self):
        """CRISIS tier: 1 tick + conf >= 0.90 = fire (fastest path)."""
        history = [
            {'state': 'HIGH_VOL', 'hysteresis_streak': 5},  # confirmed prior (HV=2)
            {'state': 'HIGH_VOL', 'hysteresis_streak': 4},
        ]
        streak = detector._hysteresis_streak(history, 'CRISIS')
        assert streak == 1
        fired, prior = detector._confirmed_transition(
            history, 'CRISIS', streak, current_confidence=0.91,
        )
        assert fired
        assert prior == 'HIGH_VOL'

    def test_crisis_does_not_fire_below_confidence_floor(self):
        """CRISIS 1-tick but conf below 0.90 = no fire — tight conf
        floor is the counterweight against single-tick noise."""
        history = [
            {'state': 'HIGH_VOL', 'hysteresis_streak': 5},
        ]
        streak = detector._hysteresis_streak(history, 'CRISIS')
        fired, _ = detector._confirmed_transition(
            history, 'CRISIS', streak, current_confidence=0.89,
        )
        assert not fired

    def test_downward_transition_requires_3_ticks(self):
        """Downward transitions (less-severe target) always use (3, 0.70)
        regardless of source severity — no urgency to re-add risk on
        regime normalization, and whipsaw protection matters more."""
        # 2-tick HIGH_VOL after confirmed CRISIS — must NOT fire (downward needs 3)
        history_2 = [
            {'state': 'HIGH_VOL', 'hysteresis_streak': 1},
            {'state': 'CRISIS',   'hysteresis_streak': 5},  # confirmed (CR=1)
        ]
        streak_2 = detector._hysteresis_streak(history_2, 'HIGH_VOL')
        fired, _ = detector._confirmed_transition(
            history_2, 'HIGH_VOL', streak_2, current_confidence=0.95,
        )
        assert streak_2 == 2
        assert not fired
        # 3-tick HIGH_VOL after confirmed CRISIS — fires
        history_3 = [
            {'state': 'HIGH_VOL', 'hysteresis_streak': 2},
            {'state': 'HIGH_VOL', 'hysteresis_streak': 1},
            {'state': 'CRISIS',   'hysteresis_streak': 5},
        ]
        streak_3 = detector._hysteresis_streak(history_3, 'HIGH_VOL')
        fired, prior = detector._confirmed_transition(
            history_3, 'HIGH_VOL', streak_3, current_confidence=0.75,
        )
        assert fired
        assert prior == 'CRISIS'

    def test_no_prior_state_no_fire(self):
        """If history is exhausted with no confirmed prior, no fire."""
        history = [
            {'state': 'LOW_VOL', 'hysteresis_streak': 2},
            {'state': 'LOW_VOL', 'hysteresis_streak': 1},
        ]
        streak = detector._hysteresis_streak(history, 'LOW_VOL')
        assert streak == 3
        fired, prior = detector._confirmed_transition(
            history, 'LOW_VOL', streak, current_confidence=0.75,
        )
        assert not fired
        assert prior is None

    def test_single_transient_tick_does_not_re_fire(self):
        """Regression for 2026-05-22 13:30/13:45 false positive.

        Real production sequence (newest-first):
            13:40 TRANSITIONING streak=2   ← current_state matching ticks
            13:35 TRANSITIONING streak=1
            13:30 LOW_VOL       streak=1   ← single noisy tick (unconfirmed)
            13:25 TRANSITIONING streak=4   ← previously confirmed regime

        New logic finds the most-recent confirmed row (13:25 TRANSITIONING)
        first, sees it matches current_state, and correctly declines to
        fire."""
        history = [
            {'state': 'TRANSITIONING', 'hysteresis_streak': 2},
            {'state': 'TRANSITIONING', 'hysteresis_streak': 1},
            {'state': 'LOW_VOL',       'hysteresis_streak': 1},
            {'state': 'TRANSITIONING', 'hysteresis_streak': 4},
            {'state': 'TRANSITIONING', 'hysteresis_streak': 3},
            {'state': 'TRANSITIONING', 'hysteresis_streak': 2},
            {'state': 'TRANSITIONING', 'hysteresis_streak': 1},
        ]
        streak = detector._hysteresis_streak(history, 'TRANSITIONING')
        assert streak == 3
        fired, prior = detector._confirmed_transition(
            history, 'TRANSITIONING', streak, current_confidence=0.85,
        )
        assert not fired
        assert prior is None

    def test_post_fire_idempotent(self):
        """After a real fire, subsequent same-state ticks must NOT
        re-fire — the immediately-prior row's confirmed streak matches
        current, so the search returns no-transition on the very first
        history row inspected."""
        history = [
            {'state': 'TRANSITIONING', 'hysteresis_streak': 3},  # fired tick
            {'state': 'TRANSITIONING', 'hysteresis_streak': 2},
            {'state': 'TRANSITIONING', 'hysteresis_streak': 1},
            {'state': 'LOW_VOL',       'hysteresis_streak': 10},
            {'state': 'LOW_VOL',       'hysteresis_streak': 9},
        ]
        streak = detector._hysteresis_streak(history, 'TRANSITIONING')
        assert streak == 4
        fired, prior = detector._confirmed_transition(
            history, 'TRANSITIONING', streak, current_confidence=0.85,
        )
        assert not fired
        assert prior is None


class TestTierHelpers:
    """Direct tests for the tier-resolution helpers."""

    def test_upward_uses_state_tier(self):
        assert detector._tier_for_transition('LOW_VOL', 'CRISIS') == (1, 0.90)
        assert detector._tier_for_transition('LOW_VOL', 'HIGH_VOL') == (2, 0.80)
        assert detector._tier_for_transition('TRANSITIONING', 'HIGH_VOL') == (2, 0.80)
        assert detector._tier_for_transition('HIGH_VOL', 'CRISIS') == (1, 0.90)
        assert detector._tier_for_transition('LOW_VOL', 'TRANSITIONING') == (3, 0.70)

    def test_downward_always_conservative(self):
        assert detector._tier_for_transition('CRISIS', 'HIGH_VOL') == (3, 0.70)
        assert detector._tier_for_transition('CRISIS', 'LOW_VOL') == (3, 0.70)
        assert detector._tier_for_transition('HIGH_VOL', 'TRANSITIONING') == (3, 0.70)
        assert detector._tier_for_transition('TRANSITIONING', 'LOW_VOL') == (3, 0.70)

    def test_same_state_conservative(self):
        assert detector._tier_for_transition('HIGH_VOL', 'HIGH_VOL') == (3, 0.70)

    def test_no_prior_uses_conservative(self):
        assert detector._tier_for_transition(None, 'CRISIS') == (3, 0.70)

    def test_find_settled_regime_via_fired_row(self):
        """A row with fired_liquidation=True defines the settled regime."""
        history = [
            {'state': 'HIGH_VOL', 'hysteresis_streak': 1, 'fired_liquidation': False},
            {'state': 'HIGH_VOL', 'hysteresis_streak': 2, 'fired_liquidation': True},  # fired
            {'state': 'LOW_VOL',  'hysteresis_streak': 10, 'fired_liquidation': False},
        ]
        assert detector._find_settled_regime(history) == 'HIGH_VOL'

    def test_find_settled_regime_via_long_streak_fallback(self):
        """No fired rows → fall back to oldest row with streak >= 3."""
        history = [
            {'state': 'HIGH_VOL', 'hysteresis_streak': 2, 'fired_liquidation': False},  # not yet settled
            {'state': 'LOW_VOL',  'hysteresis_streak': 10, 'fired_liquidation': False},  # long streak
        ]
        assert detector._find_settled_regime(history) == 'LOW_VOL'

    def test_find_settled_regime_empty_history(self):
        assert detector._find_settled_regime([]) is None

    def test_find_settled_regime_all_unconfirmed(self):
        """All rows unconfirmed (no fires, all streaks < 3) → None."""
        history = [
            {'state': 'LOW_VOL', 'hysteresis_streak': 2, 'fired_liquidation': False},
            {'state': 'LOW_VOL', 'hysteresis_streak': 1, 'fired_liquidation': False},
        ]
        assert detector._find_settled_regime(history) is None


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
    spawn branch. Pre-seeds 1 tick of HIGH_VOL (still unconfirmed under
    the 2-tick HIGH_VOL tier) above a confirmed LOW_VOL regime, so the
    current HIGH_VOL tick at conf 0.92 brings streak to 2 and fires the
    LOW_VOL→HIGH_VOL transition under the (2, 0.80) tier."""

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
        # History (newest first): 1 prior HIGH_VOL tick (still unconfirmed
        # under HIGH_VOL=2 tier) above a CONFIRMED LOW_VOL regime (streak>=3).
        # Current HIGH_VOL tick at conf 0.92 brings streak to 2 → fires
        # LOW_VOL→HIGH_VOL under tier (2, 0.80).
        self.history = [
            {'state': 'HIGH_VOL', 'hysteresis_streak': 1},
            {'state': 'LOW_VOL',  'hysteresis_streak': 8},
            {'state': 'LOW_VOL',  'hysteresis_streak': 7},
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
    # Capture _sync_regime_to_consumers calls so transition-path tests can
    # assert the regime propagation happens before the redeploy spawn.
    harness.sync_calls = []
    def _fake_sync(**kwargs):
        harness.sync_calls.append(kwargs)
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
         patch.object(detector, '_sync_regime_to_consumers',
                      side_effect=_fake_sync), \
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
                          return_value=('HIGH_VOL', 0.92,
                                        {'LOW_VOL': 0.04, 'TRANSITIONING': 0.04,
                                         'HIGH_VOL': 0.92, 'CRISIS': 0.0})), \
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
        # Regime sync must NOT fire on cooldown — operator's flatten intent
        # would otherwise be undermined by a stale-but-now-updated regime
        # row pointing the next sizer at HIGH_VOL.
        assert h.sync_calls == []


class TestRegimeSyncOnTransition:
    def test_regime_sync_fires_before_redeploy_spawn_live(self):
        """A confirmed transition must push the new regime into
        market_regime + regime_latest.json BEFORE the redeploy is spawned;
        otherwise the redeploy's engine reads the stale daily-HMM regime."""
        h = _RedeployHarness(cooldown_active=False)
        _run_tick_with_harness(h, live=True)

        assert len(h.sync_calls) == 1, (
            f'expected exactly one regime sync call, got {len(h.sync_calls)}'
        )
        call = h.sync_calls[0]
        assert call['new_state'] == 'HIGH_VOL'
        assert call['prior_state'] == 'LOW_VOL'
        assert call['transition_tag'] == 'INTRADAY_HMM_REDEPLOY_LOW_VOL_HIGH_VOL'
        # Confidence must match what the (mocked) HMM returned
        assert call['confidence'] == 0.92
        # The redeploy spawn must follow the sync — both should have fired
        assert len(h.popen_calls) == 1

    def test_regime_sync_fires_in_dry_run_too(self):
        """LIVE=0 dry-run still updates the regime sinks so the dashboard
        and any out-of-band consumer reflect the intraday-detected state."""
        h = _RedeployHarness(cooldown_active=False)
        _run_tick_with_harness(h, live=False)

        assert len(h.sync_calls) == 1
        assert h.sync_calls[0]['new_state'] == 'HIGH_VOL'


class TestSyncRegimeHelper:
    """Direct unit tests for _sync_regime_to_consumers — covers the DB
    INSERT shape and the regime_latest.json atomic-write merge logic."""

    def _features(self, ts):
        return {
            'ts_utc':       ts,
            'vix_synth_30d': 22.5,
        }

    def test_writes_market_regime_row_and_updates_json(self, tmp_path,
                                                       monkeypatch):
        ts = pd.Timestamp('2026-05-21 22:10:00', tz='UTC')
        regime_file = tmp_path / 'regime_latest.json'
        # Seed an existing file carrying frozen daily-HMM fields, so we
        # can verify the merge PURGES them (2026-07-12: the daily detector
        # no longer writes this file, so those keys can only ever be stale).
        regime_file.write_text(json.dumps({
            'date':         '2026-05-21',
            'state':        'LOW_VOL',
            'state_raw':    'LOW_VOL',
            'confidence':   0.99,
            'stress_score': 37,
            'roro_score':   2.9,
            'vix_level':    17.44,
            'prior_state':  'LOW_VOL',
        }))

        monkeypatch.setattr(detector, 'MODEL_DIR', tmp_path)

        # Track DB cursor.execute calls
        executed = []
        cur = MagicMock()
        cur.execute.side_effect = lambda sql, params: executed.append((sql, params))
        conn = MagicMock()
        conn.cursor.return_value = cur

        # Labeled map, as _state_from_hmm now returns (2026-07-12 fix —
        # raw vectors were previously mislabeled by index downstream).
        state_probs = {'LOW_VOL': 0.04, 'TRANSITIONING': 0.04,
                       'HIGH_VOL': 0.92, 'CRISIS': 0.0}
        detector._sync_regime_to_consumers(
            conn=conn,
            new_state='HIGH_VOL',
            prior_state='LOW_VOL',
            confidence=0.92,
            state_probs=state_probs,
            features=self._features(ts),
            ts_utc=ts,
            transition_tag='INTRADAY_HMM_REDEPLOY_LOW_VOL_HIGH_VOL',
        )

        # market_regime INSERT
        assert len(executed) == 1
        sql, params = executed[0]
        assert 'INSERT INTO market_regime' in sql
        assert params[0] == 'HIGH_VOL'
        assert params[1] == 22.5     # vix_synth_30d → vix_level
        # regime_data JSON blob — verify source + state_probs round-tripped
        meta = json.loads(params[3])
        assert meta['source'] == 'intraday_hmm'
        assert meta['prior_state'] == 'LOW_VOL'
        assert meta['confidence'] == 0.92
        assert meta['state_probabilities']['HIGH_VOL'] == 0.92
        assert meta['transition_tag'] == 'INTRADAY_HMM_REDEPLOY_LOW_VOL_HIGH_VOL'

        # regime_latest.json — state-related fields mutated, retired
        # daily-only fields purged (2026-07-12)
        updated = json.loads(regime_file.read_text())
        assert updated['state']       == 'HIGH_VOL'
        assert updated['state_raw']   == 'HIGH_VOL'
        assert updated['confidence']  == 0.92
        assert updated['prior_state'] == 'LOW_VOL'
        assert updated['vix_level']   == 22.5
        # Retired daily fields PURGED (frozen since 2026-06-08 — nothing
        # maintains them; consumers have explicit no-data fallbacks)
        assert 'stress_score' not in updated
        assert 'roro_score' not in updated
        assert 'date' not in updated
        assert updated['state_probabilities']['HIGH_VOL'] == 0.92
        # Intraday provenance stamped
        assert updated['intraday_source'] == 'intraday_hmm'
        assert updated['intraday_transition'] == (
            'INTRADAY_HMM_REDEPLOY_LOW_VOL_HIGH_VOL'
        )

    def test_missing_regime_file_still_writes(self, tmp_path, monkeypatch):
        """If regime_latest.json doesn't exist yet (very first intraday tick),
        the helper must still write a valid file with the new state."""
        ts = pd.Timestamp('2026-05-21 22:10:00', tz='UTC')
        monkeypatch.setattr(detector, 'MODEL_DIR', tmp_path)
        conn = MagicMock()
        conn.cursor.return_value = MagicMock()

        detector._sync_regime_to_consumers(
            conn=conn,
            new_state='TRANSITIONING',
            prior_state='LOW_VOL',
            confidence=0.75,
            state_probs=None,
            features={'ts_utc': ts, 'vix_synth_30d': 19.0},
            ts_utc=ts,
            transition_tag='INTRADAY_HMM_REDEPLOY_LOW_VOL_TRANSITIONING',
        )
        regime_file = tmp_path / 'regime_latest.json'
        assert regime_file.exists()
        body = json.loads(regime_file.read_text())
        assert body['state'] == 'TRANSITIONING'
        assert body['vix_level'] == 19.0
