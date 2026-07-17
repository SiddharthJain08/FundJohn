from strategies.implementations.oxf_linreg_slope import OxfLinregSlope

def test_id_and_class():
    s = OxfLinregSlope()
    assert s.id == 'oxf_linreg_slope'
    assert s.instrument_class == 'etp'
    assert set(s.active_in_regimes) <= {'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}
