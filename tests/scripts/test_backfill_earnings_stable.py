"""scripts/backfill_earnings.py — FMP /stable/earnings row mapper.

The script targeted /api/v3/historical/earning_calendar/{sym} (403 Legacy
Endpoint). /stable/earnings?symbol= carries the same actual-vs-estimate
fields under new names; rows must land in earnings.parquet's LIVE schema
(ticker, date, eps_actual, eps_estimated, revenue_actual, revenue_estimated,
last_updated) — not the v3-era fiscal_date_ending/updated_from_date columns.
"""
from __future__ import annotations

from datetime import date

from scripts import backfill_earnings as mod

STABLE = [
    {'symbol': 'AAPL', 'date': '2026-10-29', 'epsActual': None, 'epsEstimated': 1.98,
     'revenueActual': None, 'revenueEstimated': 113205200000, 'lastUpdated': '2026-08-23'},
    {'symbol': 'AAPL', 'date': '2026-07-30', 'epsActual': 2.02, 'epsEstimated': 1.89,
     'revenueActual': 109417000000, 'revenueEstimated': 109038900000, 'lastUpdated': '2026-08-23'},
    {'symbol': 'AAPL', 'date': None, 'epsActual': 1.0},   # no date -> dropped
]


def test_rows_from_stable_maps_to_master_schema():
    rows = mod.rows_from_stable('AAPL', STABLE, date(2026, 8, 23))
    assert [r['date'] for r in rows] == ['2026-10-29', '2026-07-30']
    assert set(rows[0]) == {'ticker', 'date', 'eps_actual', 'eps_estimated',
                            'revenue_actual', 'revenue_estimated', 'last_updated'}
    assert rows[1]['eps_actual'] == 2.02 and rows[1]['eps_estimated'] == 1.89
    assert rows[1]['revenue_actual'] == 109417000000
    assert rows[0]['eps_actual'] is None          # upcoming quarter keeps NaN actual
    assert rows[0]['last_updated'] == '2026-08-23'


def test_url_is_stable():
    assert mod.FMP_EARNINGS_URL == 'https://financialmodelingprep.com/stable/earnings'
    assert 'api/v3' not in mod.FMP_EARNINGS_URL


def test_merge_never_drops_existing_rows_even_duplicates():
    import pandas as pd
    existing = pd.DataFrame([
        {'ticker': 'AAPL', 'date': '2026-07-30', 'eps_actual': 2.02, 'eps_estimated': 1.89,
         'revenue_actual': 1.0, 'revenue_estimated': 1.0, 'last_updated': '2026-08-01'},
        {'ticker': 'AAPL', 'date': '2026-07-30', 'eps_actual': 2.02, 'eps_estimated': 1.89,
         'revenue_actual': 1.0, 'revenue_estimated': 1.0, 'last_updated': '2026-08-02'},  # pre-existing dup
    ])
    new = pd.DataFrame(mod.rows_from_stable('AAPL', STABLE, date(2026, 8, 23)))
    out = mod.merge_append_only(existing, new)
    assert len(out) == 3                       # 2 existing kept (dup included) + 1 new
    assert (out['last_updated'].iloc[:2] == ['2026-08-01', '2026-08-02']).all()
    assert '2026-10-29' in set(out['date'].astype(str).str[:10])
