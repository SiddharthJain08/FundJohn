from strategies.implementations.oxf_zero_lag_ma import OxfZeroLagMa

def test_id_and_class():
    s = OxfZeroLagMa()
    assert s.id == 'oxf_zero_lag_ma'
    assert s.instrument_class == 'etp'
    assert set(s.active_in_regimes) <= {'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}
