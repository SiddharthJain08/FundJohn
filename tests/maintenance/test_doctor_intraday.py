"""tests/test_doctor_intraday.py

Tests for the two doctor.py checks added in Stage 5:
  - check_intraday_features_freshness
  - check_intraday_hmm_calibration

Note: freshness tests skip themselves outside RTH on weekdays (the
function correctly no-ops then). Calibration tests use module-scope
fake classes so pickle works.

Run:
    pytest tests/test_doctor_intraday.py -v
"""
from __future__ import annotations

import pickle
import sys
from datetime import datetime as _dt
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from maintenance import doctor as doc  # noqa: E402

PASS = doc.PASS
WARN = doc.WARN


# Module-level fake-model classes so pickle can serialise/deserialise.

class _FakeModelOnFeature:
    """Fake HMM with single-feature feature_means_ for drift testing."""
    feature_means_ = {'vix_synth_30d': 15.0}
    feature_names_ = ['vix_synth_30d']


class _FakeModelTwoFeatures:
    feature_means_ = {'vix_synth_30d': 17.0, 'vix_synth_90d': 18.0}
    feature_names_ = ['vix_synth_30d', 'vix_synth_90d']


def _set_root(monkeypatch, tmp_path):
    """Repoint doc.ROOT to a tmp dir so tests don't touch real artifacts."""
    monkeypatch.setattr(doc, 'ROOT', tmp_path)
    (tmp_path / 'data' / 'master').mkdir(parents=True, exist_ok=True)
    (tmp_path / '.agents' / 'market-state').mkdir(parents=True, exist_ok=True)


def _is_rth_now() -> bool:
    now_et = _dt.now(ZoneInfo('America/New_York'))
    if now_et.weekday() >= 5:
        return False
    rth_start = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    rth_end   = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    return rth_start <= now_et <= rth_end


# ── intraday_features_freshness ──────────────────────────────────────────────

class TestFreshness:
    def test_no_op_outside_rth(self, monkeypatch, tmp_path):
        """Sanity: if we happen to be outside RTH right now, the check
        no-ops cleanly (PASS with skipped detail)."""
        if _is_rth_now():
            pytest.skip('inside RTH; this assertion only meaningful outside')
        _set_root(monkeypatch, tmp_path)
        result = doc.check_intraday_features_freshness()
        assert result['severity'] == PASS
        assert ('weekend' in result['detail'].lower() or
                'outside RTH' in result['detail'])

    def test_missing_parquet_during_rth_warns(self, monkeypatch, tmp_path):
        if not _is_rth_now():
            pytest.skip('outside RTH; check no-ops')
        _set_root(monkeypatch, tmp_path)
        result = doc.check_intraday_features_freshness()
        assert result['severity'] == WARN
        assert 'parquet missing' in result['detail']

    def test_fresh_row_during_rth_passes(self, monkeypatch, tmp_path):
        if not _is_rth_now():
            pytest.skip('outside RTH')
        _set_root(monkeypatch, tmp_path)
        parquet = tmp_path / 'data' / 'master' / 'intraday_features.parquet'
        df = pd.DataFrame({'ts_utc': [pd.Timestamp.now(tz='UTC') - pd.Timedelta(minutes=5)]})
        df.to_parquet(parquet, index=False)
        result = doc.check_intraday_features_freshness()
        assert result['severity'] == PASS
        assert 'min ago' in result['detail']

    def test_stale_row_during_rth_warns(self, monkeypatch, tmp_path):
        if not _is_rth_now():
            pytest.skip('outside RTH')
        _set_root(monkeypatch, tmp_path)
        parquet = tmp_path / 'data' / 'master' / 'intraday_features.parquet'
        df = pd.DataFrame({'ts_utc': [pd.Timestamp.now(tz='UTC') - pd.Timedelta(minutes=30)]})
        df.to_parquet(parquet, index=False)
        result = doc.check_intraday_features_freshness()
        assert result['severity'] == WARN


# ── intraday_hmm_calibration ─────────────────────────────────────────────────

class TestCalibration:
    def test_no_model_passes_bootstrap(self, monkeypatch, tmp_path):
        _set_root(monkeypatch, tmp_path)
        result = doc.check_intraday_hmm_calibration()
        assert result['severity'] == PASS
        assert 'bootstrap' in result['detail'].lower()

    def test_model_within_drift_threshold(self, monkeypatch, tmp_path):
        _set_root(monkeypatch, tmp_path)
        model_path = tmp_path / '.agents' / 'market-state' / 'hmm_intraday_latest.pkl'
        with open(model_path, 'wb') as f:
            pickle.dump(_FakeModelTwoFeatures(), f)
        parquet = tmp_path / 'data' / 'master' / 'intraday_features.parquet'
        rows = [{'ts_utc': pd.Timestamp.now(tz='UTC') - pd.Timedelta(hours=i),
                 'vix_synth_30d': 17.5, 'vix_synth_90d': 18.5}
                for i in range(100)]
        pd.DataFrame(rows).to_parquet(parquet, index=False)
        result = doc.check_intraday_hmm_calibration()
        assert result['severity'] == PASS
        assert 'within 50%' in result['detail']

    def test_model_drift_warns(self, monkeypatch, tmp_path):
        _set_root(monkeypatch, tmp_path)
        model_path = tmp_path / '.agents' / 'market-state' / 'hmm_intraday_latest.pkl'
        with open(model_path, 'wb') as f:
            pickle.dump(_FakeModelOnFeature(), f)
        parquet = tmp_path / 'data' / 'master' / 'intraday_features.parquet'
        rows = [{'ts_utc': pd.Timestamp.now(tz='UTC') - pd.Timedelta(hours=i),
                 'vix_synth_30d': 45.0}
                for i in range(50)]
        pd.DataFrame(rows).to_parquet(parquet, index=False)
        result = doc.check_intraday_hmm_calibration()
        assert result['severity'] == WARN
        assert 'vix_synth_30d' in result['detail']
