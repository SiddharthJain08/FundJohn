from strategies.implementations.oxf_dow_theory import OxfDowTheory

CANON = {'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}

def test_id():
    assert OxfDowTheory.id == 'oxf_dow_theory'

def test_instrument_class_etp():
    assert OxfDowTheory.instrument_class == 'etp'

def test_signal_frequency_daily():
    assert OxfDowTheory.signal_frequency == 'daily'

def test_regimes_subset_of_canonical():
    assert set(OxfDowTheory.active_in_regimes) <= CANON
    assert len(OxfDowTheory.active_in_regimes) >= 1

def test_max_signals():
    assert OxfDowTheory.MAX_SIGNALS == 25
