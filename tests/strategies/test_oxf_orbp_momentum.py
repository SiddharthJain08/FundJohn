from strategies.implementations.oxf_orbp_momentum import OxfOrbpMomentum

def test_id_instrument_class_regimes_and_adaptation():
    s = OxfOrbpMomentum()
    assert s.id == 'oxf_orbp_momentum'
    assert s.instrument_class == 'etp'
    assert set(s.active_in_regimes) <= {'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}
    assert 'adaptation' in s.description.lower()
