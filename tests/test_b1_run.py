import os
from execution.b1_run import bucket_of, run, _resolve_session

def test_bucket_mapping_rth():
    assert bucket_of("2026-06-01T13:30:00+00:00") == 0     # 9:30 ET = 13:30 UTC
    assert bucket_of("2026-06-01T14:00:00+00:00") == 1
    assert bucket_of("2026-06-01T19:30:00+00:00") == 12    # 15:30 ET
    assert bucket_of("2026-06-01T20:00:00+00:00") is None  # 16:00 close — not a working bucket

def test_resolve_session_next_session_reconstructs_t_plus_1():
    by_date = {'2026-05-28': {}, '2026-05-29': {}, '2026-06-01': {}}
    # case study: signal_date 2026-05-28 → worked session = first STRICTLY after = 05-29
    assert _resolve_session(by_date, '2026-05-28', True) == '2026-05-29'
    # weekend/holiday gap handled for free (Fri 05-29 → next is Mon 06-01)
    assert _resolve_session(by_date, '2026-05-29', True) == '2026-06-01'
    # no later session → None (skip, never impute)
    assert _resolve_session(by_date, '2026-06-01', True) is None

def test_resolve_session_exact_for_live():
    by_date = {'2026-06-02': {}}
    assert _resolve_session(by_date, '2026-06-02', False) == '2026-06-02'
    assert _resolve_session(by_date, '2026-06-03', False) is None  # not present → skip

def test_run_noop_when_gate_off(monkeypatch):
    monkeypatch.delenv('OPENCLAW_B1_SHADOW', raising=False)
    assert run(mode='case_study') == {'status': 'gate_off', 'persisted': 0}
