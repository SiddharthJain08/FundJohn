from strategies.implementations.oxf_aroon_breakout import OxfAroonBreakout

CANON = {'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}

def test_id():
    assert OxfAroonBreakout.id == 'oxf_aroon_breakout'

def test_instrument_class_etp():
    assert OxfAroonBreakout.instrument_class == 'etp'

def test_signal_frequency_daily():
    assert OxfAroonBreakout.signal_frequency == 'daily'

def test_regimes_subset_of_canonical():
    assert set(OxfAroonBreakout.active_in_regimes) <= CANON
    assert len(OxfAroonBreakout.active_in_regimes) >= 1

def test_max_signals():
    assert OxfAroonBreakout.MAX_SIGNALS == 25
