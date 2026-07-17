from strategies.implementations.oxf_smash_day_b import OxfSmashDayB

def test_id_instrument_class_regimes_and_adaptation():
    s = OxfSmashDayB()
    assert s.id == 'oxf_smash_day_b'
    assert s.instrument_class == 'etp'
    assert set(s.active_in_regimes) <= {'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}
    assert 'adaptation' in s.description.lower()
