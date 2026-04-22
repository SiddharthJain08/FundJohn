"""SEC EDGAR backfiller.

Direct-from-SEC Form 4 / 10-K / 10-Q / 8-K. Reuses
src/ingestion/edgar_client.py (async client with User-Agent enforcement
and exponential backoff per SEC fair-use policy).

For insider data, prefer the fmp backfiller — FMP's aggregated feed is
cheaper and includes transaction pricing. EDGAR is the source of truth
for genuine historical backfill beyond FMP's rolling-100 window; wire it
here when that becomes needed.

Supported column_name values:
  filings               → company filings index (last N years)
  form_10k, form_10q,
  form_8k               → type-filtered filing index
  form_4_direct         → direct-from-SEC Form 4 (bypasses FMP)
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))


FILING_COLUMNS = {
    'filings':       None,
    'form_10k':      ['10-K'],
    'form_10q':      ['10-Q'],
    'form_8k':       ['8-K'],
    'form_4_direct': ['4'],
}


def backfill(column_name: str, from_date: date, to_date: date) -> int:
    if column_name in FILING_COLUMNS:
        return asyncio.run(_backfill_filings(column_name, from_date, to_date))
    raise NotImplementedError(
        f'edgar backfiller has no handler for column={column_name!r}. '
        f'Known columns: {sorted(FILING_COLUMNS)}.'
    )


async def _backfill_filings(column_name: str, from_date: date, to_date: date) -> int:
    """Fetch the filings index per universe ticker, filtered to the
    column's form types, and append to data/master/filings.parquet.

    Ticker → CIK is resolved via SEC's ticker-to-CIK mapping
    (https://www.sec.gov/files/company_tickers.json). The mapping is
    cached locally at data/master/_sec_ticker_cik.json for 7 days.

    Does not download full filing XML/HTML — just the index rows
    (accession_no, form_type, filing_date, period_of_report,
    primary_doc_url) so strategies can dereference later on demand.
    """
    from ingestion.edgar_client import fetch_filings
    from data.parquet_store import MASTER_DIR, append_dedup
    import pandas as pd

    filings_path = MASTER_DIR / 'filings.parquet'
    form_types   = FILING_COLUMNS.get(column_name)
    ticker_to_cik = await _load_ticker_to_cik()
    tickers      = _active_universe()
    known_tickers = [t for t in tickers if t in ticker_to_cik]
    print(f'  [edgar] backfilling {column_name} for {len(known_tickers)}/{len(tickers)} tickers '
          f'({"all forms" if form_types is None else ",".join(form_types)})')

    rows  = []
    failed = 0
    cutoff = from_date.isoformat()
    for i, ticker in enumerate(known_tickers, 1):
        cik = ticker_to_cik[ticker]
        try:
            filings = await fetch_filings(cik, form_types=form_types)
        except Exception as e:
            failed += 1
            if failed <= 5:
                print(f'  [edgar] {ticker} (cik {cik}): {e}')
            continue
        for f in filings or []:
            fd = f.get('filingDate') or f.get('filing_date') or f.get('date')
            if not fd or fd < cutoff:
                continue
            rows.append({
                'ticker':            ticker,
                'cik':               cik,
                'form_type':         f.get('form') or f.get('form_type'),
                'filing_date':       fd,
                'period_of_report':  f.get('reportDate') or f.get('period_of_report'),
                'accession_no':      f.get('accessionNumber') or f.get('accession') or f.get('accession_no'),
                'primary_doc_url':   f.get('primaryDocument') or f.get('primary_doc_url'),
            })
        if i % 50 == 0:
            print(f'  [edgar] progress: {i}/{len(known_tickers)} tickers, {len(rows)} filings')

    if not rows:
        print(f'  [edgar] no filings fetched (failed={failed})')
        return 0
    df = pd.DataFrame(rows)
    total = append_dedup(filings_path, df, ['ticker', 'accession_no'], mode='replace')
    print(f'  [edgar] merged — {len(rows)} rows written, total={total}, failed={failed}')
    return len(rows)


async def _load_ticker_to_cik() -> dict[str, str]:
    """Load the SEC ticker→CIK JSON. Caches locally for 7 days."""
    import json, time, aiohttp
    cache = ROOT / 'data' / 'master' / '_sec_ticker_cik.json'
    if cache.exists() and (time.time() - cache.stat().st_mtime) < 7 * 86400:
        return json.loads(cache.read_text())
    async with aiohttp.ClientSession(headers={'User-Agent': 'FundJohn/OpenClaw contact@fundjohn.ai'}) as s:
        async with s.get('https://www.sec.gov/files/company_tickers.json', timeout=30) as r:
            if r.status != 200:
                return {}
            payload = await r.json()
    out = {row['ticker']: str(row['cik_str']).zfill(10) for row in payload.values() if row.get('ticker')}
    cache.write_text(json.dumps(out))
    return out


def _active_universe():
    try:
        import psycopg2
        conn = psycopg2.connect(os.environ['POSTGRES_URI'])
        cur  = conn.cursor()
        cur.execute("SELECT DISTINCT ticker FROM universe_config WHERE active = TRUE ORDER BY ticker")
        rows = [r[0] for r in cur.fetchall()]
        conn.close()
        if rows:
            return rows
    except Exception:
        pass
    import pandas as pd
    df = pd.read_parquet(ROOT / 'data' / 'master' / 'prices.parquet', columns=['ticker'])
    return sorted(df['ticker'].unique().tolist())
