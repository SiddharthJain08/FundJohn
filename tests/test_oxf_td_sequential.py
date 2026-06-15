from strategies.implementations.oxf_td_sequential import OxfTdSequential

CANON = {'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}

def test_id():
    assert OxfTdSequential.id == 'oxf_td_sequential'

def test_instrument_class_etp():
    assert OxfTdSequential.instrument_class == 'etp'

def test_signal_frequency_daily():
    assert OxfTdSequential.signal_frequency == 'daily'

def test_regimes_subset_of_canonical():
    assert set(OxfTdSequential.active_in_regimes) <= CANON
    assert len(OxfTdSequential.active_in_regimes) >= 1

def test_max_signals():
    assert OxfTdSequential.MAX_SIGNALS == 25
