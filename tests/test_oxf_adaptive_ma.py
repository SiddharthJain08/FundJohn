from strategies.implementations.oxf_adaptive_ma import OxfAdaptiveMa

def test_id_and_class():
    s = OxfAdaptiveMa()
    assert s.id == 'oxf_adaptive_ma'
    assert s.instrument_class == 'etp'
    assert set(s.active_in_regimes) <= {'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}
