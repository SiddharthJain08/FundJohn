from strategies.implementations.oxf_false_breakout import OxfFalseBreakout

def test_id_instrument_class_regimes_and_adaptation():
    s = OxfFalseBreakout()
    assert s.id == 'oxf_false_breakout'
    assert s.instrument_class == 'etp'
    assert set(s.active_in_regimes) <= {'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}
    assert 'adaptation' in s.description.lower()
