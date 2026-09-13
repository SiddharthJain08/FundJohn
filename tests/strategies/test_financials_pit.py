# tests/strategies/test_financials_pit.py
"""A1 (spec 2026-09-12 §1) — fundamentals become visible on their FILING date,
not their period end. Synthetic frames only; never reads data/master."""
from __future__ import annotations

import pandas as pd
import pytest

from strategies import aux_data_loader as adl


def _install(monkeypatch, tmp_path, fin: pd.DataFrame, earn: pd.DataFrame | None):
    fp = tmp_path / 'financials.parquet'
    fin.to_parquet(fp, index=False)
    monkeypatch.setattr(adl, 'FINANCIALS_PATH', fp)
    ep = tmp_path / 'earnings.parquet'
    (earn if earn is not None else pd.DataFrame({'ticker': [], 'date': []})).to_parquet(ep, index=False)
    monkeypatch.setattr(adl, 'EARNINGS_PATH', ep)
    monkeypatch.setattr(adl, '_FIN_DF', None)
    monkeypatch.setattr(adl, '_FIN_AVAIL_DF', None)
    monkeypatch.setattr(adl, '_EARNINGS_DF', None)


FIN = pd.DataFrame({
    'ticker':       ['AAA', 'BBB'],
    'period':       ['2026Q1', '2026Q1'],
    'date':         ['2026-03-31', '2026-03-31'],
    'roe':          [1.40, 0.20],
    'total_assets': [3.6e11, 1.0e11],
})
EARN = pd.DataFrame({'ticker': ['AAA'], 'date': ['2026-05-05']})


def test_pit_hides_unfiled_quarter(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, FIN, EARN)
    monkeypatch.setenv('OPENCLAW_FINANCIALS_PIT', '1')
    assert adl._financials_slice('2026-04-15') == {}


def test_pit_reveals_on_the_report_date(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, FIN, EARN)
    monkeypatch.setenv('OPENCLAW_FINANCIALS_PIT', '1')
    out = adl._financials_slice('2026-05-05')
    assert set(out) == {'AAA'}
    assert out['AAA']['returnOnEquity'] == 1.40
    assert 'available_at' not in out['AAA']


def test_pit_fallback_sixty_days_when_no_earnings_row(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, FIN, EARN)
    monkeypatch.setenv('OPENCLAW_FINANCIALS_PIT', '1')
    assert 'BBB' not in adl._financials_slice('2026-05-29')   # 03-31 + 60 d = 05-30
    assert 'BBB' in adl._financials_slice('2026-05-30')


def test_pit_ignores_a_report_beyond_the_120_day_window(monkeypatch, tmp_path):
    late = pd.DataFrame({'ticker': ['AAA'], 'date': ['2026-08-15']})   # 03-31 + 137 d
    _install(monkeypatch, tmp_path, FIN, late)
    monkeypatch.setenv('OPENCLAW_FINANCIALS_PIT', '1')
    assert 'AAA' in adl._financials_slice('2026-05-30')   # falls back to +60 d, not 08-15


def test_flag_unset_is_the_legacy_period_end_slice(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, FIN, EARN)
    monkeypatch.delenv('OPENCLAW_FINANCIALS_PIT', raising=False)
    legacy = adl._financials_slice('2026-04-15')
    assert set(legacy) == {'AAA', 'BBB'}
    assert legacy['AAA']['returnOnEquity'] == 1.40


def test_availability_frame_is_cached(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, FIN, EARN)
    monkeypatch.setenv('OPENCLAW_FINANCIALS_PIT', '1')
    first = adl._financials_with_availability()
    assert adl._financials_with_availability() is first
    assert str(first.loc[first['ticker'] == 'AAA', 'available_at'].iloc[0])[:10] == '2026-05-05'
    assert str(first.loc[first['ticker'] == 'BBB', 'available_at'].iloc[0])[:10] == '2026-05-30'


def test_prior_period_aliases_survive_pit(monkeypatch, tmp_path):
    fin = pd.DataFrame({
        'ticker':          ['AAA', 'AAA'],
        'period':          ['2025Q1', '2026Q1'],
        'date':            ['2025-03-31', '2026-03-31'],
        'total_assets':    [3.0e11, 3.6e11],
        'working_capital': [1.0e10, 1.2e10],
    })
    earn = pd.DataFrame({'ticker': ['AAA', 'AAA'], 'date': ['2025-05-05', '2026-05-05']})
    _install(monkeypatch, tmp_path, fin, earn)
    monkeypatch.setenv('OPENCLAW_FINANCIALS_PIT', '1')
    out = adl._financials_slice('2026-06-01')
    assert out['AAA']['totalAssets'] == 3.6e11
    assert out['AAA']['totalAssetsPriorYear'] == 3.0e11
    assert out['AAA']['workingCapitalPriorYear'] == 1.0e10
