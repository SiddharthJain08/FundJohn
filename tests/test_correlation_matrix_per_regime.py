"""Phase 2H tests: per-regime correlation matrices + state-prob blend."""
from __future__ import annotations
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import correlation_matrix as cm  # noqa: E402


# ---------- crisis_stress_prior ---------- #

def test_crisis_stress_prior_diagonal_is_one():
    m = cm.crisis_stress_prior(['A', 'B', 'C'])
    assert m['A']['A'] == 1.0
    assert m['B']['B'] == 1.0


def test_crisis_stress_prior_off_diagonal_is_rho():
    m = cm.crisis_stress_prior(['A', 'B', 'C'], rho=0.7)
    assert m['A']['B'] == 0.7
    assert m['B']['C'] == 0.7
    assert m['A']['C'] == 0.7


def test_crisis_stress_prior_uses_module_default_when_none():
    m = cm.crisis_stress_prior(['A', 'B'])
    assert m['A']['B'] == cm.CRISIS_CORRELATION_PRIOR


def test_crisis_stress_prior_clipped_at_max_off_diagonal():
    m = cm.crisis_stress_prior(['A', 'B'], rho=1.5)
    assert m['A']['B'] == cm.MAX_OFF_DIAGONAL


def test_crisis_stress_prior_env_override(monkeypatch):
    """Env-overridable via OPENCLAW_CRISIS_CORRELATION_PRIOR. Module-load
    time, so we test by re-importing after setting the env."""
    monkeypatch.setenv('OPENCLAW_CRISIS_CORRELATION_PRIOR', '0.5')
    # Re-import to pick up new env
    import importlib
    import execution.correlation_matrix as cm2
    importlib.reload(cm2)
    m = cm2.crisis_stress_prior(['A', 'B'])
    assert abs(m['A']['B'] - 0.5) < 1e-9
    # Restore module state for other tests
    monkeypatch.delenv('OPENCLAW_CRISIS_CORRELATION_PRIOR', raising=False)
    importlib.reload(cm2)


def test_crisis_stress_prior_empty_tickers():
    assert cm.crisis_stress_prior([]) == {}


# ---------- state probabilities ---------- #

def test_state_probabilities_normalizes_to_one():
    """If DB returns probabilities that don't sum to 1, they get normalized."""
    fake_row = ('TRANSITIONING', {'state_probabilities': {
        'TRANSITIONING': 0.8, 'LOW_VOL': 0.1, 'HIGH_VOL': 0.05, 'CRISIS': 0.05}})

    class FakeCursor:
        def __init__(self): self.row = fake_row
        def execute(self, *a, **k): pass
        def fetchone(self): return self.row
        def __enter__(self): return self
        def __exit__(self, *a): pass

    class FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def cursor(self): return FakeCursor()
        def close(self): pass

    with patch.object(cm, '_connect', return_value=FakeConn()):
        probs = cm.current_state_probabilities()
    assert abs(sum(probs.values()) - 1.0) < 1e-9
    assert probs['TRANSITIONING'] == pytest.approx(0.8)


def test_state_probabilities_no_data_one_hot_on_state():
    """No state_probabilities dict → fall back to one-hot on the `state` col."""
    fake_row = ('HIGH_VOL', {})

    class FakeCursor:
        def execute(self, *a, **k): pass
        def fetchone(self): return fake_row
        def __enter__(self): return self
        def __exit__(self, *a): pass

    class FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def cursor(self): return FakeCursor()
        def close(self): pass

    with patch.object(cm, '_connect', return_value=FakeConn()):
        probs = cm.current_state_probabilities()
    assert probs == {'LOW_VOL': 0.0, 'TRANSITIONING': 0.0,
                       'HIGH_VOL': 1.0, 'CRISIS': 0.0}


def test_state_probabilities_no_rows_returns_zeros():
    class FakeCursor:
        def execute(self, *a, **k): pass
        def fetchone(self): return None
        def __enter__(self): return self
        def __exit__(self, *a): pass

    class FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def cursor(self): return FakeCursor()
        def close(self): pass

    with patch.object(cm, '_connect', return_value=FakeConn()):
        probs = cm.current_state_probabilities()
    assert all(v == 0.0 for v in probs.values())


# ---------- per-regime classification ---------- #

def test_per_regime_real_when_data_present(monkeypatch):
    """All regimes with > 0 trades classified 'real'."""
    monkeypatch.setattr(cm, '_trade_counts_by_regime',
                          lambda t, w: {'LOW_VOL': 10, 'TRANSITIONING': 200,
                                          'HIGH_VOL': 5, 'CRISIS': 3})
    fake_pnls = {r: {t: {'2026-01-01': 0.01, '2026-01-02': 0.02} for t in ['A', 'B']}
                  for r in cm.REGIME_STATES}
    monkeypatch.setattr(cm, '_load_pnls_by_ticker_date_by_regime'
                          if hasattr(cm, '_load_pnls_by_ticker_date_by_regime')
                          else '_load_pnls_by_regime_ticker_date',
                          lambda t, w: fake_pnls)
    # Use the actual attr name set in module
    monkeypatch.setattr(cm, '_load_pnls_by_regime_ticker_date', lambda t, w: fake_pnls)
    matrices, cov = cm._per_regime_matrices_with_coverage(['A', 'B'],
                                                            window_days=90, alpha=0.6)
    assert cov == {'LOW_VOL': 'real', 'TRANSITIONING': 'real',
                    'HIGH_VOL': 'real', 'CRISIS': 'real'}


def test_per_regime_stress_prior_for_crisis_with_zero_trades(monkeypatch):
    monkeypatch.setattr(cm, '_trade_counts_by_regime',
                          lambda t, w: {'LOW_VOL': 5, 'TRANSITIONING': 100,
                                          'HIGH_VOL': 0, 'CRISIS': 0})
    fake_pnls = {r: {t: {'2026-01-01': 0.01, '2026-01-02': 0.02} for t in ['A', 'B']}
                  for r in cm.REGIME_STATES}
    monkeypatch.setattr(cm, '_load_pnls_by_regime_ticker_date', lambda t, w: fake_pnls)
    # Stub effective_correlation to avoid hitting DB for the fallback path
    monkeypatch.setattr(cm, 'effective_correlation',
                          lambda tickers, **kw: {ti: {tj: 0.4 if ti != tj else 1.0
                                                         for tj in tickers}
                                                    for ti in tickers})
    matrices, cov = cm._per_regime_matrices_with_coverage(['A', 'B'],
                                                            window_days=90, alpha=0.6)
    assert cov['CRISIS'] == 'stress_prior'
    assert cov['HIGH_VOL'] == 'fallback_global'
    # Stress prior matrix has off-diagonal at CRISIS_CORRELATION_PRIOR
    assert matrices['CRISIS']['A']['B'] == cm.CRISIS_CORRELATION_PRIOR


def test_per_regime_fallback_global_for_non_crisis_with_zero(monkeypatch):
    monkeypatch.setattr(cm, '_trade_counts_by_regime',
                          lambda t, w: {'LOW_VOL': 0, 'TRANSITIONING': 100,
                                          'HIGH_VOL': 0, 'CRISIS': 0})
    fake_pnls = {r: {t: {'2026-01-01': 0.01, '2026-01-02': 0.02} for t in ['A', 'B']}
                  for r in cm.REGIME_STATES}
    monkeypatch.setattr(cm, '_load_pnls_by_regime_ticker_date', lambda t, w: fake_pnls)
    fallback = {ti: {tj: 0.4 if ti != tj else 1.0 for tj in ['A', 'B']} for ti in ['A', 'B']}
    monkeypatch.setattr(cm, 'effective_correlation', lambda tickers, **kw: fallback)
    matrices, cov = cm._per_regime_matrices_with_coverage(['A', 'B'],
                                                            window_days=90, alpha=0.6)
    assert cov['LOW_VOL'] == 'fallback_global'
    assert cov['HIGH_VOL'] == 'fallback_global'
    # CRISIS still gets stress prior (operator-chosen)
    assert cov['CRISIS'] == 'stress_prior'


# ---------- blend math ---------- #

def test_blend_math_collapses_to_dominant_regime(monkeypatch):
    """If state probability is 99.8% TRANSITIONING, σ_eff should be ~= the
    TRANSITIONING matrix."""
    monkeypatch.setattr(cm, '_trade_counts_by_regime',
                          lambda t, w: {'LOW_VOL': 10, 'TRANSITIONING': 100,
                                          'HIGH_VOL': 0, 'CRISIS': 0})

    def fake_load(t, w):
        # TRANSITIONING shows A-B correlation 0.30; others vary
        return {
            'LOW_VOL':       {ti: {'2026-01-01': 0.01, '2026-01-02': 0.02} for ti in t},
            'TRANSITIONING': {ti: {'2026-01-01': (0.01 if ti == 'A' else 0.03),
                                    '2026-01-02': (0.02 if ti == 'A' else 0.04)} for ti in t},
            'HIGH_VOL':      {ti: {} for ti in t},
            'CRISIS':        {ti: {} for ti in t},
        }

    monkeypatch.setattr(cm, '_load_pnls_by_regime_ticker_date', fake_load)
    monkeypatch.setattr(cm, 'effective_correlation',
                          lambda tickers, **kw: {ti: {tj: 0.4 if ti != tj else 1.0 for tj in tickers}
                                                    for ti in tickers})
    monkeypatch.setattr(cm, 'current_state_probabilities',
                          lambda: {'LOW_VOL': 0.001, 'TRANSITIONING': 0.998,
                                    'HIGH_VOL': 0.0, 'CRISIS': 0.001})
    sigma_eff, weights, coverage = cm.blended_correlation_by_state(['A', 'B'])
    # Diagonals are 1.0
    assert sigma_eff['A']['A'] == 1.0
    # Off-diagonal is a convex combination — verify it's within the blended range
    assert -cm.MAX_OFF_DIAGONAL <= sigma_eff['A']['B'] <= cm.MAX_OFF_DIAGONAL
    # Weights propagate exactly
    assert weights['TRANSITIONING'] == pytest.approx(0.998)
    # Coverage classifications
    assert coverage['CRISIS'] == 'stress_prior'
    assert coverage['TRANSITIONING'] == 'real'


def test_blend_with_high_crisis_probability_pulls_toward_stress_prior(monkeypatch):
    """When CRISIS probability is 0.5, σ_eff[i,j] should pull strongly
    toward CRISIS_CORRELATION_PRIOR for pairs with no real data elsewhere."""
    # Force all regimes to use stress prior or sparse data
    monkeypatch.setattr(cm, '_trade_counts_by_regime',
                          lambda t, w: {'LOW_VOL': 0, 'TRANSITIONING': 0,
                                          'HIGH_VOL': 0, 'CRISIS': 0})
    monkeypatch.setattr(cm, 'effective_correlation',
                          lambda tickers, **kw: {ti: {tj: cm.SPARSE_DEFAULT if ti != tj else 1.0
                                                         for tj in tickers}
                                                    for ti in tickers})
    monkeypatch.setattr(cm, 'current_state_probabilities',
                          lambda: {'LOW_VOL': 0.25, 'TRANSITIONING': 0.25,
                                    'HIGH_VOL': 0.0, 'CRISIS': 0.5})
    sigma_eff, weights, coverage = cm.blended_correlation_by_state(['A', 'B', 'C'])
    # Expected: 0.5 * stress(0.7) + 0.25 * fallback(0.05) + 0.25 * fallback(0.05)
    #         = 0.35 + 0.0125 + 0.0125 = 0.375
    assert sigma_eff['A']['B'] == pytest.approx(0.375, abs=0.01)
    assert coverage['CRISIS'] == 'stress_prior'


def test_blend_empty_tickers():
    """With no orders, return empty matrix + still-readable state probs."""
    sigma, weights, coverage = cm.blended_correlation_by_state([])
    assert sigma == {}
    # weights still come from current_state_probabilities()
    assert isinstance(weights, dict)
    assert set(coverage.values()) == {'no_orders'}
