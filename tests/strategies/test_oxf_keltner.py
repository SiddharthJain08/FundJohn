from strategies.implementations.oxf_keltner import OxfKeltner

CANON = {'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}

def test_id():
    assert OxfKeltner.id == 'oxf_keltner'

def test_instrument_class_etp():
    assert OxfKeltner.instrument_class == 'etp'

def test_signal_frequency_daily():
    assert OxfKeltner.signal_frequency == 'daily'

def test_regimes_subset_of_canonical():
    assert set(OxfKeltner.active_in_regimes) <= CANON
    assert len(OxfKeltner.active_in_regimes) >= 1

def test_max_signals():
    assert OxfKeltner.MAX_SIGNALS == 25
