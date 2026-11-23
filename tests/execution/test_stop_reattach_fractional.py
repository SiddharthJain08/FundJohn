from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from execution import stop_reattach as sr


def test_helpers():
    assert sr._qty_str(3.0) == '3' and sr._qty_str(0.4) == '0.4'
    assert sr._qty_str(1.6) == '1.6' and sr._qty_str(0.0000001) == '0'
    assert sr._tif_for_qty(3.0) == 'gtc' and sr._tif_for_qty(0.4) == 'day'
    assert not sr._is_fractional_qty(3.0) and sr._is_fractional_qty(1.6)


def test_oco_body_whole_share_stays_gtc():
    b = sr._oco_order_body('F', 'sell', 3.0, 12.0, 10.0, 'c1')
    assert b['qty'] == '3' and b['time_in_force'] == 'gtc'


def test_oco_body_fractional_day():
    b = sr._oco_order_body('F', 'sell', 0.4, 12.0, 10.0, 'c1')
    assert b['qty'] == '0.4' and b['time_in_force'] == 'day'


def _stop_args(monkeypatch, qty):
    seen = {}
    def fake(args, timeout=15):
        seen['args'] = args
        return True, {'id': 'x'}, None
    monkeypatch.setattr(sr, '_run_cli', fake)
    sr.submit_protective_stop(ticker='F', position_side='long', qty=qty,
                              stop_price=10.0, dry_run=False)
    return seen['args']


def test_protective_stop_fractional_args(monkeypatch):
    a = _stop_args(monkeypatch, 0.4)
    assert a[a.index('--qty') + 1] == '0.4'
    assert a[a.index('--time-in-force') + 1] == 'day'


def test_protective_stop_whole_args(monkeypatch):
    a = _stop_args(monkeypatch, 3.0)
    assert a[a.index('--qty') + 1] == '3'
    assert a[a.index('--time-in-force') + 1] == 'gtc'


def _drive(monkeypatch, qty, active=None, dry_run=False, cli_ok=True):
    calls = {'oco': 0, 'cli': [], 'cancel': 0}
    monkeypatch.setattr(sr, 'fetch_active_stops', lambda: dict(active or {}))
    monkeypatch.setattr(sr, 'fetch_tp_covered', lambda linked_only=False: {})
    monkeypatch.setattr(sr, '_resolve_intended_bracket',
                        lambda conn, sym, side: {'stop': 10.0, 'target': 14.0})
    monkeypatch.setattr(sr, '_compute_new_stop', lambda pos, sub: (10.0, 'ok'))
    monkeypatch.setattr(sr, '_compute_new_target', lambda pos, sub: (14.0, 'ok'))
    def cancel(*a, **k):
        calls['cancel'] += 1
        return 1 if active else 0
    monkeypatch.setattr(sr, 'cancel_stops_for', cancel)
    monkeypatch.setattr(sr, '_wait_qty_freed', lambda *a, **k: True)
    def oco(**k):
        calls['oco'] += 1
        return {'status': 'submitted'}
    monkeypatch.setattr(sr, 'submit_protective_oco', oco)
    def cli(args, timeout=15):
        calls['cli'].append(args)
        return (True, {'id': 'x'}, None) if cli_ok else (False, None, {'error': 'no'})
    monkeypatch.setattr(sr, '_run_cli', cli)
    pos = [{'symbol': 'F', 'qty': str(qty), 'side': 'long'}]
    return sr.run_oco_reattach(None, pos, dry_run), calls


def test_run_oco_reattach_fractional_uses_stop_not_oco(monkeypatch):
    stats, calls = _drive(monkeypatch, 0.4)
    assert calls['oco'] == 0
    assert len(calls['cli']) == 1
    a = calls['cli'][0]
    assert a[a.index('--qty') + 1] == '0.4'
    assert a[a.index('--time-in-force') + 1] == 'day'
    assert stats['oco'] == 0


def test_run_oco_reattach_whole_share_still_oco(monkeypatch):
    stats, calls = _drive(monkeypatch, 3)
    assert calls['oco'] == 1 and stats['oco'] == 1 and not calls['cli']


def test_fractional_already_covered_skips(monkeypatch):
    stats, calls = _drive(monkeypatch, 0.4, active={'F': 0.4})
    assert not calls['cli'] and calls['oco'] == 0 and calls['cancel'] == 0
    assert stats['already_covered'] == 1 and stats.get('fractional_stop', 0) == 0


def test_fractional_stale_stop_cancelled_then_stop(monkeypatch):
    stats, calls = _drive(monkeypatch, 0.4, active={'F': 0.1})
    assert calls['cancel'] == 1 and calls['oco'] == 0 and len(calls['cli']) == 1
    a = calls['cli'][0]
    assert a[a.index('--time-in-force') + 1] == 'day'
    assert stats['fractional_stop'] == 1


def test_fractional_dry_run_counts_no_cli(monkeypatch):
    stats, calls = _drive(monkeypatch, 0.4, dry_run=True)
    assert stats['fractional_stop'] == 1 and not calls['cli']


def test_fractional_rejected_not_counted(monkeypatch):
    stats, calls = _drive(monkeypatch, 0.4, cli_ok=False)
    assert stats.get('fractional_stop', 0) == 0 and stats['rejected'] == 1


# ── 1e-6 tolerance (was 0.01): tiny fractional positions must not read covered
def test_tiny_fractional_position_gets_day_stop(monkeypatch):
    stats, calls = _drive(monkeypatch, 0.008)
    assert calls['oco'] == 0 and len(calls['cli']) == 1
    a = calls['cli'][0]
    assert a[a.index('--qty') + 1] == '0.008'
    assert a[a.index('--time-in-force') + 1] == 'day'
    assert stats['fractional_stop'] == 1 and stats['already_tp'] == 0


def test_naked_audit_reports_tiny_gap(monkeypatch):
    monkeypatch.setattr(sr, 'fetch_active_stops', lambda: {})
    monkeypatch.setattr(sr, 'fetch_tp_covered', lambda linked_only=False: {})
    msgs = []
    monkeypatch.setattr(sr, 'post_discord', lambda *a, **k: msgs.append(a), raising=False)
    out = sr.audit_naked_positions([{'symbol': 'F', 'qty': '0.008', 'side': 'long'}], [], True)
    assert out == ['F (0.008/0.008 sh uncovered)']


def test_floor_pass_need_tolerance_is_qty_eps():
    assert sr.QTY_EPS == 1e-6
