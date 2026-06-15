from strategies.implementations.oxf_bollinger_momentum import OxfBollingerMomentum

CANON = {'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}

def test_id():
    assert OxfBollingerMomentum.id == 'oxf_bollinger_momentum'

def test_instrument_class_etp():
    assert OxfBollingerMomentum.instrument_class == 'etp'

def test_signal_frequency_daily():
    assert OxfBollingerMomentum.signal_frequency == 'daily'

def test_regimes_subset_of_canonical():
    assert set(OxfBollingerMomentum.active_in_regimes) <= CANON
    assert len(OxfBollingerMomentum.active_in_regimes) >= 1

def test_max_signals():
    assert OxfBollingerMomentum.MAX_SIGNALS == 25
