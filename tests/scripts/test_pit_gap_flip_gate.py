# tests/scripts/test_pit_gap_flip_gate.py
"""G1 uniformity verdict for the PIT+gap live flip. No DB: the gate's decision
logic is exercised through its pure `verdict` function."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _mod():
    spec = importlib.util.spec_from_file_location(
        'pgfg', ROOT / 'scripts' / 'pit_gap_flip_gate.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


LIVE = ['S_a', 'S_b', 'S_c']


def test_all_live_rows_on_the_new_config_is_ok():
    m = _mod()
    rows = [['S_a', 'open', True, '2026-09-20T01:00:00'],
            ['S_b', 'open', True, '2026-09-20T02:00:00'],
            ['S_c', 'open', True, '2026-09-20T03:00:00']]
    ok, detail = m.verdict(LIVE, rows, max_lagging=0)
    assert ok is True
    assert any('on_new=3' in line for line in detail)


def test_a_row_missing_gap_fill_lags():
    m = _mod()
    rows = [['S_a', 'level', True, '2026-09-20T01:00:00'],
            ['S_b', 'open', True, '2026-09-20T02:00:00'],
            ['S_c', 'open', True, '2026-09-20T03:00:00']]
    ok, detail = m.verdict(LIVE, rows, max_lagging=0)
    assert ok is False
    assert any('S_a' in line for line in detail)


def test_a_row_missing_financials_pit_lags():
    m = _mod()
    rows = [['S_a', 'open', False, '2026-09-20T01:00:00'],
            ['S_b', 'open', True, '2026-09-20T02:00:00'],
            ['S_c', 'open', True, '2026-09-20T03:00:00']]
    assert m.verdict(LIVE, rows, max_lagging=0)[0] is False


def test_a_live_strategy_with_no_row_at_all_lags():
    m = _mod()
    rows = [['S_a', 'open', True, '2026-09-20T01:00:00']]
    ok, detail = m.verdict(LIVE, rows, max_lagging=0)
    assert ok is False
    assert any('S_b' in line and 'S_c' in line for line in detail)


def test_max_lagging_tolerance_admits_a_stuck_strategy():
    m = _mod()
    rows = [['S_a', 'level', True, '2026-09-20T01:00:00'],
            ['S_b', 'open', True, '2026-09-20T02:00:00'],
            ['S_c', 'open', True, '2026-09-20T03:00:00']]
    assert m.verdict(LIVE, rows, max_lagging=1)[0] is True


def test_non_live_rows_are_ignored():
    m = _mod()
    rows = [['S_a', 'open', True, '2026-09-20T01:00:00'],
            ['S_b', 'open', True, '2026-09-20T02:00:00'],
            ['S_c', 'open', True, '2026-09-20T03:00:00'],
            ['S_candidate', 'level', False, '2026-09-20T04:00:00']]
    assert m.verdict(LIVE, rows, max_lagging=0)[0] is True
