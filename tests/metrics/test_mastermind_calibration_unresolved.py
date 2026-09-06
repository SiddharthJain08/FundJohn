"""2026-09-06: outcomes whose decisive live window has no closed trades are
UNRESOLVED (direction_match None), not misses — and the report counts them
apart. Before this, 55 of 96 no-evidence outcomes were scored as misses at
~0.75 confidence and the Brier read 0.43 instead of 0.25."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from metrics import mastermind_calibration as cal  # noqa: E402


def test_expansion_without_post_evidence_is_unresolved():
    assert cal._direction_match(proposal={'proposed_eligible': True, 'proposed_size_scalar': None,
                                          'current_size_scalar': None},
                                live_sharpe_pre=0.4, live_sharpe_post=None) is None


def test_restriction_without_pre_evidence_is_unresolved():
    assert cal._direction_match(proposal={'proposed_eligible': False, 'proposed_size_scalar': None,
                                          'current_size_scalar': None},
                                live_sharpe_pre=None, live_sharpe_post=-1.0) is None


def test_size_change_needs_both_windows():
    base = {'proposed_eligible': None, 'proposed_size_scalar': 0.7, 'current_size_scalar': 0.5}
    assert cal._direction_match(proposal=base, live_sharpe_pre=None, live_sharpe_post=1.0) is None
    assert cal._direction_match(proposal=base, live_sharpe_pre=1.0, live_sharpe_post=None) is None
    assert cal._direction_match(proposal=base, live_sharpe_pre=1.0, live_sharpe_post=0.5) is False
    assert cal._direction_match(proposal=base, live_sharpe_pre=0.5, live_sharpe_post=1.0) is True


def test_resolved_evidence_still_scores():
    assert cal._direction_match(proposal={'proposed_eligible': True}, live_sharpe_pre=None, live_sharpe_post=0.2) is True
    assert cal._direction_match(proposal={'proposed_eligible': True}, live_sharpe_pre=None, live_sharpe_post=0.0) is False
    assert cal._direction_match(proposal={'proposed_eligible': False}, live_sharpe_pre=-0.3, live_sharpe_post=None) is True


def test_brier_and_report_ignore_unresolved(monkeypatch):
    obs = [{'confidence': 0.9, 'direction_match': None},
           {'confidence': 0.8, 'direction_match': True},
           {'confidence': 0.6, 'direction_match': False}]
    assert abs(cal._brier_score(obs) - ((0.8 - 1) ** 2 + (0.6 - 0) ** 2) / 2) < 1e-12

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql): pass
        def fetchall(self): return [(0.9, None, 'approved'), (0.8, True, 'approved'), (0.6, False, 'rejected')]

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _Cur()
    monkeypatch.setattr(cal, '_connect', lambda: _Conn())
    rep = cal.calibration_report()
    assert rep['total_observations'] == 3 and rep['resolved_observations'] == 2
    assert rep['hit_rate'] == 0.5 and abs(rep['mean_confidence'] - 0.7) < 1e-12
