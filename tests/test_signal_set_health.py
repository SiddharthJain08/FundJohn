# tests/test_signal_set_health.py — pure gate for "is the intraday redeploy's active
# signal set abnormally thin?" (W3 F2a). No DB, no I/O.
from src.execution.signal_set_health import recent_baseline, is_signal_set_thin

def test_recent_baseline_median():
    assert recent_baseline([10, 20, 30]) == 20
    assert recent_baseline([10, 20, 30, 40]) == 25
    assert recent_baseline([]) == 0.0

def test_thin_below_floor():
    # below the absolute floor → thin regardless of baseline
    assert is_signal_set_thin(5, baseline_count=100, floor=10, frac=0.30) is True

def test_thin_below_frac_of_baseline():
    # 25 < 0.30*100=30 → thin
    assert is_signal_set_thin(25, baseline_count=100, floor=10, frac=0.30) is True

def test_healthy_set_not_thin():
    # 40 >= max(10, 30) → healthy
    assert is_signal_set_thin(40, baseline_count=100, floor=10, frac=0.30) is False

def test_no_baseline_uses_floor_only():
    # baseline<=0 → only the floor applies
    assert is_signal_set_thin(8, baseline_count=0, floor=10, frac=0.30) is True
    assert is_signal_set_thin(12, baseline_count=0, floor=10, frac=0.30) is False
