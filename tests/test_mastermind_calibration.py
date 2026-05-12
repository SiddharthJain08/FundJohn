"""Tests for mastermind_calibration — proposal outcome tracking + report."""
from __future__ import annotations
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from metrics import mastermind_calibration as cal  # noqa: E402


def test_brier_score_perfect_predictions():
    """Brier = 0 when every confidence matches outcome perfectly."""
    obs = [
        {'confidence': 1.0, 'direction_match': True},
        {'confidence': 0.0, 'direction_match': False},
    ]
    assert cal._brier_score(obs) == 0.0


def test_brier_score_worst_case():
    """Brier = 1 when confidence=1 but outcome is False (and vice versa)."""
    obs = [
        {'confidence': 1.0, 'direction_match': False},
        {'confidence': 0.0, 'direction_match': True},
    ]
    assert cal._brier_score(obs) == 1.0


def test_brier_score_partial_calibration():
    """Confidence 0.5 on every trial with 50/50 outcome → Brier = 0.25."""
    obs = [
        {'confidence': 0.5, 'direction_match': True},
        {'confidence': 0.5, 'direction_match': False},
    ]
    assert cal._brier_score(obs) == 0.25


def test_bucket_aggregates():
    """Buckets confidence into [0-0.2, 0.2-0.4, …], reports fraction matched per bucket."""
    obs = [
        {'confidence': 0.85, 'direction_match': True},
        {'confidence': 0.90, 'direction_match': True},
        {'confidence': 0.95, 'direction_match': False},  # high-conf miss
        {'confidence': 0.30, 'direction_match': True},
        {'confidence': 0.10, 'direction_match': False},
    ]
    buckets = cal._bucket_aggregates(obs)
    # 0.8-1.0 bucket: 3 obs, 2 matched = 0.667
    high = next(b for b in buckets if b['range'] == '[0.8, 1.0]')
    assert high['count'] == 3
    assert abs(high['match_rate'] - 2 / 3) < 0.01


def test_direction_match_for_size_up_proposal():
    """size_up proposal + post-decision Sharpe rose → match."""
    assert cal._direction_match(
        proposal={'proposed_size_scalar': 0.7, 'proposed_eligible': None,
                  'current_size_scalar': 0.5},
        live_sharpe_pre=1.0,
        live_sharpe_post=1.5,
    ) is True


def test_direction_match_for_eligibility_off_proposal():
    """eligibility=False proposal: match if post Sharpe rose OR if no signals fired (was bleeding)."""
    # We approved making it ineligible; if pre-Sharpe was bad, that's a win.
    assert cal._direction_match(
        proposal={'proposed_eligible': False, 'proposed_size_scalar': None,
                  'current_size_scalar': None},
        live_sharpe_pre=-0.5,
        live_sharpe_post=0.0,  # no trades post — neutral; pre was bad → still "match"
    ) is True
