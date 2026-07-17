from strategies.implementations.oxf_bull_oops import OxfBullOops

def test_id_instrument_class_regimes_and_adaptation():
    s = OxfBullOops()
    assert s.id == 'oxf_bull_oops'
    assert s.instrument_class == 'etp'
    assert set(s.active_in_regimes) <= {'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}
    assert 'adaptation' in s.description.lower()
