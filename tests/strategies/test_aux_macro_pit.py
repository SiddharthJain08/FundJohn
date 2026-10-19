"""D1b: macro release lags, available_at builders, OPENCLAW_MACRO_PIT cut."""
from __future__ import annotations

import ast
from pathlib import Path

import pandas as pd
import pytest

from strategies import aux_data_loader as adl

ROOT = Path(adl.__file__).resolve().parent.parent


def _write_macro(tmp_path, rows):
    p = tmp_path / 'macro.parquet'
    pd.DataFrame(rows, columns=['date', 'series', 'value']).to_parquet(p, index=False)
    return p


@pytest.fixture()
def macro(monkeypatch, tmp_path):
    def _make(rows, flag):
        monkeypatch.setattr(adl, 'MACRO_PATH', _write_macro(tmp_path, rows))
        monkeypatch.setattr(adl, '_MACRO_SERIES', None)
        monkeypatch.setattr(adl, '_MACRO_PIT_ANNOUNCED', False)
        monkeypatch.setattr(adl, '_AVAIL_FRAMES', {})
        if flag is None:
            monkeypatch.delenv('OPENCLAW_MACRO_PIT', raising=False)
        else:
            monkeypatch.setenv('OPENCLAW_MACRO_PIT', flag)
    return _make


def test_flag_unset_is_legacy_observation_cut(macro):
    macro([('2026-09-01', 'INDPRO', 100.0)], None)
    assert 'INDPRO' in adl._macro_slice('2026-09-05')


def test_monthly_boundary_indpro(macro):
    macro([('2026-09-01', 'INDPRO', 100.0)], '1')
    assert 'INDPRO' not in adl._macro_slice('2026-10-18')
    out = adl._macro_slice('2026-10-19')
    assert out['INDPRO'].index.max() == pd.Timestamp('2026-09-01')


@pytest.mark.parametrize('name', ['DEXUSEU', 'DTWEXBGS', 'DCOILWTICO'])
def test_weekly_release_boundary(macro, name):
    assert adl.macro_release_lag(name) == 7
    macro([('2026-10-09', name, 1.0)], '1')
    assert name not in adl._macro_slice('2026-10-15')
    assert name in adl._macro_slice('2026-10-16')


def test_lag_table_ruling_values():
    L = adl.MACRO_RELEASE_LAG_DAYS
    assert L['INDPRO'] == 48
    for k in ('DEXUSEU', 'DEXJPUS', 'DTWEXBGS', 'DCOILWTICO'):
        assert L[k] == 7
    assert L['DGS10'] == 1 and L['NYFED_SOFR'] == 1 and L['VIX'] == 0
    assert adl.MACRO_DEFAULT_LAG_DAYS == 0


def test_daily_boundary_dgs10(macro):
    macro([('2026-10-12', 'DGS10', 4.0)], '1')
    assert 'DGS10' not in adl._macro_slice('2026-10-12')
    assert 'DGS10' in adl._macro_slice('2026-10-13')


def test_vix_identical_both_states(macro):
    rows = [('2026-10-01', 'VIX', 15.0), ('2026-10-02', 'VIX', 16.0)]
    macro(rows, None)
    a = adl._macro_slice('2026-10-02')['VIX']
    macro(rows, '1')
    b = adl._macro_slice('2026-10-02')['VIX']
    pd.testing.assert_series_equal(a, b)


def test_unmapped_series_lag0_and_warned_once(macro, capsys):
    macro([('2026-10-12', 'ZZMACRO', 1.0)], '1')
    assert 'ZZMACRO' in adl._macro_slice('2026-10-12')
    adl._macro_slice('2026-10-13')
    err = capsys.readouterr().err
    assert err.count('[macro_pit] WARN unmapped=ZZMACRO') == 1
    assert err.count('[macro_pit]') == 1


def test_announce_line_when_all_mapped(macro, capsys):
    macro([('2026-10-12', 'DGS10', 4.0)], '1')
    adl._macro_slice('2026-10-13')
    adl._macro_slice('2026-10-14')
    err = capsys.readouterr().err
    assert err.count('[macro_pit] on series=1 mapped=1 unmapped=-') == 1


def test_flag_unset_prints_nothing(macro, capsys):
    macro([('2026-10-12', 'ZZMACRO', 1.0)], None)
    adl._macro_slice('2026-10-12')
    assert capsys.readouterr().err == ''


def _tuple_strings(path, name):
    tree = ast.parse(Path(path).read_text())
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for t in targets:
            if isinstance(t, ast.Name) and t.id == name:
                return ast.literal_eval(node.value)
    raise AssertionError(f'{name} not found in {path}')


def test_every_writer_series_has_an_explicit_lag():
    ing = ROOT / 'ingestion'
    fred = _tuple_strings(ing / 'ingest_macro_rates.py', 'FRED_SERIES')
    nyfed = _tuple_strings(ing / 'ingest_macro_rates.py', 'NYFED_TYPES')
    vols = [b for _, b in _tuple_strings(ing / 'cboe_vol_indices.py', 'VOL_INDICES')]
    names = list(fred) + ['NYFED_' + t for t in nyfed] + vols
    assert names
    missing = [n for n in names if n not in adl.MACRO_RELEASE_LAG_DAYS]
    assert not missing, missing


def test_available_at_frame_macro_matches_cut(macro):
    rows = [('2026-08-01', 'INDPRO', 1.0), ('2026-09-01', 'INDPRO', 2.0),
            ('2026-10-08', 'DEXUSEU', 1.1), ('2026-10-12', 'DGS10', 4.0),
            ('2026-10-13', 'DGS10', 4.1), ('2026-10-14', 'VIX', 15.0)]
    macro(rows, '1')
    frame = adl.available_at_frame('macro')
    assert set(frame.columns) == {'date', 'series', 'value', 'available_at'}
    for d in ('2026-10-13', '2026-10-15', '2026-10-19'):
        ts = pd.Timestamp(d)
        sl = adl._macro_slice(d)
        vis = frame[frame.available_at <= ts]
        assert set(sl) == set(vis.series.unique())
        for name, grp in vis.groupby('series'):
            exp = grp.set_index('date')['value'].sort_index()
            pd.testing.assert_series_equal(sl[name], exp, check_names=False, check_freq=False)


def test_available_at_frame_insider_is_filing_date(monkeypatch, tmp_path):
    p = tmp_path / 'insider.parquet'
    pd.DataFrame({'ticker': ['AAA'], 'date': ['2026-10-01'],
                  'transaction_type': ['P'], 'insider_name': ['x'],
                  'net_value': [1.0], 'shares': [1.0]}).to_parquet(p, index=False)
    monkeypatch.setattr(adl, 'INSIDER_PATH', p)
    monkeypatch.setattr(adl, '_AVAIL_FRAMES', {})
    monkeypatch.setattr(adl, '_INSIDER_DATE_INDEX', None)
    monkeypatch.setattr(adl, '_INSIDER_BY_DATE', None)
    adl._insider_slice.cache_clear()
    f = adl.available_at_frame('insider')
    assert (f['available_at'] == pd.to_datetime(f['date'])).all() and len(f) == 1
    sl = adl._insider_slice('2026-10-05')
    assert 'available_at' not in sl['AAA'][0]
    adl._insider_slice.cache_clear()


def test_available_at_frame_options_is_build_date(monkeypatch, tmp_path):
    p = tmp_path / 'agg.parquet'
    pd.DataFrame({'ticker': ['AAA'], 'date': ['2026-10-01'], 'iv30': [0.3]}
                 ).to_parquet(p, index=False)
    monkeypatch.setattr(adl, 'AGG_PATH', p)
    monkeypatch.setattr(adl, '_AGG_DF', None)
    monkeypatch.setattr(adl, '_AVAIL_FRAMES', {})
    adl._day_slice.cache_clear()
    f = adl.available_at_frame('options')
    assert (f['available_at'] == pd.to_datetime(f['date'])).all()
    assert 'available_at' not in adl._day_slice('2026-10-01')['AAA']
    adl._day_slice.cache_clear()


def test_available_at_frame_unknown_kind_raises():
    with pytest.raises(ValueError):
        adl.available_at_frame('bogus')
