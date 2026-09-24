"""Regression: the atr_r flip gate must pair each live strategy's PRIMARY atr_r
row with its latest flat row even though the fleet refresh has demoted that
flat row to primary_window=false.

2026-09-17: 86/86 live strategies were on atr_r yet G2 reported "only 0 live
strategies have both an atr_r and a flat row" every night since 09-11 — the
query filtered `primary_window = true`, which no flat baseline satisfies once
its successor lands. The flip could never fire.

Run: python3 -m pytest tests/scripts/test_target_mode_flip_gate.py -q
"""
import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    'target_mode_flip_gate', ROOT / 'scripts' / 'target_mode_flip_gate.py')
gate = importlib.util.module_from_spec(_SPEC)
sys.modules['target_mode_flip_gate'] = gate
_SPEC.loader.exec_module(gate)


class _Cur:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        assert 'primary_window = true' not in sql, 'flat baselines are non-primary after demotion'

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _Cur(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _rows(n, atr_sharpe=1.0, flat_sharpe=1.0, window='full_history'):
    t_atr = datetime(2026, 9, 15, 3, 0)
    t_flat = t_atr - timedelta(days=5)
    out = []
    for i in range(n):
        sid = f'S_{i}'
        out.append((sid, 'atr_r', atr_sharpe, t_atr, window, True))
        # demoted baseline: same window_kind, primary_window=False, no target_mode key -> 'flat'
        out.append((sid, 'flat', flat_sharpe, t_flat, window, False))
    return out


def _run(monkeypatch, tmp_path, rows, capsys, n_live):
    man = {'strategies': {f'S_{i}': {'state': 'live'} for i in range(n_live)}}
    mp = tmp_path / 'manifest.json'
    mp.write_text(json.dumps(man))
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://fake')
    monkeypatch.setattr(gate.psycopg2, 'connect', lambda uri: _Conn(rows))
    monkeypatch.setattr(sys, 'argv', ['gate', '--manifest', str(mp)])
    rc = gate.main()
    out = capsys.readouterr().out
    return rc, out


def test_demoted_flat_baselines_still_pair_and_the_gate_can_pass(monkeypatch, tmp_path, capsys):
    rc, out = _run(monkeypatch, tmp_path, _rows(25), capsys, 25)
    assert rc == 0
    assert out.splitlines()[0] == 'OK', out
    assert 'n=25' in out


def test_fleet_lagging_when_the_primary_row_is_still_flat(monkeypatch, tmp_path, capsys):
    rows = _rows(25)
    # one strategy whose primary row is still flat (no atr_r run yet)
    rows.append(('S_25', 'flat', 1.0, datetime(2026, 9, 16), 'full_history', True))
    rc, out = _run(monkeypatch, tmp_path, rows, capsys, 26)
    assert 'lagging=1' in out and 'S_25' in out


def test_a_worse_geometry_holds_the_flip(monkeypatch, tmp_path, capsys):
    rc, out = _run(monkeypatch, tmp_path, _rows(25, atr_sharpe=0.5, flat_sharpe=1.0), capsys, 25)
    assert out.splitlines()[0] == 'NOT_YET'
    assert 'median_dSharpe=-0.500' in out


def test_a_flat_row_from_another_window_is_not_a_baseline(monkeypatch, tmp_path, capsys):
    rows = []
    for i in range(25):
        rows.append((f'S_{i}', 'atr_r', 1.0, datetime(2026, 9, 15), 'full_history', True))
        rows.append((f'S_{i}', 'flat', 1.0, datetime(2026, 9, 10), 'oos_2024', False))
    rc, out = _run(monkeypatch, tmp_path, rows, capsys, 25)
    assert 'only 0 live strategies have both' in out
