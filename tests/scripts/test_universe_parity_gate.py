"""scripts/universe_parity_gate.py — G1/G2 logic with fake rows (no DB).
Run: PYTHONPATH=src python3 -m pytest tests/scripts/test_universe_parity_gate.py -q"""
from __future__ import annotations
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_S = importlib.util.spec_from_file_location('universe_parity_gate', ROOT / 'scripts' / 'universe_parity_gate.py')
g = importlib.util.module_from_spec(_S)
sys.modules['universe_parity_gate'] = g
_S.loader.exec_module(g)

SINCE = datetime(2026, 10, 10, tzinfo=timezone.utc)
POST = SINCE + timedelta(days=2)
PRE = SINCE - timedelta(days=3)


def manifest(n=25, ref_for=lambda i: True, state='live'):
    return {f'S{i}': {'state': state, 'metadata': ({'universe_filter_ref': 'm:tier_liquid'} if ref_for(i) else {})}
            for i in range(n)}


def row(sid, src, sharpe, run_at, prim=True, wk='full_history', tier=None):
    return (sid, src, tier, sharpe, run_at, wk, prim)


def fleet(n=25, pre=1.0, post=1.0, src='filter_ref'):
    rows = []
    for i in range(n):
        rows.append(row(f'S{i}', None, pre, PRE, prim=False))
        rows.append(row(f'S{i}', src, post, POST))
    return rows


def test_all_ok():
    ok, out = g.evaluate(fleet(), manifest(), SINCE)
    assert ok, out
    assert any('lagging=0' in l for l in out)


def test_g1_lagging_rows_without_key_or_stale():
    rows = [r for r in fleet() if r[0] not in ('S0', 'S1', 'S2', 'S3')]
    rows += [row('S0', None, 1.0, POST), row('S1', 'filter_ref', 1.0, PRE),   # no key; predates epoch
             row('S2', 'none', 1.0, POST), row('S3', 'explicit', 1.0, POST)]  # failed open; explicit
    ok, out = g.evaluate(rows, manifest(), SINCE, max_lagging=3)
    assert not ok
    txt = '\n'.join(out)
    assert 'lagging=4' in txt and 'ref failed open' in txt and 'lacks universe_bound_source' in txt and 'stale' in txt


def test_none_ok_when_no_ref_listed_separately():
    m = manifest(ref_for=lambda i: i != 5)
    rows = fleet()
    rows = [r for r in rows if not (r[0] == 'S5' and r[1] == 'filter_ref')] + [row('S5', 'none', 1.0, POST)]
    ok, out = g.evaluate(rows, m, SINCE)
    assert ok
    txt = '\n'.join(out)
    assert 'static_by_design=1' in txt and 'S5' in txt


def test_cap_strategy_with_stale_row_is_lagging():
    # a capped strategy that was not re-run on the new artifact is NOT uniform
    rows = [r for r in fleet() if r[0] != 'S7'] + [row('S7', 'cap', 1.0, PRE)]
    ok, out = g.evaluate(rows, manifest(), SINCE, max_lagging=0)
    assert not ok and any('S7' in l for l in out)


def test_g2_median_delta_fails():
    ok, out = g.evaluate(fleet(pre=1.0, post=0.7), manifest(), SINCE)
    assert not ok
    assert any('G2' in l and 'NOT_YET' in l and 'median_dSharpe=-0.300' in l for l in out)


def test_g2_positive_fraction_fails():
    rows = fleet(pre=0.5, post=0.5)
    # 5 of 25 flip positive -> non-positive: median stays 0 but positive count 20 < 0.9*25
    rows = [r for r in rows if not (r[0] in {f'S{i}' for i in range(5)} and r[1] == 'filter_ref')]
    rows += [row(f'S{i}', 'filter_ref', -0.01, POST) for i in range(5)]
    ok, out = g.evaluate(rows, manifest(), SINCE)
    assert not ok and any('positive after=20 before=25' in l for l in out)


def test_g2_pre_row_is_latest_before_since_even_non_primary_and_same_window():
    rows = fleet()
    # an older, worse pre row and a different-window row must not be picked
    rows += [row('S0', None, -5.0, PRE - timedelta(days=30), prim=False),
             row('S0', None, 9.0, PRE + timedelta(days=1), prim=False, wk='dense_modern')]
    ok, out = g.evaluate(rows, manifest(), SINCE)
    assert ok, out


def test_g2_needs_min_pairs():
    ok, out = g.evaluate(fleet(n=10), manifest(n=10), SINCE)
    assert not ok and any('need >= 20' in l for l in out)


def test_non_live_ignored():
    m = manifest(); m['X'] = {'state': 'candidate', 'metadata': {}}
    ok, _ = g.evaluate(fleet(), m, SINCE)
    assert ok


def test_epoch_start_from_tag_file(tmp_path):
    (tmp_path / '.refresh_backtests.done.pre-universe-parity-20261008').write_text('x')
    (tmp_path / '.refresh_backtests.done.pre-universe-parity-20261010').write_text('x')
    assert g.epoch_start(None, str(tmp_path)) == SINCE
    assert g.epoch_start('2026-10-11T06:30', str(tmp_path)) == datetime(2026, 10, 11, 6, 30, tzinfo=timezone.utc)
    assert g.epoch_start(None, str(tmp_path / 'none')) is None


def test_main_not_yet_without_checkpoint(tmp_path, capsys):
    assert g.main(['--data-dir', str(tmp_path)]) == 0
    assert capsys.readouterr().out.startswith('NOT_YET')
