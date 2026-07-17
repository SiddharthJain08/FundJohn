from strategies.implementations.oxf_frama import OxfFrama

def test_id_and_class():
    s = OxfFrama()
    assert s.id == 'oxf_frama'
    assert s.instrument_class == 'etp'
    assert set(s.active_in_regimes) <= {'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}
