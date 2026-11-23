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


def _drive(monkeypatch, qty):
    calls = {'oco': 0, 'cli': []}
    monkeypatch.setattr(sr, 'fetch_tp_covered', lambda linked_only=False: {})
    monkeypatch.setattr(sr, '_resolve_intended_bracket',
                        lambda conn, sym, side: {'stop': 10.0, 'target': 14.0})
    monkeypatch.setattr(sr, '_compute_new_stop', lambda pos, sub: (10.0, 'ok'))
    monkeypatch.setattr(sr, '_compute_new_target', lambda pos, sub: (14.0, 'ok'))
    monkeypatch.setattr(sr, 'cancel_stops_for', lambda *a, **k: 0)
    def oco(**k):
        calls['oco'] += 1
        return {'status': 'submitted'}
    monkeypatch.setattr(sr, 'submit_protective_oco', oco)
    def cli(args, timeout=15):
        calls['cli'].append(args)
        return True, {'id': 'x'}, None
    monkeypatch.setattr(sr, '_run_cli', cli)
    pos = [{'symbol': 'F', 'qty': str(qty), 'side': 'long'}]
    return sr.run_oco_reattach(None, pos, False), calls


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
