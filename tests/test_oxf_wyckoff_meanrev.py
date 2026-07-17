from strategies.implementations.oxf_wyckoff_meanrev import OxfWyckoffMeanrev

CANON = {'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}

def test_id():
    assert OxfWyckoffMeanrev.id == 'oxf_wyckoff_meanrev'

def test_instrument_class_etp():
    assert OxfWyckoffMeanrev.instrument_class == 'etp'

def test_signal_frequency_daily():
    assert OxfWyckoffMeanrev.signal_frequency == 'daily'

def test_regimes_subset_of_canonical():
    assert set(OxfWyckoffMeanrev.active_in_regimes) <= CANON
    assert len(OxfWyckoffMeanrev.active_in_regimes) >= 1

def test_max_signals():
    assert OxfWyckoffMeanrev.MAX_SIGNALS == 25
