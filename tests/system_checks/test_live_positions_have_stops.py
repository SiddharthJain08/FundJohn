"""live_positions_have_stops must read the book NESTED — a flat order list
hides OCO/bracket stop legs, so a fully OCO-protected book reads as 100%
naked (2026-07-21: daily maintenance false-alarmed "12/13 naked" on a book
where 11/12 positions carried full OCOs; same trap as the 07-16 dashboard
"3 stops vs 9" incident)."""
from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from system_checks.checks import broker  # noqa: E402
from system_checks.types import Status  # noqa: E402


def _fake_run(positions, orders):
    def run(args, capture_output=True, text=True, timeout=15):
        payload = positions if 'position' in args else orders
        assert 'order' not in args or '--nested' in args, \
            'order list must be nested — flat reads hide OCO stop legs'
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(payload),
                                     stderr='')
    return run


_POSITIONS = [
    {'symbol': 'ODD', 'qty': '-170', 'asset_class': 'us_equity'},
    {'symbol': 'SNDK', 'qty': '1', 'asset_class': 'us_equity'},
]


def test_oco_protected_book_passes(monkeypatch):
    """OCO parent (limit) with a held stop leg + a bare top-level stop → both
    positions covered. The old flat read saw only the bare stop."""
    orders = [
        {'symbol': 'ODD', 'type': 'limit', 'order_class': 'oco', 'qty': '170',
         'status': 'new', 'legs': [
             {'symbol': 'ODD', 'type': 'stop', 'stop_price': '17.19',
              'qty': '170', 'status': 'held'}]},
        {'symbol': 'SNDK', 'type': 'stop', 'stop_price': '1367.84', 'qty': '1',
         'status': 'new'},
    ]
    monkeypatch.setattr(subprocess, 'run', _fake_run(_POSITIONS, orders))
    status, msg = broker._live_positions_have_stops()
    assert status is Status.PASS, msg


def test_truly_naked_position_still_fails(monkeypatch):
    orders = [
        {'symbol': 'ODD', 'type': 'limit', 'order_class': 'oco', 'qty': '170',
         'status': 'new', 'legs': [
             {'symbol': 'ODD', 'type': 'stop', 'stop_price': '17.19',
              'qty': '170', 'status': 'held'}]},
    ]
    monkeypatch.setattr(subprocess, 'run', _fake_run(_POSITIONS, orders))
    status, msg = broker._live_positions_have_stops()
    assert status is Status.FAIL and 'SNDK' in msg


def test_dead_stop_legs_do_not_count(monkeypatch):
    """A canceled stop leg (OCO whose TP side is about to fill) is not
    protection."""
    orders = [
        {'symbol': 'ODD', 'type': 'limit', 'order_class': 'oco', 'qty': '170',
         'status': 'new', 'legs': [
             {'symbol': 'ODD', 'type': 'stop', 'stop_price': '17.19',
              'qty': '170', 'status': 'canceled'}]},
        {'symbol': 'SNDK', 'type': 'stop', 'stop_price': '1367.84', 'qty': '1',
         'status': 'new'},
    ]
    monkeypatch.setattr(subprocess, 'run', _fake_run(_POSITIONS, orders))
    status, msg = broker._live_positions_have_stops()
    assert status is Status.FAIL and 'ODD' in msg


def test_leg_without_symbol_inherits_parent(monkeypatch):
    orders = [
        {'symbol': 'ODD', 'type': 'limit', 'order_class': 'oco', 'qty': '170',
         'status': 'new', 'legs': [
             {'type': 'stop', 'stop_price': '17.19', 'status': 'held'}]},
        {'symbol': 'SNDK', 'type': 'stop', 'stop_price': '1367.84', 'qty': '1',
         'status': 'new'},
    ]
    monkeypatch.setattr(subprocess, 'run', _fake_run(_POSITIONS, orders))
    status, msg = broker._live_positions_have_stops()
    assert status is Status.PASS, msg


def test_order_list_is_capped_at_500_and_keyset_paginated(monkeypatch):
    """`order list` returns 50 rows by DEFAULT and caps --limit at 500. The
    check must ask for 500 and page with --after-order-id until a short page,
    otherwise a 200+-order breadth book reads as mostly naked (08-12 incident)."""
    page1 = [{'symbol': f'T{i}', 'type': 'limit', 'qty': '1', 'status': 'new', 'id': f'o{i}'}
             for i in range(500)]
    page2 = [{'symbol': 'ODD', 'type': 'stop', 'stop_price': '17.19', 'qty': '170',
              'status': 'new', 'id': 'o500'},
             {'symbol': 'SNDK', 'type': 'stop', 'stop_price': '1367.84', 'qty': '1',
              'status': 'new', 'id': 'o501'}]
    seen = []
    def run(args, capture_output=True, text=True, timeout=15):
        if 'position' in args:
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(_POSITIONS), stderr='')
        seen.append(list(args))
        page = page2 if '--after-order-id' in args else page1
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(page), stderr='')
    monkeypatch.setattr(subprocess, 'run', run)
    status, msg = broker._live_positions_have_stops()
    assert status is Status.PASS, msg          # stops live on page 2
    assert len(seen) == 2
    assert seen[0][seen[0].index('--limit') + 1] == '500'
    assert '--nested' in seen[0] and seen[0][seen[0].index('--direction') + 1] == 'asc'
    assert seen[1][seen[1].index('--after-order-id') + 1] == 'o499'
