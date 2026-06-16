from strategies.implementations.oxf_price_momentum import OxfPriceMomentum

def test_id_and_class():
    s = OxfPriceMomentum()
    assert s.id == 'oxf_price_momentum'
    assert s.instrument_class == 'etp'
    assert set(s.active_in_regimes) <= {'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}
