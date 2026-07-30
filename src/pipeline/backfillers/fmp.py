"""FMP (Financial Modeling Prep) backfiller.

Pulls historical financial statements + earnings surprises per universe
ticker and merges them into the master parquets
(data/master/financials.parquet, earnings.parquet). Reuses:

  - workspaces/default/tools/fmp.py — endpoint wrappers (statements,
    ratios, earnings-surprises, key-metrics). Already rate-limited via
    tools/_rate_limiter.py.
  - src/data/parquet_store.py::write_fundamentals — dedup-on-merge writer.

Supported column_name values (from schema_registry.json):
  financials      → quarterly income statement + ratios (revenue, net_income,
                    eps, pe_ratio, market_cap, roe, roic, ...) for every
                    ticker in the active universe.
  earnings        → per-ticker earnings surprises (actual vs estimate, %
                    surprise) for the requested window.
  financial_ratios, key_metrics — same as `financials` path (FMP returns
                    them together; we keep the single write path).

Lookback: FMP's per-symbol endpoints return the last N quarters. We compute
N = ceil(days / 90) + 1 so a 5-year (1825-day) backfill pulls ~21 quarters.
FMP starter/standard plans cap at ~30 quarters historical; if a strategy
needs longer, pay attention to the `limit_capped` flag in the return dict.
"""
from __future__ import annotations

import math
import os
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'workspaces' / 'default' / 'tools'))
sys.path.insert(0, str(ROOT / 'src'))


FINANCIALS_COLUMNS = {'financials', 'financial_ratios', 'key_metrics',
                      'revenue', 'eps', 'pe_ratio', 'market_cap',
                      'net_income', 'gross_margin', 'operating_margin',
                      'roe', 'roic', 'debt_equity_ratio', 'p_fcf_ratio',
                      'ev_ebitda', 'ev_revenue'}

INSIDER_COLUMNS    = {'insider', 'insider_transactions',
                      'cluster_buy_score', 'form_4'}

FMP_QUARTER_CAP    = 30    # plan's historical limit
# Seconds between TICKERS. The financials path now makes 4 API calls per
# ticker (statements + ratios + key-metrics + balance-sheet); at the old
# 0.05s the burst rate was ~80 calls/s vs FMP Starter's ~5/s cap — the
# 2026-07-03 sp500 backfill 429'd on 409/581 tickers. 1.0s keeps the
# steady-state at ~4 calls/s.
MIN_BACKFILL_CALLS = 1.0


def backfill(column_name: str, from_date: date, to_date: date) -> int:
    if column_name in FINANCIALS_COLUMNS:
        return _backfill_financials(from_date, to_date)
    if column_name in INSIDER_COLUMNS:
        return _backfill_insider(from_date, to_date)
    raise NotImplementedError(
        f'fmp backfiller has no handler for column={column_name!r}. '
        f'Known columns: {sorted(FINANCIALS_COLUMNS | INSIDER_COLUMNS)}'
    )


def _active_universe():
    """Load active tickers from the universe_config table. Falls back to the
    prices.parquet ticker list if the DB is unreachable."""
    try:
        import psycopg2
        conn = psycopg2.connect(os.environ['POSTGRES_URI'])
        cur  = conn.cursor()
        cur.execute("SELECT DISTINCT ticker FROM universe_config WHERE active = TRUE ORDER BY ticker")
        rows = [r[0] for r in cur.fetchall()]
        conn.close()
        if rows:
            return rows
    except Exception as e:
        print(f'  [fmp] universe_config unreachable ({e}); falling back to prices.parquet')
    import pandas as pd
    df = pd.read_parquet(ROOT / 'data' / 'master' / 'prices.parquet', columns=['ticker'])
    return sorted(df['ticker'].unique().tolist())


def _quarters_for(from_date: date, to_date: date) -> int:
    days = max((to_date - from_date).days, 90)
    q = int(math.ceil(days / 90)) + 1
    return min(q, FMP_QUARTER_CAP)


def build_financial_rows(ticker, statements, metrics, ratios, balance) -> list:
    """Statements+metrics+ratios+balance -> master financials.parquet rows.

    Extracted 2026-07-30 so the 14:30 tier-1 intraday adapter and this EOD
    backfiller build rows from ONE definition. A second copy would drift, and
    the drift would surface as strategies scoring a same-day reporter on
    subtly different fields than every other name in the cross-section.
    """
    out = []
    metrics_by_period = {m.get('date') or m.get('calendarYear'): m for m in (metrics or [])}
    ratios_by_period  = {r.get('date') or r.get('calendarYear'): r for r in (ratios  or [])}
    balance_by_period = {b.get('date') or b.get('calendarYear'): b for b in (balance or [])}

    for s in statements or []:
        period = s.get('period') or s.get('calendarYear')
        d      = s.get('date')
        if not d:
            continue
        m = metrics_by_period.get(d, {}) or {}
        r = ratios_by_period.get(d, {}) or {}
        b = balance_by_period.get(d, {}) or {}

        def _first(*vals):
            """First non-None value — unlike `or`-chains, a real 0.0 wins."""
            return next((v for v in vals if v is not None), None)

        rev   = _f(s.get('revenue'))
        gp    = _f(s.get('grossProfit'))
        ebit  = _f(s.get('ebitda') or s.get('operatingIncome'))
        ni    = _f(s.get('netIncome'))
        eps   = _f(s.get('eps'))
        gm    = (gp / rev) if rev and gp and rev > 0 else None
        om    = _f(s.get('operatingIncomeRatio')) or _f(r.get('operatingProfitMargin'))
        nm    = _f(s.get('netIncomeRatio'))       or _f(r.get('netProfitMargin'))

        out.append({
            'ticker':              ticker,
            'period':              period,
            'date':                d,
            'revenue':             rev,
            'gross_profit':        gp,
            'ebitda':              ebit,
            'net_income':          ni,
            'eps':                 eps,
            'gross_margin':        gm,
            'operating_margin':    om,
            'net_margin':          nm,
            # /stable/ field names FIRST, legacy v3 names as fallbacks.
            # The v3-only reads returned None for every row since the
            # stable migration — roe/roic/debt_equity/p_fcf were 0%
            # populated across 4,159 rows (found 2026-07-03; S10 +
            # S_bankruptcy were unable to signal anywhere).
            'revenue_growth':      _first(_f(r.get('revenueGrowth')), _f(m.get('revenueGrowthTTM'))),
            'ev_revenue':          _first(_f(m.get('evToSales')),
                                          _f(m.get('enterpriseValueOverRevenue')),
                                          _f(m.get('enterpriseValueOverRevenueTTM'))),
            'ev_ebitda':           _first(_f(m.get('evToEBITDA')),
                                          _f(r.get('enterpriseValueMultiple')),
                                          _f(m.get('enterpriseValueOverEBITDA')),
                                          _f(m.get('enterpriseValueOverEBITDATTM'))),
            'pe_ratio':            _first(_f(r.get('priceToEarningsRatio')),
                                          _f(m.get('peRatio')), _f(r.get('priceEarningsRatio'))),
            'market_cap':          _f(m.get('marketCap')),
            'roe':                 _first(_f(m.get('returnOnEquity')),
                                          _f(r.get('returnOnEquity')), _f(m.get('roeTTM'))),
            'roic':                _first(_f(m.get('returnOnInvestedCapital')),
                                          _f(m.get('returnOnCapitalEmployed')),
                                          _f(r.get('returnOnCapitalEmployed')), _f(m.get('roicTTM'))),
            'debt_equity_ratio':   _first(_f(r.get('debtToEquityRatio')),
                                          _f(r.get('debtEquityRatio')), _f(m.get('debtToEquityTTM'))),
            'p_fcf_ratio':         _first(_f(r.get('priceToFreeCashFlowRatio')),
                                          _f(r.get('priceToFreeCashFlowsRatio')), _f(m.get('pfcfRatioTTM'))),
            # Balance-sheet block (Altman-Z / S_bankruptcy_risk_anomaly)
            'total_assets':        _f(b.get('totalAssets')),
            'total_liabilities':   _f(b.get('totalLiabilities')),
            'retained_earnings':   _f(b.get('retainedEarnings')),
            'working_capital':     _first(_f(m.get('workingCapital')),
                                          (None if _f(b.get('totalCurrentAssets')) is None
                                           or _f(b.get('totalCurrentLiabilities')) is None
                                           else _f(b.get('totalCurrentAssets')) - _f(b.get('totalCurrentLiabilities')))),
            'operating_income':    _f(s.get('operatingIncome')),
        })
    return out


def _backfill_financials(from_date: date, to_date: date) -> int:
    """Pull quarterly income statement + key metrics + ratios for every
    universe ticker and merge into financials.parquet."""
    import fmp as fmp_tool      # workspaces/default/tools/fmp.py
    from data.parquet_store import write_fundamentals, row_count, FUNDAMENTALS_PATH

    tickers  = _active_universe()
    quarters = _quarters_for(from_date, to_date)
    print(f'  [fmp] backfilling financials: {len(tickers)} tickers × {quarters} quarters')

    rows_out = []
    failed   = 0
    for i, ticker in enumerate(tickers, 1):
        try:
            statements = fmp_tool.get_financial_statements(ticker, period='quarterly', limit=quarters)
            metrics    = fmp_tool.get_key_metrics(ticker, limit=quarters)
            ratios     = fmp_tool.get_ratios(ticker, limit=quarters)
            balance    = fmp_tool.get_balance_sheet(ticker, period='quarterly', limit=quarters)
        except Exception as e:
            failed += 1
            if failed <= 5:
                print(f'  [fmp] {ticker}: {e}')
            continue

        rows_out.extend(build_financial_rows(ticker, statements, metrics, ratios, balance))
        time.sleep(MIN_BACKFILL_CALLS)
        if i % 50 == 0:
            print(f'  [fmp] progress: {i}/{len(tickers)} tickers, {len(rows_out)} rows so far')

    if not rows_out:
        print(f'  [fmp] no rows fetched (failed={failed})')
        return 0

    total = write_fundamentals(rows_out)
    delta = total - row_count(FUNDAMENTALS_PATH) + len(rows_out)  # approximate new rows
    print(f'  [fmp] financials merged — {len(rows_out)} rows written, total={total}, failed={failed}')
    return len(rows_out)


def _backfill_insider(from_date: date, to_date: date) -> int:
    """Pull Form 4 insider trades per universe ticker via FMP's
    /insider-trading/search endpoint. Merges into insider.parquet
    (ticker, filing_date, insider_name, transaction_type, shares, price,
    net_value)."""
    import requests as _rq
    import pandas as pd
    from data.parquet_store import write_insider, row_count, INSIDER_PATH

    FMP_KEY = os.environ.get('FMP_API_KEY', '')
    if not FMP_KEY:
        raise RuntimeError('FMP_API_KEY not set')

    tickers = _active_universe()
    # FMP caps `limit` at 100 for /insider-trading/search. The endpoint
    # orders most-recent-first; to cover a long history we don't paginate
    # here (the endpoint offers no cursor), so for deep history the
    # operator should rely on the Form-4 EDGAR path once implemented.
    limit = 100 if (to_date - from_date).days > 365 else 50
    print(f'  [fmp] backfilling insider trades: {len(tickers)} tickers × {limit} recent')

    rows = []
    failed = 0
    cutoff_iso = from_date.isoformat()
    for i, ticker in enumerate(tickers, 1):
        try:
            url = (f'https://financialmodelingprep.com/stable/insider-trading/'
                   f'search?symbol={ticker}&limit={limit}&apikey={FMP_KEY}')
            r = _rq.get(url, timeout=30)
            if r.status_code in (402, 429):
                print(f'  [fmp] insider quota/rate-limit hit at ticker {ticker} — stopping')
                break
            if r.status_code == 404:
                continue
            r.raise_for_status()
            data = r.json() or []
        except Exception as e:
            failed += 1
            if failed <= 5:
                print(f'  [fmp] {ticker}: {e}')
            continue

        for txn in data:
            d = txn.get('transactionDate') or txn.get('filingDate')
            if not d or d < cutoff_iso:
                continue
            shares = _f(txn.get('securitiesTransacted'))
            price  = _f(txn.get('price'))
            rows.append({
                'ticker':            ticker,
                'filing_date':       txn.get('filingDate') or d,
                'transaction_date':  txn.get('transactionDate') or d,
                'insider_name':      txn.get('reportingName') or txn.get('name') or '',
                'role':              txn.get('typeOfOwner') or '',
                'transaction_type':  txn.get('transactionType') or '',
                'shares':            shares,
                'price_per_share':   price,
                'net_value':         (shares * price) if (shares is not None and price is not None) else None,
                'shares_owned_after':_f(txn.get('securitiesOwned')),
            })
        time.sleep(MIN_BACKFILL_CALLS)
        if i % 50 == 0:
            print(f'  [fmp] progress: {i}/{len(tickers)} tickers, {len(rows)} txns')

    if not rows:
        print(f'  [fmp] no insider rows fetched (failed={failed})')
        return 0
    total = write_insider(rows)
    print(f'  [fmp] insider merged — {len(rows)} rows written, total={total}, failed={failed}')
    return len(rows)


def _f(v):
    if v is None:
        return None
    try:
        f = float(v)
        if f != f or f in (float('inf'), float('-inf')):
            return None
        return f
    except (TypeError, ValueError):
        return None
