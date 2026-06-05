from execution.b1_ledger import build_report

def test_report_classifies_beat_tie_loss_vs_both_baselines():
    rows = [
        # beats both naive and base
        {'exec_ledger': 10.0, 'naive_ledger': 2.0, 'vwap_base_ledger': 4.0,
         'completion': 1.0, 'regime_state': 'LOW_VOL'},
        # beats neither: loses to naive (1.0) and to VWAP-base (-4.0 > -5.0)
        {'exec_ledger': -5.0, 'naive_ledger': 1.0, 'vwap_base_ledger': -4.0,
         'completion': 1.0, 'regime_state': 'LOW_VOL'},
    ]
    rep = build_report(rows)
    assert rep['n'] == 2
    assert rep['beats_naive'] == 1
    assert rep['beats_base'] == 1
    assert 'LOW_VOL' in rep['by_regime']
    assert abs(rep['total_exec_ledger'] - 5.0) < 1e-9
