from strategies.implementations.oxf_hook import OxfHook

def test_id_instrument_class_regimes_and_adaptation():
    s = OxfHook()
    assert s.id == 'oxf_hook'
    assert s.instrument_class == 'etp'
    assert set(s.active_in_regimes) <= {'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}
    assert 'adaptation' in s.description.lower()
