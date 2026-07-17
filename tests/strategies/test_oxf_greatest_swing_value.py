from strategies.implementations.oxf_greatest_swing_value import OxfGreatestSwingValue

def test_id_instrument_class_regimes_and_adaptation():
    s = OxfGreatestSwingValue()
    assert s.id == 'oxf_greatest_swing_value'
    assert s.instrument_class == 'etp'
    assert set(s.active_in_regimes) <= {'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}
    assert 'adaptation' in s.description.lower()
