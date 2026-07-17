import importlib.util
from pathlib import Path

def _load(monkeypatch):
    monkeypatch.setenv('OPENCLAW_INTRADAY_15MIN_PREFETCH', '1')
    spec = importlib.util.spec_from_file_location(
        'rims', Path('/root/openclaw/scripts/run_intraday_market_state.py'))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m

def test_is_candidate_true_on_first_new_tick(monkeypatch):
    m = _load(monkeypatch)
    assert m._is_candidate_transition(
        settled='LOW_VOL', state='HIGH_VOL', streak=1,
        confidence=0.95, market_open=True) is True

def test_is_candidate_false_when_streak_gt1_or_lowconf_or_closed_or_same(monkeypatch):
    m = _load(monkeypatch)
    assert m._is_candidate_transition('LOW_VOL', 'HIGH_VOL', 2, 0.95, True) is False
    assert m._is_candidate_transition('LOW_VOL', 'HIGH_VOL', 1, 0.50, True) is False
    assert m._is_candidate_transition('LOW_VOL', 'HIGH_VOL', 1, 0.95, False) is False
    assert m._is_candidate_transition('HIGH_VOL', 'HIGH_VOL', 1, 0.95, True) is False
    assert m._is_candidate_transition(None, 'HIGH_VOL', 1, 0.95, True) is False
