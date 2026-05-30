import pytest
from execution import backtest_coupled_recs as bc


def test_candidate_pct_uses_default_when_no_base():
    assert bc.candidate_pct(base=None, delta=0.10, default=0.07) == pytest.approx(0.077)


def test_candidate_pct_uses_existing_base():
    assert bc.candidate_pct(base=0.05, delta=0.20, default=0.07) == pytest.approx(0.06)


def test_candidate_pct_clamped():
    assert bc.candidate_pct(base=0.07, delta=5.0, default=0.07) == 0.30
    assert bc.candidate_pct(base=0.07, delta=-0.99, default=0.07) == 0.01


def test_candidate_pct_none_delta_returns_none():
    assert bc.candidate_pct(base=0.07, delta=None, default=0.07) is None


def test_candidate_pct_noise_delta_returns_none():
    assert bc.candidate_pct(base=0.07, delta=0.004, default=0.07) is None


def test_accept_rule():
    assert bc.qualifies(baseline_sharpe=0.50, candidate_sharpe=0.61, candidate_n_trades=30) is True
    assert bc.qualifies(baseline_sharpe=0.50, candidate_sharpe=0.59, candidate_n_trades=30) is False
    assert bc.qualifies(baseline_sharpe=0.50, candidate_sharpe=0.61, candidate_n_trades=29) is False
    assert bc.qualifies(baseline_sharpe=0.50, candidate_sharpe=0.65, candidate_n_trades=100) is True


def test_has_actionable_delta():
    assert bc.has_actionable_delta({'stop_delta_pct': None, 'target_delta_pct': None}) is False
    assert bc.has_actionable_delta({'stop_delta_pct': 0.05, 'target_delta_pct': None}) is True
    assert bc.has_actionable_delta({'stop_delta_pct': 0.004, 'target_delta_pct': None}) is False
