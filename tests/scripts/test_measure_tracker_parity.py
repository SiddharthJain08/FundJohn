# tests/scripts/test_measure_tracker_parity.py
"""Pure-function + seam tests for the two-checkout tracker parity tool. No DB, no masters."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _mod():
    spec = importlib.util.spec_from_file_location(
        'mtp', ROOT / 'scripts' / 'measure_tracker_parity.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _run(trades=None, cfg='{"a": 1}', row='["x"]', sid='S_a'):
    return {'strategy_id': sid, 'trade_rows': trades if trades is not None else [[1], [2]],
            'config_json': cfg, 'run_row': row}


def test_replay_kwargs_maps_provenance_keys():
    m = _mod()
    cfg = {'start_date': '2020-01-01', 'end_date': '2021-01-01', 'max_hold_days': 21,
           'fill_model': 'same_close', 'gap_fill': 'open', 'financials_pit': True,
           'macro_pit': True, 'target_mode': 'atr_r', 'double_touch': 'stop',
           'event_gate': 'on', 'intraday_exit': 'off', 'asset_gate': 'g1',
           'rf': {'source': 'dgs3mo'}}
    kw, env = m.replay_kwargs(cfg)
    assert kw == {'start_date': '2020-01-01', 'end_date': '2021-01-01',
                  'max_hold_days': 21, 'fill_model': 'same_close'}
    assert env == {'OPENCLAW_BT_GAP_FILL': 'open', 'OPENCLAW_FINANCIALS_PIT': '1',
                   'OPENCLAW_MACRO_PIT': '1', 'OPENCLAW_TARGET_MODE': 'atr_r',
                   'OPENCLAW_BT_DOUBLE_TOUCH': 'stop', 'OPENCLAW_BT_EVENT_GATE': '1',
                   'OPENCLAW_BT_INTRADAY_EXIT': '0', 'OPENCLAW_BT_ASSET_GATE': 'g1',
                   'OPENCLAW_RF_SOURCE': 'dgs3mo'}
    _, env2 = m.replay_kwargs({'financials_pit': False, 'event_gate': 'off',
                               'intraday_exit': 'on'})
    assert env2 == {'OPENCLAW_FINANCIALS_PIT': '0', 'OPENCLAW_MACRO_PIT': '0',
                    'OPENCLAW_BT_EVENT_GATE': '0', 'OPENCLAW_BT_INTRADAY_EXIT': '1'}


def test_compare_identical_ok(capsys):
    m = _mod()
    ok, lines = m.compare({'runs': {'r1': _run()}}, {'runs': {'r1': _run()}})
    assert ok
    assert lines == ['[tracker_parity] run=r1 sid=S_a trades=IDENTICAL '
                     'config_json=IDENTICAL run_row=IDENTICAL']
    assert lines[0] in capsys.readouterr().out


def test_compare_reports_first_diff_index_and_config_keys():
    m = _mod()
    a = _run(trades=[[1], [2], [3]], cfg='{"a": 1, "b": 2, "c": 3}')
    b = _run(trades=[[1], [9]], cfg='{"a": 1, "b": 5, "d": 3}', row='["y"]')
    ok, lines = m.compare({'runs': {'r1': a}}, {'runs': {'r1': b}})
    assert not ok
    assert lines[0] == ('[tracker_parity] run=r1 sid=S_a trades=DIFF(n=3/2 first=1) '
                        'config_json=DIFF(keys=b,c,d) run_row=DIFF')


def test_compare_missing_run_is_diff():
    m = _mod()
    ok, lines = m.compare({'runs': {'r1': _run(), 'r2': _run()}}, {'runs': {'r1': _run()}})
    assert not ok
    assert 'run=r2' in lines[1] and 'DIFF' in lines[1]


def test_capture_one_strips_run_id_and_code_sha():
    m = _mod()
    cfg = json.dumps({'max_hold_days': 5})

    def fake(sid, conn, commit, instrument_class, **kw):
        assert commit is False and instrument_class == 'equity' and kw == {'fill_model': 'f'}
        cur = conn.cursor()
        cur.execute('INSERT INTO strategy_backtest_runs …',
                    ['RUNID', sid, 'SHA', 0.5, cfg])
        ub.psycopg2.extras.execute_values(
            cur, 'INSERT INTO strategy_backtest_trades …', [['RUNID', 'AAA', 1.0]])
    ub = SimpleNamespace(psycopg2=SimpleNamespace(extras=SimpleNamespace(execute_values=None)),
                         run_backtest=fake, _resolve_instrument_class=lambda s: 'equity')
    out = m.capture_one(ub, 'S_a', {'fill_model': 'f'}, {'OPENCLAW_X': '1'})
    assert out['trade_rows'] == [['AAA', 1.0]]
    assert out['config_json'] == cfg
    assert json.loads(out['run_row']) == ['S_a', 0.5, cfg]
    assert ub.psycopg2.extras.execute_values is None  # patch restored


def test_rows_json_seam_never_touches_db(tmp_path, monkeypatch):
    m = _mod()
    import psycopg2

    def boom(*a, **k):
        raise AssertionError('DB touched')
    monkeypatch.setattr(psycopg2, 'connect', boom)
    p = tmp_path / 'rows.json'
    p.write_text(json.dumps([['r1', 'S_a', {'gap_fill': 'open'}],
                             ['r2', 'S_b', '{"x": 1}']]))
    assert m.load_rows(rows_json=str(p)) == [('r1', 'S_a', {'gap_fill': 'open'}),
                                             ('r2', 'S_b', {'x': 1})]
