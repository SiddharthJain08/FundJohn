from strategies.implementations.oxf_livermore import OxfLivermore

CANON = {'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}

def test_id():
    assert OxfLivermore.id == 'oxf_livermore'

def test_instrument_class_etp():
    assert OxfLivermore.instrument_class == 'etp'

def test_signal_frequency_daily():
    assert OxfLivermore.signal_frequency == 'daily'

def test_regimes_subset_of_canonical():
    assert set(OxfLivermore.active_in_regimes) <= CANON
    assert len(OxfLivermore.active_in_regimes) >= 1

def test_max_signals():
    assert OxfLivermore.MAX_SIGNALS == 25
