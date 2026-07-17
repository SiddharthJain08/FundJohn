from strategies.implementations.oxf_gap_a import OxfGapA

def test_id_instrument_class_regimes_and_adaptation():
    s = OxfGapA()
    assert s.id == 'oxf_gap_a'
    assert s.instrument_class == 'etp'
    assert set(s.active_in_regimes) <= {'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}
    assert 'adaptation' in s.description.lower()
