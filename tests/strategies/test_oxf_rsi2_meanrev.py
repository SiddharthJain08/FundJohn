from strategies.implementations.oxf_rsi2_meanrev import OxfRsi2Meanrev

def test_id_and_class():
    s = OxfRsi2Meanrev()
    assert s.id == 'oxf_rsi2_meanrev'
    assert s.instrument_class == 'etp'
    # Mean-reversion variant: gated to TRANSITIONING / HIGH_VOL per the plan.
    assert set(s.active_in_regimes) <= {'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}
    assert set(s.active_in_regimes) == {'TRANSITIONING', 'HIGH_VOL'}
