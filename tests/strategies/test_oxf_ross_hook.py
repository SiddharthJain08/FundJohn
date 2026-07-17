from strategies.implementations.oxf_ross_hook import OxfRossHook

def test_id_instrument_class_regimes_and_adaptation():
    s = OxfRossHook()
    assert s.id == 'oxf_ross_hook'
    assert s.instrument_class == 'etp'
    assert set(s.active_in_regimes) <= {'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}
    assert 'adaptation' in s.description.lower()
