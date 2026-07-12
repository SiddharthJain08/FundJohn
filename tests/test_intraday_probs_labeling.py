"""Pin the 2026-07-12 regime-diagnostics fixes:

1. `_state_from_hmm` returns state probabilities labeled via the SAME
   internal-state→name map used to pick the state — NOT raw-index labels.
   (Bug: regime_latest.json / market_regime showed HIGH_VOL:1.0 while
   state=LOW_VOL because the HMM's internal state order isn't vol-ascending.)
2. `_refresh_regime_file` purges the retired daily-detector keys (frozen at
   the 2026-06-08 false-CRISIS values since the daily HMM stopped writing the
   file) and writes the labeled probability map verbatim.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np

# Load the script directly via importlib (it's a script, not a package).
_spec = importlib.util.spec_from_file_location(
    'run_intraday_market_state',
    Path(__file__).resolve().parents[1] / 'scripts' / 'run_intraday_market_state.py',
)
detector = importlib.util.module_from_spec(_spec)
sys.modules['run_intraday_market_state'] = detector
_spec.loader.exec_module(detector)


class _StubModel:
    """Minimal HMM stand-in: internal state order deliberately scrambled so
    raw-index labeling would mislabel (internal 2 = the calm cluster, as in
    the real fitted model that surfaced the bug)."""

    feature_names_ = ['vix_synth_30d', 'vix_synth_90d']
    feature_means_ = {'vix_synth_30d': 15.0, 'vix_synth_90d': 18.0}
    regime_name_by_state_ = {0: 'HIGH_VOL', 1: 'CRISIS',
                             2: 'LOW_VOL', 3: 'TRANSITIONING'}

    def __init__(self, probs):
        self._probs = np.asarray(probs, dtype=float)

    def predict_proba(self, x):
        return self._probs.reshape(1, -1)


class TestProbsLabeling:
    def test_labels_follow_name_map_not_raw_index(self):
        model = _StubModel([0.0, 0.0, 1.0, 0.0])   # internal state 2 wins
        state, conf, probs = detector._state_from_hmm(
            model, {'vix_synth_30d': 15.2, 'vix_synth_90d': 18.4})
        assert state == 'LOW_VOL'
        assert conf == 1.0
        # The winning probability must be labeled LOW_VOL — the raw-index
        # labeling this replaces would have produced HIGH_VOL: 1.0 here.
        assert probs == {'HIGH_VOL': 0.0, 'CRISIS': 0.0,
                         'LOW_VOL': 1.0, 'TRANSITIONING': 0.0}

    def test_argsort_fallback_labels_by_vol_rank(self):
        model = _StubModel([0.1, 0.2, 0.6, 0.1])
        del_attr = model.regime_name_by_state_
        model.regime_name_by_state_ = None
        try:
            # No trainer map -> fallback sorts internal states by mean of the
            # first feature column ascending. means_ col0: state2 lowest ->
            # LOW_VOL, state0 -> TRANSITIONING, state3 -> HIGH_VOL, state1 -> CRISIS.
            model.means_ = np.array([[16.0], [30.0], [12.0], [22.0]])
            state, conf, probs = detector._state_from_hmm(
                model, {'vix_synth_30d': 15.2, 'vix_synth_90d': 18.4})
            assert state == 'LOW_VOL'          # argmax = internal 2 = lowest mean
            assert probs == {'TRANSITIONING': 0.1, 'CRISIS': 0.2,
                             'LOW_VOL': 0.6, 'HIGH_VOL': 0.1}
        finally:
            model.regime_name_by_state_ = del_attr

    def test_probs_dict_state_and_confidence_consistent(self):
        model = _StubModel([0.05, 0.05, 0.7, 0.2])
        state, conf, probs = detector._state_from_hmm(
            model, {'vix_synth_30d': 15.2, 'vix_synth_90d': 18.4})
        assert probs[state] == max(probs.values()) == round(conf, 4)
        assert abs(sum(probs.values()) - 1.0) < 1e-9


RETIRED_KEYS = ('date', 'stress_score', 'roro_score',
                'transition_probs_tomorrow', 'vix_percentile', 'features',
                'days_in_current_state', 'position_scale',
                'regime_change_alert', 'refit_performed', 'resync_note',
                'notes', 'candidates_identified', 'active_strategies')

_FROZEN_DAILY = {
    'date': '2026-06-08', 'stress_score': 82, 'roro_score': -41.4,
    'transition_probs_tomorrow': {'CRISIS': 0.8685},
    'vix_percentile': 91.0, 'features': {'vix': 21.51},
    'position_scale': 0.4, 'notes': 'frozen daily block',
}


class TestRegimeFilePurgeAndProbs:
    def _write_and_reload(self, tmp_path, monkeypatch, **kwargs):
        monkeypatch.setattr(detector, 'MODEL_DIR', tmp_path)
        regime_file = tmp_path / 'regime_latest.json'
        seed = dict(_FROZEN_DAILY)
        seed.update({'state': 'LOW_VOL', 'state_probabilities':
                     {'HIGH_VOL': 1.0}})   # the historical mislabeled map
        regime_file.write_text(json.dumps(seed))
        detector._refresh_regime_file(**kwargs)
        return json.loads(regime_file.read_text())

    def test_purges_retired_daily_keys_and_writes_labeled_probs(self, tmp_path, monkeypatch):
        out = self._write_and_reload(
            tmp_path, monkeypatch,
            state='LOW_VOL', confidence=1.0, vix=15.17,
            prior_state='LOW_VOL',
            state_probs={'LOW_VOL': 1.0, 'TRANSITIONING': 0.0,
                         'HIGH_VOL': 0.0, 'CRISIS': 0.0},
            ts_utc='2026-07-13 13:00:00+00:00', transition_tag=None)
        for k in RETIRED_KEYS:
            assert k not in out, f'retired daily key survived: {k}'
        assert out['state'] == 'LOW_VOL'
        assert out['vix_level'] == 15.17
        assert out['state_probabilities'] == {
            'LOW_VOL': 1.0, 'TRANSITIONING': 0.0, 'HIGH_VOL': 0.0, 'CRISIS': 0.0}
        assert out['intraday_source'] == 'intraday_hmm'

    def test_carry_forward_none_probs_still_purges_and_preserves_state(self, tmp_path, monkeypatch):
        out = self._write_and_reload(
            tmp_path, monkeypatch,
            state='LOW_VOL', confidence=1.0, vix=None,
            prior_state='LOW_VOL', state_probs=None,
            ts_utc='2026-07-13 01:00:00+00:00', transition_tag=None)
        for k in RETIRED_KEYS:
            assert k not in out, f'retired daily key survived carry-forward: {k}'
        # Merge semantics: existing probabilities key is preserved when the
        # tick carries no fresh posterior (matches pre-fix behavior).
        assert out['state_probabilities'] == {'HIGH_VOL': 1.0}
        assert out['state'] == 'LOW_VOL'

    def test_unknown_state_advances_freshness_but_keeps_last_good_state(self, tmp_path, monkeypatch):
        out = self._write_and_reload(
            tmp_path, monkeypatch,
            state='UNKNOWN', confidence=0.0, vix=None,
            prior_state=None, state_probs=None,
            ts_utc='2026-07-13 01:05:00+00:00', transition_tag=None)
        assert out['state'] == 'LOW_VOL'       # preserved, not overwritten
        assert out['intraday_updated_at'] == '2026-07-13 01:05:00+00:00'
        for k in RETIRED_KEYS:
            assert k not in out
