"""scripts/universe_parity_report.py — report assembly with fake rows (no DB).
Run: PYTHONPATH=src python3 -m pytest tests/scripts/test_universe_parity_report.py -q"""
from __future__ import annotations
import csv
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
_S = importlib.util.spec_from_file_location('universe_parity_report', ROOT / 'scripts' / 'universe_parity_report.py')
rep = importlib.util.module_from_spec(_S)
sys.modules['universe_parity_report'] = rep
_S.loader.exec_module(rep)

SINCE = datetime(2026, 10, 10, tzinfo=timezone.utc)
BENCH = {'LOW_VOL': 0.5, 'TRANSITIONING': 0.4, 'HIGH_VOL': 0.4, 'CRISIS': 1.0}


def run(rid, at, sharpe, src=None, usz=3000.0, wk='full_history', tier=None, trades=500):
    return {'run_id': rid, 'run_at': at, 'window_kind': wk, 'primary_window': src is not None,
            'total_sharpe': sharpe, 'total_trades': trades, 'universe_size': usz,
            'bound_source': src, 'filter_ref_tier': tier, 'bound_tier': tier}


def sleeves(lv, tr=0.2):
    # trade_count >= 100, dd under the class ceiling
    return [{'regime_state': 'LOW_VOL', 'sharpe': lv, 'trade_count': 300, 'max_dd_pct': 5.0, 'calmar': 2.0},
            {'regime_state': 'TRANSITIONING', 'sharpe': tr, 'trade_count': 300, 'max_dd_pct': 5.0, 'calmar': 2.0}]


MAN = {'A': {'metadata': {'universe_filter_ref': 'm:tier_liquid'}},
       'B': {'metadata': {'universe_filter_ref': 'm:stocks_liquid'}},
       'C': {'metadata': {}}, 'D': {'metadata': {'universe_filter_ref': 'm:sp500'}}}
PRE, POST = SINCE - timedelta(days=2), SINCE + timedelta(days=1)


def build():
    runs = {
        'A': [run('a0', PRE, 1.0), run('a1', POST, 0.4, 'filter_ref', 1200.0, tier='tier_liquid')],
        'B': [run('b0', PRE, 0.2), run('b1', POST, 0.9, 'filter_ref', 900.0, tier='stocks_liquid')],
        'C': [run('c0', PRE, 0.1), run('c1', POST, 0.1, 'none', 3000.0)],
        'D': [run('d0', PRE, 0.5), run('d1', POST, 0.5, 'none')],          # ref present, failed open
        'E': [run('e0', PRE, 0.5)],                                         # no post yet
    }
    regs = {'a0': sleeves(1.0), 'a1': sleeves(0.3), 'b0': sleeves(0.2), 'b1': sleeves(0.9),
            'c0': sleeves(0.1), 'c1': sleeves(0.1), 'd0': sleeves(0.6), 'd1': sleeves(0.6), 'e0': sleeves(0.6)}
    return runs, regs


def assemble():
    runs, regs = build()
    man = dict(MAN, E={'metadata': {}})
    return rep.assemble(['A', 'B', 'C', 'D', 'E'], man, runs, regs, SINCE, bench=BENCH,
                        current_by_sid={'A': {'LOW_VOL': True}})


def test_sorted_by_abs_delta_none_last():
    rows = assemble()
    assert [r['strategy_id'] for r in rows][:2] == ['B', 'A']          # |+0.7| then |-0.6|
    assert rows[-1]['strategy_id'] == 'E' and rows[-1]['delta_sharpe'] is None


def test_columns_and_activation_under_bench_rule():
    rows = {r['strategy_id']: r for r in assemble()}
    a = rows['A']
    assert a['pre_universe_size'] == 3000.0 and a['post_universe_size'] == 1200.0
    assert a['post_bound_source'] == 'filter_ref' and a['post_tier'] == 'tier_liquid'
    assert a['pre_bound_source'] == 'static(pre-epoch)'
    assert a['pre_eligible'] == 'LOW_VOL' and a['post_eligible'] == ''      # 1.0>=0.5 ; 0.3<0.5
    assert 'DEACTIVATED in LOW_VOL' in a['implication']
    assert 'live eligibility' in a['implication'] or a['current_eligible'] == 'LOW_VOL'
    b = rows['B']
    assert b['pre_eligible'] == '' and b['post_eligible'] == 'LOW_VOL'
    assert 'ACTIVATED in LOW_VOL' in b['implication']
    assert b['post_sharpe_LOW_VOL'] == 0.9 and b['pre_sharpe_LOW_VOL'] == 0.2


def test_failed_open_and_no_post_text():
    rows = {r['strategy_id']: r for r in assemble()}
    assert 'failed OPEN' in rows['D']['implication']
    assert 'no post-epoch run' in rows['E']['implication']
    assert 'ineligible in every regime before and after' == rows['C']['implication']
    assert 'DORMANT' in rows['A']['implication']


def test_pick_pair_same_window_kind_and_selection_ids():
    runs, _ = build()
    runs['A'].append(run('a_dm', PRE + timedelta(hours=1), 9.0, wk='dense_modern'))
    pre, post = rep.pick_pair(runs['A'], SINCE)
    assert pre['run_id'] == 'a0' and post['run_id'] == 'a1'
    assert rep.assemble_selection(['A', 'E'], runs, SINCE) == {'a0', 'a1', 'e0'}


def test_outputs_csv_md_and_no_overwrite(tmp_path):
    rows = assemble()
    base = str(tmp_path / 'rep')
    rep.write_outputs(rows, base, SINCE, BENCH, 0.0)
    got = list(csv.DictReader(open(base + '.csv')))
    assert [r['strategy_id'] for r in got][:2] == ['B', 'A']
    md = open(base + '.md').read()
    assert '| B |' in md and 'bench vector' in md and 'LOW_VOL=0.500' in md
    with pytest.raises(SystemExit):
        rep.write_outputs(rows, base, SINCE, BENCH, 0.0)


def test_queries_never_touch_trades_table():
    src = open(ROOT / 'scripts' / 'universe_parity_report.py').read()
    code = src.split('"""', 2)[2]          # drop the docstring that names the table
    assert 'strategy_backtest_trades' not in code
    assert 'run_id = ANY' in code and 'strategy_id = ANY' in code
