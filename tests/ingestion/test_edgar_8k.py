import asyncio
from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock, MagicMock
import pytest

from src.ingestion.edgar_8k import (
    ingest_8k_filings,
    _compose_title,
    _accession_no_dashes,
    _primary_doc_url,
)


def _run(coro):
    return asyncio.run(coro)


# ---------- Pure helpers ----------

def test_accession_no_dashes_strips_hyphens():
    assert _accession_no_dashes('0000320193-26-000011') == '000032019326000011'


def test_primary_doc_url_builds_correctly():
    url = _primary_doc_url(
        cik='0000320193',
        accession='0000320193-26-000011',
        primary_document='aapl-20260430.htm',
    )
    assert url == (
        'https://www.sec.gov/Archives/edgar/data/320193/'
        '000032019326000011/aapl-20260430.htm'
    )


def test_compose_title_two_items_includes_ticker_and_descriptions():
    title = _compose_title(['5.02', '9.01'], 'GLW')
    assert 'GLW' in title
    assert '5.02' in title
    assert '9.01' in title
    assert 'Departure of Directors' in title or 'Officers' in title


def test_compose_title_unparsed_fallback():
    title = _compose_title([], 'GLW')
    assert 'GLW' in title
    assert 'unparsed' in title.lower() or 'no items' in title.lower()


# ---------- Integration with mocked EDGARClient + DB ----------

def _make_submissions(filings):
    """filings: list of dicts with keys form, accession, filing_date, primaryDocument"""
    return {
        'filings': {
            'recent': {
                'form':            [f['form'] for f in filings],
                'accessionNumber': [f['accession'] for f in filings],
                'filingDate':      [f['filing_date'] for f in filings],
                'primaryDocument': [f['primaryDocument'] for f in filings],
                'acceptanceDateTime': [f.get('accepted_at', '') for f in filings],
            }
        }
    }


@patch('src.ingestion.edgar_8k._record_provider_health')
@patch('src.ingestion.edgar_8k._upsert_edgar_8k_rows')
@patch('src.ingestion.edgar_8k._upsert_market_news_row')
@patch('src.ingestion.edgar_8k._existing_accessions_for')
@patch('src.ingestion.edgar_8k._load_ticker_to_cik')
@patch('src.ingestion.edgar_8k.EDGARClient')
def test_ingest_writes_market_news_and_per_item_rows(
    mock_client_cls, mock_load_cik, mock_existing, mock_mn_upsert,
    mock_8k_upsert, mock_provider_health,
):
    mock_load_cik.return_value = {'GLW': '0000024741'}
    mock_existing.return_value = set()  # nothing in DB yet

    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    fake_subs = _make_submissions([{
        'form': '8-K', 'accession': '0000024741-26-000123',
        'filing_date': today, 'primaryDocument': 'glw-20260528.htm',
        'accepted_at': f'{today}T11:30:00.000Z',
    }])

    async def _make_client():
        c = AsyncMock()
        c.__aenter__.return_value = c
        c.__aexit__.return_value = None
        c.get_submissions = AsyncMock(return_value=fake_subs)
        c.fetch_document = AsyncMock(return_value=(
            b'<html>Item 5.02 Departure of Directors and Officers.</html>'
        ))
        return c

    mock_client_cls.return_value = asyncio.run(_make_client())

    out = _run(ingest_8k_filings(['GLW'], lookback_hours=24))

    assert out == {'GLW': 1}
    assert mock_mn_upsert.call_count == 1
    # The market_news row should have a non-empty title with the ticker
    mn_row = mock_mn_upsert.call_args[0][0]
    assert mn_row['primary_ticker'] == 'GLW'
    assert mn_row['uuid'] == '0000024741-26-000123'
    assert 'GLW' in mn_row['title']
    assert '5.02' in mn_row['title']

    # The edgar_8k_filings upsert should have been called with one row per Item
    assert mock_8k_upsert.call_count == 1
    items_passed = mock_8k_upsert.call_args[0][0]
    assert len(items_passed) == 1
    assert items_passed[0]['item_number'] == '5.02'
    assert items_passed[0]['ticker'] == 'GLW'


@patch('src.ingestion.edgar_8k._record_provider_health')
@patch('src.ingestion.edgar_8k._upsert_edgar_8k_rows')
@patch('src.ingestion.edgar_8k._upsert_market_news_row')
@patch('src.ingestion.edgar_8k._existing_accessions_for')
@patch('src.ingestion.edgar_8k._load_ticker_to_cik')
@patch('src.ingestion.edgar_8k.EDGARClient')
def test_ingest_skips_ticker_without_cik(
    mock_client_cls, mock_load_cik, *_ignored
):
    mock_load_cik.return_value = {'GLW': '0000024741'}  # NOSUCH not in map

    async def _make_client():
        c = AsyncMock()
        c.__aenter__.return_value = c
        c.__aexit__.return_value = None
        c.get_submissions = AsyncMock(return_value=_make_submissions([]))
        c.fetch_document = AsyncMock(return_value=None)
        return c
    mock_client_cls.return_value = asyncio.run(_make_client())

    out = _run(ingest_8k_filings(['GLW', 'NOSUCH'], lookback_hours=24))
    assert 'NOSUCH' not in out  # skipped silently
    assert 'GLW' in out


@patch('src.ingestion.edgar_8k._record_provider_health')
@patch('src.ingestion.edgar_8k._upsert_edgar_8k_rows')
@patch('src.ingestion.edgar_8k._upsert_market_news_row')
@patch('src.ingestion.edgar_8k._existing_accessions_for')
@patch('src.ingestion.edgar_8k._load_ticker_to_cik')
@patch('src.ingestion.edgar_8k.EDGARClient')
def test_ingest_skips_already_ingested_accessions(
    mock_client_cls, mock_load_cik, mock_existing,
    mock_mn_upsert, mock_8k_upsert, mock_provider_health,
):
    mock_load_cik.return_value = {'GLW': '0000024741'}
    mock_existing.return_value = {'0000024741-26-000123'}  # already ingested

    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    fake_subs = _make_submissions([{
        'form': '8-K', 'accession': '0000024741-26-000123',
        'filing_date': today, 'primaryDocument': 'glw-20260528.htm',
    }])

    async def _make_client():
        c = AsyncMock()
        c.__aenter__.return_value = c
        c.__aexit__.return_value = None
        c.get_submissions = AsyncMock(return_value=fake_subs)
        c.fetch_document = AsyncMock(return_value=b'<html>Item 5.02</html>')
        return c
    mock_client_cls.return_value = asyncio.run(_make_client())

    out = _run(ingest_8k_filings(['GLW'], lookback_hours=24))

    assert out == {'GLW': 0}
    assert mock_mn_upsert.call_count == 0
    assert mock_8k_upsert.call_count == 0


@patch('src.ingestion.edgar_8k._record_provider_health')
@patch('src.ingestion.edgar_8k._upsert_edgar_8k_rows')
@patch('src.ingestion.edgar_8k._upsert_market_news_row')
@patch('src.ingestion.edgar_8k._existing_accessions_for')
@patch('src.ingestion.edgar_8k._load_ticker_to_cik')
@patch('src.ingestion.edgar_8k.EDGARClient')
def test_ingest_unparsed_filing_still_writes_both_rows(
    mock_client_cls, mock_load_cik, mock_existing,
    mock_mn_upsert, mock_8k_upsert, mock_provider_health,
):
    mock_load_cik.return_value = {'GLW': '0000024741'}
    mock_existing.return_value = set()

    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    fake_subs = _make_submissions([{
        'form': '8-K', 'accession': '0000024741-26-000999',
        'filing_date': today, 'primaryDocument': 'mystery.htm',
    }])

    async def _make_client():
        c = AsyncMock()
        c.__aenter__.return_value = c
        c.__aexit__.return_value = None
        c.get_submissions = AsyncMock(return_value=fake_subs)
        # primary doc with NO Item headers
        c.fetch_document = AsyncMock(return_value=b'<html>No items here.</html>')
        return c
    mock_client_cls.return_value = asyncio.run(_make_client())

    _run(ingest_8k_filings(['GLW'], lookback_hours=24))

    # market_news row still written
    assert mock_mn_upsert.call_count == 1
    mn = mock_mn_upsert.call_args[0][0]
    assert 'unparsed' in mn['title'].lower() or 'no items' in mn['title'].lower()

    # edgar_8k_filings gets ONE row with item_number='UNPARSED'
    items = mock_8k_upsert.call_args[0][0]
    assert len(items) == 1
    assert items[0]['item_number'] == 'UNPARSED'


def test_ingest_empty_ticker_list_returns_empty_dict():
    out = _run(ingest_8k_filings([], lookback_hours=24))
    assert out == {}
