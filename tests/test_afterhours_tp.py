"""W3: extended-hours take-profit placement (limit/day/extended_hours)."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from execution import afterhours_tp as ah


def test_desired_tps_long_and_short():
    positions = [
        {'symbol': 'WDC', 'side': 'long', 'qty': '46'},
        {'symbol': 'MU', 'side': 'short', 'qty': '12'},
        {'symbol': 'NOTP', 'side': 'long', 'qty': '5'},
    ]
    def lookup(sym, side):
        return {'WDC': {'target': 717.03}, 'MU': {'target': 880.0}}.get(sym)
    out = {d['ticker']: d for d in ah.desired_tps(positions, lookup)}
    assert out['WDC']['side'] == 'sell' and abs(out['WDC']['tp'] - 717.03) < 1e-6
    assert out['MU']['side'] == 'buy' and abs(out['MU']['tp'] - 880.0) < 1e-6
    assert 'NOTP' not in out            # no known TP → no order


def test_already_covered_qty_is_skipped():
    positions = [{'symbol': 'WDC', 'side': 'long', 'qty': '46'}]
    lookup = lambda s, side: {'target': 717.03}
    out = ah.desired_tps(positions, lookup, tp_covered={'WDC': 46})
    assert out == []                    # already has a resting limit for full qty


def test_place_submits_extended_hours_limit(monkeypatch):
    monkeypatch.setenv('OPENCLAW_AFTERHOURS_TP', '1')
    calls = []
    def _fake_submit(**kw):
        calls.append(kw)
        return True, {'id': 'oid'}, None
    monkeypatch.setattr(ah, '_submit_limit', _fake_submit)
    plan = [{'ticker': 'WDC', 'side': 'sell', 'qty': 46, 'tp': 717.03}]
    n = ah._place_plan(plan, dry_run=False)
    assert n == 1
    kw = calls[0]
    assert kw['order_type'] == 'limit' and kw['extended_hours'] is True
    assert kw['tif'] == 'day' and kw['order_class'] == 'simple'
    assert abs(kw['limit_price'] - 717.03) < 1e-6


def test_gate_off_places_nothing(monkeypatch):
    monkeypatch.delenv('OPENCLAW_AFTERHOURS_TP', raising=False)
    assert ah._place_plan([{'ticker': 'WDC', 'side': 'sell', 'qty': 46,
                            'tp': 717.03}], dry_run=False) == 0
