"""Unit tests for scripts/generate_tearsheet.py (task P3+R3, 2026-08-24).

No real DB access — the DB-facing loaders (_fetch_run/_fetch_trades) are
monkeypatched. Synthetic data only.

Includes the brief-mandated quantstats-on-pandas-3 probe: a unit test that
directly calls `qs.reports.html(series, output=path, title=...)` on a
synthetic series to confirm the library still works on this box's pandas
3.0.2 (verified separately in an ad-hoc probe before writing any code; see
task-P3R3-report.md).
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'src'))

import generate_tearsheet as gt  # noqa: E402


# ── quantstats-on-pandas-3 probe (brief-mandated) ───────────────────────────

def test_quantstats_probe_synthetic_series(tmp_path):
    """Direct probe: quantstats 0.0.81 report generation must not raise on
    this box's pandas 3.0.2. If this ever starts failing, generate_tearsheet's
    runtime try/except will route to the self-rendered fallback automatically
    — but we want to know explicitly, not just silently degrade."""
    import matplotlib
    matplotlib.use('Agg')
    import quantstats as qs

    idx = pd.date_range('2024-01-01', periods=60, freq='D')
    rng = np.random.default_rng(0)
    series = pd.Series(rng.normal(0.001, 0.01, size=60), index=idx)
    out = tmp_path / 'probe.html'
    qs.reports.html(series, output=str(out), title='probe')
    assert out.exists()
    assert out.stat().st_size > 1024


# ── build_daily_returns ──────────────────────────────────────────────────

def test_build_daily_returns_zero_on_no_exit_days():
    trades = [
        {'exit_date': date(2024, 1, 2), 'pnl_pct': 2.0},
        {'exit_date': date(2024, 1, 2), 'pnl_pct': -1.0},
        {'exit_date': date(2024, 1, 4), 'pnl_pct': 3.0},
    ]
    daily = gt.build_daily_returns(trades, date(2024, 1, 1), date(2024, 1, 5))
    assert daily.loc[pd.Timestamp('2024-01-01')] == pytest.approx(0.0)
    assert daily.loc[pd.Timestamp('2024-01-02')] == pytest.approx((2.0 - 1.0) / 100.0)
    assert daily.loc[pd.Timestamp('2024-01-03')] == pytest.approx(0.0)
    assert daily.loc[pd.Timestamp('2024-01-04')] == pytest.approx(3.0 / 100.0)
    assert daily.loc[pd.Timestamp('2024-01-05')] == pytest.approx(0.0)


def test_build_daily_returns_ignores_trades_with_no_exit():
    trades = [{'exit_date': None, 'pnl_pct': 5.0}]
    daily = gt.build_daily_returns(trades, date(2024, 1, 1), date(2024, 1, 3))
    assert (daily == 0.0).all()


# ── generate_html_tearsheet ──────────────────────────────────────────────

def _synthetic_returns(n=60, seed=0):
    idx = pd.date_range('2024-01-01', periods=n, freq='D')
    rng = np.random.default_rng(seed)
    return pd.Series(rng.normal(0.001, 0.01, size=n), index=idx)


def _synthetic_trades(n=25, seed=0):
    rng = np.random.default_rng(seed)
    return [
        {'ticker': 'ZZT1', 'pnl_pct': float(rng.normal(0.5, 2.0)),
         'exit_date': date(2024, 1, 1 + (i % 28))}
        for i in range(n)
    ]


def test_generate_html_tearsheet_writes_file_over_1kb(tmp_path):
    out_path = tmp_path / 'S_ZZT_run123.html'
    returns = _synthetic_returns()
    trades = _synthetic_trades()
    result = gt.generate_html_tearsheet(
        returns, trades, strategy_id='S_ZZT', run_id='run123',
        output_path=out_path,
    )
    assert Path(result).exists()
    assert Path(result).stat().st_size > 1024


def test_generate_html_tearsheet_falls_back_when_quantstats_raises(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError('synthetic pandas-3 incompatibility')
    monkeypatch.setattr(gt, '_render_quantstats', _boom)

    out_path = tmp_path / 'S_ZZT_fallback.html'
    returns = _synthetic_returns()
    trades = _synthetic_trades()
    result = gt.generate_html_tearsheet(
        returns, trades, strategy_id='S_ZZT', run_id='fallback-run',
        output_path=out_path,
    )
    html = Path(result).read_text()
    assert Path(result).stat().st_size > 1024
    assert 'Sortino' in html  # fallback stats table reuses tail_stats


def test_fallback_reuses_tail_stats_sortino(tmp_path):
    """The fallback's reported Sortino must come from sleeve_tail_stats,
    not an independently-reimplemented formula.

    The trade fixture is built with deterministic mixed-sign pnl_pct (not
    drawn from an RNG) so downside_dev is guaranteed > 0 and sortino is
    guaranteed non-None -- an RNG-derived fixture that happened to land
    all-positive would silently skip this test's only assertion (the
    finding this test now guards against)."""
    from backtest.tail_stats import sleeve_tail_stats

    trades = [
        {'ticker': 'ZZT1', 'pnl_pct': 3.0 if i % 3 else -4.0,
         'exit_date': date(2024, 1, 1 + (i % 28))}
        for i in range(25)
    ]
    pnl_list = [t['pnl_pct'] for t in trades]
    expected = sleeve_tail_stats(pnl_list)
    assert expected['sortino'] is not None  # fixture must be non-degenerate

    out_path = tmp_path / 'S_ZZT_direct_fallback.html'
    returns = _synthetic_returns(seed=1)
    gt._render_fallback(
        returns, trades, strategy_id='S_ZZT', run_id='direct-fallback',
        output_path=out_path,
    )
    html = out_path.read_text()
    assert f'{expected["sortino"]:.4f}' in html


# ── run() orchestration (no real DB — loaders monkeypatched) ────────────

def test_run_zero_trades_exits_0_with_reason(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gt, '_load_env', lambda: None)
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://fake/fake')
    monkeypatch.setattr(gt.psycopg2, 'connect', lambda uri: MagicMock())
    monkeypatch.setattr(gt, '_fetch_run', lambda conn, **kw: {
        'run_id': 'r1', 'strategy_id': 'S_ZZT',
        'start_date': date(2024, 1, 1), 'end_date': date(2024, 1, 10),
    })
    monkeypatch.setattr(gt, '_fetch_trades', lambda conn, run_id: [])

    rc = gt.run(run_id='r1', output_dir=tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert 'no trades' in out.lower()
    assert not list(tmp_path.glob('*.html'))


def test_run_missing_run_exits_0_with_reason(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gt, '_load_env', lambda: None)
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://fake/fake')
    monkeypatch.setattr(gt.psycopg2, 'connect', lambda uri: MagicMock())
    monkeypatch.setattr(gt, '_fetch_run', lambda conn, **kw: None)

    rc = gt.run(run_id='does-not-exist', output_dir=tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert 'no run' in out.lower()


def test_run_writes_tearsheet_and_prints_path(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gt, '_load_env', lambda: None)
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://fake/fake')
    monkeypatch.setattr(gt.psycopg2, 'connect', lambda uri: MagicMock())
    monkeypatch.setattr(gt, '_fetch_run', lambda conn, **kw: {
        'run_id': 'r2', 'strategy_id': 'S_ZZT',
        'start_date': date(2024, 1, 1), 'end_date': date(2024, 2, 29),
    })
    monkeypatch.setattr(gt, '_fetch_trades', lambda conn, run_id: _synthetic_trades())

    rc = gt.run(run_id='r2', output_dir=tmp_path)
    out = capsys.readouterr().out.strip()
    assert rc == 0
    printed_path = Path(out.splitlines()[-1])
    assert printed_path.exists()
    assert printed_path.stat().st_size > 1024
