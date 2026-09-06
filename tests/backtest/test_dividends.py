from __future__ import annotations
import logging
import pandas as pd
import pytest


SPY_EX_DATES = ['2024-03-15', '2024-06-15', '2024-09-15', '2024-12-15', '2025-03-15', '2025-06-15',
                '2025-09-15', '2025-12-15', '2026-03-15', '2026-06-15']          # quarterly, 10 payments


def _fixture(tmp_path, monkeypatch):
    rows = [{'symbol': 'SPY', 'action_type': 'cash_dividend', 'ex_date': pd.Timestamp(d).date(), 'cash_amount': 1.5}
            for d in SPY_EX_DATES]
    rows.append({'symbol': 'ONE', 'action_type': 'cash_dividend', 'ex_date': pd.Timestamp('2025-05-15').date(), 'cash_amount': 2.0})
    rows.append({'symbol': 'SPY', 'action_type': 'forward_split', 'ex_date': pd.Timestamp('2025-01-02').date(), 'cash_amount': None})
    rows.append({'symbol': 'NODIV', 'action_type': 'reverse_split', 'ex_date': pd.Timestamp('2025-01-02').date(), 'cash_amount': None})
    p = tmp_path / 'corporate_actions.parquet'
    pd.DataFrame(rows).to_parquet(p, index=False)
    monkeypatch.setenv('OPENCLAW_CORPORATE_ACTIONS_PARQUET', str(p))
    from backtest import dividends
    dividends.clear_cache()
    return dividends


def test_trailing_year_sum_over_spot(tmp_path, monkeypatch):
    dv = _fixture(tmp_path, monkeypatch)
    # (2025-06-30, 2026-06-30] holds 2025-09-15, 2025-12-15, 2026-03-15, 2026-06-15 → 4 × 1.5
    assert dv.dividend_yield_asof('SPY', '2026-06-30', 500.0) == pytest.approx(6.0 / 500.0)


def test_window_is_open_below_and_closed_above(tmp_path, monkeypatch):
    dv = _fixture(tmp_path, monkeypatch)                            # ONE pays 2.0 on 2025-05-15 only
    assert dv.dividend_yield_asof('ONE', '2025-05-15', 100.0) == pytest.approx(0.02)   # ex_date == as_of counts
    assert dv.dividend_yield_asof('ONE', '2025-05-14', 100.0) == 0.0                   # not yet
    assert dv.dividend_yield_asof('ONE', '2026-05-14', 100.0) == pytest.approx(0.02)   # 364 days later: still inside
    assert dv.dividend_yield_asof('ONE', '2026-05-15', 100.0) == 0.0                   # ex_date == as_of − 365 d: out


def test_no_dividend_ticker_and_unknown_ticker_are_zero(tmp_path, monkeypatch):
    dv = _fixture(tmp_path, monkeypatch)
    assert dv.dividend_yield_asof('NODIV', '2026-06-30', 50.0) == 0.0
    assert dv.dividend_yield_asof('ZZZT', '2026-06-30', 50.0) == 0.0
    assert dv.dividend_yield_asof('SPY', '2026-06-30', 0.0) == 0.0


def test_pre_coverage_backfills_first_full_year_and_warns_once(tmp_path, monkeypatch, caplog):
    dv = _fixture(tmp_path, monkeypatch)
    assert dv.coverage_start() == pd.Timestamp('2024-03-15')
    assert dv.backfill_reference_date() == pd.Timestamp('2025-03-15')
    with caplog.at_level(logging.WARNING):
        q1 = dv.dividend_yield_asof('SPY', '2024-06-01', 400.0)               # trailing window starts 2023-06 < coverage
        q2 = dv.dividend_yield_asof('SPY', '2024-09-01', 400.0, ref_spot=600.0)
    # first full year [2024-03-15, 2025-03-15): 2024-03-15, 06-15, 09-15, 12-15 → 4 × 1.5 = 6.0
    assert q1 == pytest.approx(6.0 / 400.0)
    assert q2 == pytest.approx(6.0 / 600.0)                                    # ref_spot wins when given
    assert sum('q backfilled' in r.message for r in caplog.records) == 1


def test_unreadable_file_is_zero_not_an_error(tmp_path, monkeypatch):
    bad = tmp_path / 'corporate_actions.parquet'; bad.write_text('not a parquet')
    monkeypatch.setenv('OPENCLAW_CORPORATE_ACTIONS_PARQUET', str(bad))
    from backtest import dividends
    dividends.clear_cache()
    assert dividends.dividend_yield_asof('SPY', '2026-06-30', 500.0) == 0.0
    assert dividends.coverage_start() is None
