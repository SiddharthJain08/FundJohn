"""Phase 2H tests: doctor regime_correlation_coverage check."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from maintenance import doctor as doc  # noqa: E402


def _stub(monkeypatch, coverage=None, weights=None):
    monkeypatch.setattr(doc, '_query_latest_regime_coverage',
                          lambda: (coverage, weights))


def test_no_2h_rows_yet_returns_pass(monkeypatch):
    _stub(monkeypatch, coverage=None, weights=None)
    r = doc.check_regime_correlation_coverage()
    assert r['severity'] == doc.PASS
    assert 'no 2H rows yet' in r['detail']


def test_all_real_returns_pass(monkeypatch):
    _stub(monkeypatch,
          coverage={'LOW_VOL': 'real', 'TRANSITIONING': 'real',
                     'HIGH_VOL': 'real', 'CRISIS': 'real'},
          weights={'LOW_VOL': 0.1, 'TRANSITIONING': 0.7,
                    'HIGH_VOL': 0.1, 'CRISIS': 0.1})
    r = doc.check_regime_correlation_coverage()
    assert r['severity'] == doc.PASS


def test_crisis_stress_prior_with_low_prob_returns_warn(monkeypatch):
    """Stress prior is fine when crisis probability is low."""
    _stub(monkeypatch,
          coverage={'LOW_VOL': 'real', 'TRANSITIONING': 'real',
                     'HIGH_VOL': 'real', 'CRISIS': 'stress_prior'},
          weights={'LOW_VOL': 0.001, 'TRANSITIONING': 0.998,
                    'HIGH_VOL': 0.0, 'CRISIS': 0.001})
    r = doc.check_regime_correlation_coverage()
    assert r['severity'] == doc.WARN
    assert 'CRISIS=stress_prior' in r['detail']


def test_crisis_stress_prior_with_high_prob_returns_fail(monkeypatch):
    """Made-up correlation + high crisis probability = FAIL."""
    _stub(monkeypatch,
          coverage={'LOW_VOL': 'real', 'TRANSITIONING': 'real',
                     'HIGH_VOL': 'fallback_global', 'CRISIS': 'stress_prior'},
          weights={'LOW_VOL': 0.05, 'TRANSITIONING': 0.30,
                    'HIGH_VOL': 0.15, 'CRISIS': 0.50})
    r = doc.check_regime_correlation_coverage()
    assert r['severity'] == doc.FAIL
    assert 'p(CRISIS)=0.50' in r['detail']


def test_fallback_global_only_returns_warn(monkeypatch):
    _stub(monkeypatch,
          coverage={'LOW_VOL': 'fallback_global', 'TRANSITIONING': 'real',
                     'HIGH_VOL': 'fallback_global', 'CRISIS': 'real'},
          weights={'LOW_VOL': 0.001, 'TRANSITIONING': 0.99,
                    'HIGH_VOL': 0.0, 'CRISIS': 0.009})
    r = doc.check_regime_correlation_coverage()
    assert r['severity'] == doc.WARN
    assert 'fallback_global' in r['detail']


def test_db_error_returns_warn(monkeypatch):
    def boom():
        raise RuntimeError('db down')
    monkeypatch.setattr(doc, '_query_latest_regime_coverage', boom)
    r = doc.check_regime_correlation_coverage()
    assert r['severity'] == doc.WARN
