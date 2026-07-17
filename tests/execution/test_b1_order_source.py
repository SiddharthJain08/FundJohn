from execution.b1_order_source import si_from_pct, SIZE_CAP

def test_si_normalizes_to_unit_interval():
    assert si_from_pct(None) == 0.0
    assert si_from_pct(0.0) == 0.0
    assert abs(si_from_pct(SIZE_CAP) - 1.0) < 1e-9
    assert si_from_pct(SIZE_CAP * 2) == 1.0      # clamps at 1
    assert abs(si_from_pct(SIZE_CAP / 2) - 0.5) < 1e-9
