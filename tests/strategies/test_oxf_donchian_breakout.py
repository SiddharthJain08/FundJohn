import pandas as pd
from strategies.implementations.oxf_donchian_breakout import OxfDonchianBreakout

def test_instantiates_and_declares_id():
    s = OxfDonchianBreakout()
    assert s.id == 'oxf_donchian_breakout'
    assert s.instrument_class == 'etp'
    assert set(s.active_in_regimes) <= {'LOW_VOL','TRANSITIONING','HIGH_VOL','CRISIS'}
