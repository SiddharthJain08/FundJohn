from strategies.implementations.oxf_hull_ma import OxfHullMa

def test_id_and_class():
    s = OxfHullMa()
    assert s.id == 'oxf_hull_ma'
    assert s.instrument_class == 'etp'
    assert set(s.active_in_regimes) <= {'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}
