from strategies.implementations.oxf_dual_momentum_roc import OxfDualMomentumRoc

def test_id_and_class():
    s = OxfDualMomentumRoc()
    assert s.id == 'oxf_dual_momentum_roc'
    assert s.instrument_class == 'etp'
    assert set(s.active_in_regimes) <= {'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}
