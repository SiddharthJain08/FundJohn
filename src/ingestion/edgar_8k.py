"""SEC EDGAR 8-K ingester.

Async; runs over a list of tickers, calls EDGARClient.get_submissions and
fetch_document, parses Items, dual-writes to market_news + edgar_8k_filings.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import psycopg2

from src.ingestion.edgar_client import EDGARClient
from src.ingestion.edgar_items import (
    ITEM_DESCRIPTIONS,
    UNPARSED_DESCRIPTION,
    UNPARSED_PLACEHOLDER,
    parse_items_from_document,
)
from src.pipeline.backfillers.edgar import _load_ticker_to_cik


log = logging.getLogger(__name__)

_SEC_ARCHIVE_BASE = 'https://www.sec.gov/Archives/edgar/data'


# ---------- Pure helpers ----------

def _accession_no_dashes(accession: str) -> str:
    return accession.replace('-', '')


def _primary_doc_url(cik: str, accession: str, primary_document: str) -> str:
    cik_int = str(int(cik))  # strip leading zeros
    return f'{_SEC_ARCHIVE_BASE}/{cik_int}/{_accession_no_dashes(accession)}/{primary_document}'


def _compose_title(items: list[str], ticker: str) -> str:
    if not items:
        return f'8-K filed (Items unparsed) — {ticker}'
    parts = [
        f'Item {n} ({ITEM_DESCRIPTIONS[n].split(";", 1)[0].strip()})'
        for n in items if n in ITEM_DESCRIPTIONS
    ]
    return f'8-K — {", ".join(parts)} — {ticker}'


def _compose_summary(items: list[str]) -> str:
    if not items:
        return UNPARSED_DESCRIPTION
    return ' '.join(
        f'Item {n}: {ITEM_DESCRIPTIONS[n]}.'
        for n in items if n in ITEM_DESCRIPTIONS
    )


def _parse_accepted_at(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError:
        return None


# ---------- DB helpers ----------

def _existing_accessions_for(tickers: list[str]) -> set[str]:
    if not tickers:
        return set()
    dsn = os.environ['POSTGRES_URI']
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            'SELECT DISTINCT accession FROM edgar_8k_filings WHERE ticker = ANY(%s)',
            (tickers,),
        )
        return {row[0] for row in cur.fetchall()}


def _upsert_market_news_row(row: dict) -> None:
    dsn = os.environ['POSTGRES_URI']
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO market_news
              (uuid, primary_ticker, title, publisher, url, published_at,
               summary, related_tickers)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (uuid) DO NOTHING
            """,
            (
                row['uuid'], row['primary_ticker'], row['title'],
                row['publisher'], row['url'], row['published_at'],
                row['summary'], row['related_tickers'],
            ),
        )
        conn.commit()


def _upsert_edgar_8k_rows(rows: list[dict]) -> None:
    if not rows:
        return
    dsn = os.environ['POSTGRES_URI']
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        for r in rows:
            cur.execute(
                """
                INSERT INTO edgar_8k_filings
                  (accession, cik, ticker, filing_date, accepted_at,
                   item_number, item_description, primary_doc_url,
                   market_news_uuid)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (accession, item_number) DO NOTHING
                """,
                (
                    r['accession'], r['cik'], r['ticker'], r['filing_date'],
                    r['accepted_at'], r['item_number'], r['item_description'],
                    r['primary_doc_url'], r['market_news_uuid'],
                ),
            )
        conn.commit()


def _record_provider_health(success: bool, error: Optional[str] = None) -> None:
    """Lazy import to avoid a hard dep at module load."""
    try:
        from src.maintenance.provider_health import record
        record('edgar', 'submissions', success=success, error=error)
    except Exception:  # noqa: BLE001 — telemetry must never break the ingester
        log.debug('provider_health record failed (ignored)', exc_info=True)


# ---------- Per-filing handler ----------

async def _process_filing(
    client: EDGARClient,
    ticker: str,
    cik: str,
    filing: dict,
) -> Optional[tuple[dict, list[dict]]]:
    """Returns (market_news_row, edgar_8k_rows) or None on hard failure.

    On parse failure: returns the market_news row with the "unparsed"
    title and a single edgar_8k row with item_number=UNPARSED."""
    accession = filing['accession']
    primary_document = filing['primary_document']
    filing_date = filing['filing_date']
    accepted_at = _parse_accepted_at(filing.get('accepted_at'))
    primary_doc_url = _primary_doc_url(cik, accession, primary_document)

    doc_bytes = await client.fetch_document(primary_doc_url)
    items = parse_items_from_document(doc_bytes) if doc_bytes else []

    title = _compose_title(items, ticker)
    summary = _compose_summary(items)

    published_at = (
        accepted_at or datetime.combine(
            datetime.strptime(filing_date, '%Y-%m-%d').date(),
            datetime.min.time(),
            tzinfo=timezone.utc,
        )
    )

    market_news_row = {
        'uuid': accession,
        'primary_ticker': ticker,
        'title': title,
        'publisher': 'SEC EDGAR',
        'url': primary_doc_url,
        'published_at': published_at,
        'summary': summary,
        'related_tickers': [ticker],
    }

    if items:
        edgar_rows = [
            {
                'accession': accession, 'cik': cik, 'ticker': ticker,
                'filing_date': filing_date, 'accepted_at': accepted_at,
                'item_number': n,
                'item_description': ITEM_DESCRIPTIONS[n],
                'primary_doc_url': primary_doc_url,
                'market_news_uuid': accession,
            }
            for n in items
        ]
    else:
        edgar_rows = [{
            'accession': accession, 'cik': cik, 'ticker': ticker,
            'filing_date': filing_date, 'accepted_at': accepted_at,
            'item_number': UNPARSED_PLACEHOLDER,
            'item_description': UNPARSED_DESCRIPTION,
            'primary_doc_url': primary_doc_url,
            'market_news_uuid': accession,
        }]
    return market_news_row, edgar_rows


# ---------- Per-ticker handler ----------

async def _process_ticker(
    client: EDGARClient,
    ticker: str,
    cik: str,
    cutoff: datetime,
    already_have: set[str],
) -> int:
    try:
        subs = await client.get_submissions(cik)
    except Exception as e:  # noqa: BLE001
        log.warning('get_submissions failed for %s (cik=%s): %s', ticker, cik, e)
        _record_provider_health(success=False, error=str(e)[:200])
        return 0

    if not subs:
        return 0

    recent = subs.get('filings', {}).get('recent', {})
    forms = recent.get('form', [])
    accessions = recent.get('accessionNumber', [])
    filing_dates = recent.get('filingDate', [])
    primary_documents = recent.get('primaryDocument', [])
    accepted_dts = recent.get('acceptanceDateTime', [''] * len(forms))

    new_count = 0
    for idx, form in enumerate(forms):
        if form != '8-K':
            continue
        accession = accessions[idx]
        if accession in already_have:
            continue
        filing_date_str = filing_dates[idx]
        try:
            filing_dt = datetime.strptime(filing_date_str, '%Y-%m-%d').replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        if filing_dt < cutoff:
            # Filings are typically reverse-chronological; we could break here,
            # but skipping is safer if order is ever surprising.
            continue

        try:
            result = await _process_filing(client, ticker, cik, {
                'accession': accession,
                'filing_date': filing_date_str,
                'primary_document': primary_documents[idx],
                'accepted_at': accepted_dts[idx] if idx < len(accepted_dts) else '',
            })
        except Exception as e:  # noqa: BLE001
            log.warning('process_filing failed %s/%s: %s', ticker, accession, e)
            continue

        if result is None:
            continue
        mn_row, edgar_rows = result
        try:
            _upsert_market_news_row(mn_row)
            _upsert_edgar_8k_rows(edgar_rows)
            new_count += 1
        except Exception as e:  # noqa: BLE001
            log.warning('DB upsert failed %s/%s: %s', ticker, accession, e)
            continue

    _record_provider_health(success=True)
    return new_count


# ---------- Entry ----------

async def ingest_8k_filings(
    tickers: list[str],
    lookback_hours: int,
) -> dict[str, int]:
    """Returns {ticker: new_filings_count} for tickers that had a CIK lookup hit.
    Tickers without CIKs are silently skipped (not in the returned dict)."""
    if not tickers:
        return {}

    cik_map = await _load_ticker_to_cik()
    mapped = [(t, cik_map[t]) for t in tickers if t in cik_map]
    if not mapped:
        return {}

    mapped_tickers = [t for t, _ in mapped]
    already_have = _existing_accessions_for(mapped_tickers)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    results: dict[str, int] = {}
    async with EDGARClient() as client:
        for ticker, cik in mapped:
            results[ticker] = await _process_ticker(
                client, ticker, cik, cutoff, already_have
            )
    return results
