from strategies.implementations.oxf_vortex import OxfVortex

def test_id_and_class():
    s = OxfVortex()
    assert s.id == 'oxf_vortex'
    assert s.instrument_class == 'etp'
    assert set(s.active_in_regimes) <= {'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}
