from strategies.implementations.oxf_sma_filter import OxfSmaFilter

def test_id_and_class():
    s = OxfSmaFilter()
    assert s.id == 'oxf_sma_filter'
    assert s.instrument_class == 'etp'
    assert set(s.active_in_regimes) <= {'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}
