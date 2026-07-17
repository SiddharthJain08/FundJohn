from strategies.implementations.oxf_heikin_ashi import OxfHeikinAshi

CANON = {'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}

def test_id():
    assert OxfHeikinAshi.id == 'oxf_heikin_ashi'

def test_instrument_class_etp():
    assert OxfHeikinAshi.instrument_class == 'etp'

def test_signal_frequency_daily():
    assert OxfHeikinAshi.signal_frequency == 'daily'

def test_regimes_subset_of_canonical():
    assert set(OxfHeikinAshi.active_in_regimes) <= CANON
    assert len(OxfHeikinAshi.active_in_regimes) >= 1

def test_max_signals():
    assert OxfHeikinAshi.MAX_SIGNALS == 25
