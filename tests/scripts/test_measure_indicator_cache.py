"""D2c -- scripts/measure_indicator_cache.py. Synthetic panels only; no Postgres."""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))

import measure_indicator_cache as mic  # noqa: E402

DAYS, TICKERS = '320', '12'


@pytest.fixture(autouse=True)
def _no_postgres():
    def boom(*a, **k):
        raise AssertionError('psycopg2.connect called in a synthetic test')
    import psycopg2
    with patch.object(psycopg2, 'connect', boom):
        yield


def _stub(tmp_path, name, thr=0.0):
    src = textwrap.dedent(f'''
        from typing import List
        from strategies.base import BaseStrategy, Signal


        class {name}(BaseStrategy):
            id = '{name.lower()}'
            name = '{name}'
            description = 'measure fixture'
            tier = 3
            min_lookback = 30
            active_in_regimes = ['LOW_VOL']

            def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
                out = []
                if len(prices) < 30:
                    return out
                sma = self.cached('sma', lambda p: p.rolling(20).mean(), prices)
                last = sma.iloc[-1]
                for t in list(prices.columns)[:6]:
                    px = float(prices[t].iloc[-1])
                    m = float(last[t])
                    if m == m and px > m * (1.0 + {thr}):
                        out.append(Signal(ticker=t, direction='LONG', entry_price=px,
                                          stop_loss=px * 0.97, target_1=px * 1.03,
                                          target_2=px * 1.06, target_3=px * 1.09,
                                          confidence='MED', position_size_pct=0.01,
                                          signal_params={{'m': round(m, 6)}}))
                return out
    ''')
    p = tmp_path / f'{name.lower()}.py'
    p.write_text(src)
    return str(p)


def test_make_panel_has_gaps_and_late_listings():
    close, bars, regimes = mic.make_panel(320, 12, 0)
    assert list(close.columns)[:2] == ['ZZTEST0000', 'ZZTEST0001']
    assert {'SPY', 'QQQ'} <= set(close.columns)
    assert close.shape[0] == 320 and str(close.index[-1].date()) == '2026-10-16'
    late = [c for c in close.columns if close[c].iloc[:5].isna().all()]
    assert late, 'no late listings'
    interior = 0
    for c in close.columns:
        s = close[c]
        first = s.first_valid_index()
        interior += int(s.loc[first:].isna().sum())
    assert interior > 0
    assert set(bars) >= set(close.columns)
    assert list(bars['SPY'].columns) == ['open', 'high', 'low', 'close']
    assert len(regimes) == 320
    again, _, _ = mic.make_panel(320, 12, 0)
    assert again.equals(close)


def test_run_once_identical_both_ways(tmp_path):
    import backtest.unified_backtest as ub
    cls = ub.load_strategy_class(_stub(tmp_path, 'ZzMeasA'))
    close, bars, regimes = mic.make_panel(320, 12, 0)
    regimes[:] = 'LOW_VOL'
    r0, s0, _ = mic.run_once(cls, close, bars, regimes, cache=False)
    r1, s1, rss = mic.run_once(cls, close, bars, regimes, cache=True)
    assert r0 and r0 == r1
    assert s0 > 0 and s1 > 0 and rss > 0
    assert len(r0[0]) == 11


def test_main_prints_one_line_rc0(tmp_path, capsys):
    f = _stub(tmp_path, 'ZzMeasB')
    rc = mic.main(['--strategy-file', f, '--days', DAYS, '--tickers', TICKERS])
    out = capsys.readouterr().out.strip().splitlines()
    assert rc == 0
    assert len(out) == 1 and out[0].startswith('[bt_cache_measure] strategy=')
    assert 'signals_identical=true' in out[0] and 'trades_identical=true' in out[0]
    assert 'orig=' not in out[0]
    for k in ('uncached_s=', 'cached_s=', 'speedup=', 'n_signals=', 'sha=', 'peak_rss_mb_process='):
        assert k in out[0]


def test_compare_to_detects_divergent_port(tmp_path, capsys, monkeypatch):
    f = _stub(tmp_path, 'ZzMeasC')
    g = _stub(tmp_path, 'ZzMeasD', thr=0.02)
    import backtest.unified_backtest as ub
    monkeypatch.setattr(ub, 'find_strategy_file',
                        lambda sid: g if sid == 'orig_sid' else None)
    rc = mic.main(['--strategy-file', f, '--compare-to', 'orig_sid',
                   '--days', DAYS, '--tickers', TICKERS])
    out = capsys.readouterr().out.strip()
    assert rc == 1
    assert 'orig=orig_sid orig_identical=false' in out
    assert 'signals_identical=true' in out


def test_compare_to_identical_rc0(tmp_path, capsys, monkeypatch):
    f = _stub(tmp_path, 'ZzMeasE')
    import backtest.unified_backtest as ub
    monkeypatch.setattr(ub, 'find_strategy_file', lambda sid: f)
    rc = mic.main(['--strategy-file', f, '--compare-to', 'same', '--days', DAYS,
                   '--tickers', TICKERS])
    assert rc == 0
    assert 'orig_identical=true' in capsys.readouterr().out


def test_never_reads_real_aux(tmp_path):
    import backtest.unified_backtest as ub
    import strategies.aux_data_loader as adl
    cls = ub.load_strategy_class(_stub(tmp_path, 'ZzMeasF'))
    close, bars, regimes = mic.make_panel(320, 12, 1)
    regimes[:] = 'LOW_VOL'
    # The real loader (Postgres/parquet) is booby-trapped: run_once must patch
    # it with the stub, and psycopg2.connect is blocked by the autouse fixture.
    with patch.object(adl, 'load_aux_data', side_effect=AssertionError('real aux')) as trap:
        recs, _, _ = mic.run_once(cls, close, bars, regimes, cache=True)
        assert adl.load_aux_data is trap
    assert recs
    trap.assert_not_called()


def test_bad_strategy_file_rc2(tmp_path, capsys):
    bad = tmp_path / 'bad_syntax.py'
    bad.write_text('def (:\n')
    assert mic.main(['--strategy-file', str(bad), '--days', DAYS, '--tickers', TICKERS]) == 2
    imp = tmp_path / 'bad_import.py'
    imp.write_text('import zz_no_such_module_xyz\n')
    assert mic.main(['--strategy-file', str(imp), '--days', DAYS, '--tickers', TICKERS]) == 2
    assert 'ERROR' in capsys.readouterr().err
