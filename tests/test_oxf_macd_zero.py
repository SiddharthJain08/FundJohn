from strategies.implementations.oxf_macd_zero import OxfMacdZero

def test_id_and_class():
    s = OxfMacdZero()
    assert s.id == 'oxf_macd_zero'
    assert s.instrument_class == 'etp'
    assert set(s.active_in_regimes) <= {'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}
