from strategies.implementations.oxf_welles_wilder_breakout import OxfWellesWilderBreakout

def test_id_instrument_class_regimes_and_adaptation():
    s = OxfWellesWilderBreakout()
    assert s.id == 'oxf_welles_wilder_breakout'
    assert s.instrument_class == 'etp'
    assert set(s.active_in_regimes) <= {'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}
    assert 'adaptation' in s.description.lower()
