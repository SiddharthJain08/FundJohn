# EDGAR 8-K Ingester Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an async, twice-daily SEC EDGAR 8-K ingester that closes the news-coverage gap exposed by the 2026-05-28 GLW post-mortem. Held equity positions → CIK → 8-K filings → Item-number extraction → dual-write to `market_news` (scanner-consumable) and a new `edgar_8k_filings` table (per-Item analytics).

**Architecture:** Standalone async ingester reusing the existing production-grade `EDGARClient` (rate-limited, User-Agent enforced, retry-aware), the existing CIK cache loader from the `edgar.py` backfiller, and the pre-market scanner's `load_open_equity_positions` helper. Two systemd timers (07:15 + 08:45 ET) plus a one-shot 7-day backfill script. Master gate `OPENCLAW_EDGAR_8K_INGEST` default-OFF.

**Tech Stack:** Python 3.11 + `aiohttp` (via existing `EDGARClient`) + `psycopg2` + `unittest.mock.AsyncMock` (no `pytest-asyncio` dependency — tests use `asyncio.run()` directly).

**Spec reference:** `docs/superpowers/specs/2026-05-28-edgar-8k-ingester-design.md` (commit `f42e56d`).

---

## Resolved open questions (from spec §12)

1. **EDGARClient surface:** fully async, lowest primitive is `async get(url, params=None) -> Optional[Any]` which JSON-decodes. For HTML primary docs we need a sibling `async fetch_document(url) -> Optional[bytes]` — additive, ~15 LOC. Plan adds this as Task 2 explicitly.

2. **Primary doc URL convention (verified live):**
   ```
   https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_dashes}/{primaryDocument}
   ```
   `cik_int` is the integer form (no leading zeros). `accession_no_dashes` strips hyphens from `accessionNumber` (e.g., `0000320193-26-000011` → `000032019326000011`). Verified HTTP 200 against AAPL's latest 8-K.

3. **CIK cache:** `from src.pipeline.backfillers.edgar import _load_ticker_to_cik` (async, returns `dict[str, str]` ticker→zero-padded-10-digit CIK). Reusable as-is.

4. **provider_health writes:** use `from src.maintenance.provider_health import record; record('edgar', 'submissions', success=True)` — already the project convention.

---

## File structure

| File | Status | Responsibility |
|------|--------|----------------|
| `src/database/migrations/121_edgar_8k_filings.sql` | NEW | Audit table schema. |
| `src/ingestion/edgar_items.py` | NEW | `ITEM_DESCRIPTIONS` map + `parse_items_from_document(html) -> list[str]`. Pure, no I/O. |
| `src/ingestion/edgar_client.py` | CHANGED (additive) | Add `async fetch_document(url) -> Optional[bytes]`. Existing methods untouched. |
| `src/ingestion/edgar_8k.py` | NEW | Core async ingester. ~200 LOC. `async ingest_8k_filings(tickers, lookback_hours) -> dict[str, int]`. |
| `scripts/ingest_edgar_8k.py` | NEW | CLI: `--lookback-hours`, gates, calendar guard. Calls `asyncio.run(...)`. |
| `scripts/backfill_edgar_8k.py` | NEW | CLI: `--days 7`, `--tickers <override>`. Same code path as live, wider window. |
| `docs/openclaw-edgar-8k@.service` | NEW | Templated systemd service (`%i` = lookback hours). |
| `docs/openclaw-edgar-8k-0715.timer` | NEW | systemd timer, `Mon..Fri 07:15 America/New_York`. |
| `docs/openclaw-edgar-8k-0845.timer` | NEW | systemd timer, `Mon..Fri 08:45 America/New_York`. |
| `tests/database/test_migration_121.py` | NEW | Schema contract test. |
| `tests/ingestion/test_edgar_items.py` | NEW | Item parser + descriptions map unit tests (~12). |
| `tests/ingestion/test_edgar_client_fetch_document.py` | NEW | New `fetch_document` method test with mocked aiohttp. |
| `tests/ingestion/test_edgar_8k.py` | NEW | Integration test with mocked EDGARClient + Postgres (~6). |
| `tests/ingestion/fixtures/edgar/` | NEW | 3 real-filing HTML fixtures for parser testing. |

**Non-touch files (verified):** `src/pipeline/run_premarket_scan.py`, `src/pipeline/run_sentiment_step.py`, `src/ingestion/alpaca_news.py`, `src/pipeline/backfillers/edgar.py` (we IMPORT from it; we don't modify it), every `feat/sp*` branch.

---

### Task 1: Migration 121 — `edgar_8k_filings` table

**Files:**
- Create: `src/database/migrations/121_edgar_8k_filings.sql`
- Test: `tests/database/test_migration_121.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/database/test_migration_121.py
import os
import psycopg2
import pytest

DSN = os.environ.get('POSTGRES_URI')

@pytest.mark.skipif(DSN is None, reason='POSTGRES_URI not set')
def test_migration_121_creates_table_with_expected_columns():
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
             WHERE table_name = 'edgar_8k_filings'
          ORDER BY ordinal_position
        """)
        cols = {name for (name,) in cur.fetchall()}

    expected = {
        'id', 'accession', 'cik', 'ticker', 'filing_date', 'accepted_at',
        'item_number', 'item_description', 'primary_doc_url',
        'market_news_uuid', 'fetched_at',
    }
    missing = expected - cols
    assert not missing, f'missing columns: {missing}'


@pytest.mark.skipif(DSN is None, reason='POSTGRES_URI not set')
def test_migration_121_composite_unique_constraint():
    """One row per (accession, item_number) — duplicate inserts should no-op."""
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT constraint_name FROM information_schema.table_constraints
             WHERE table_name = 'edgar_8k_filings'
               AND constraint_type = 'UNIQUE'
        """)
        constraints = [name for (name,) in cur.fetchall()]
    assert any('accession' in c for c in constraints), (
        f'expected a UNIQUE constraint involving accession; got {constraints}'
    )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
POSTGRES_URI=$DATABASE_URL pytest tests/database/test_migration_121.py -v
```
Expected: FAIL with `relation "edgar_8k_filings" does not exist`.

- [ ] **Step 3: Write the migration**

```sql
-- src/database/migrations/121_edgar_8k_filings.sql
-- EDGAR 8-K filings, one row per Item.
-- Companion to migration 120 (premarket_panic_alerts).

CREATE TABLE IF NOT EXISTS edgar_8k_filings (
    id                  BIGSERIAL PRIMARY KEY,
    accession           TEXT NOT NULL,
    cik                 TEXT NOT NULL,
    ticker              TEXT NOT NULL,
    filing_date         DATE NOT NULL,
    accepted_at         TIMESTAMPTZ,
    item_number         TEXT NOT NULL,
    item_description    TEXT NOT NULL,
    primary_doc_url     TEXT,
    market_news_uuid    TEXT,
    fetched_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (accession, item_number)
);

CREATE INDEX IF NOT EXISTS edgar_8k_filings_ticker_date
    ON edgar_8k_filings(ticker, filing_date DESC);
CREATE INDEX IF NOT EXISTS edgar_8k_filings_item_number
    ON edgar_8k_filings(item_number, filing_date DESC);
CREATE INDEX IF NOT EXISTS edgar_8k_filings_accession
    ON edgar_8k_filings(accession);
```

- [ ] **Step 4: Apply the migration**

Try the project's standard runner first; if it errors on pre-existing aborted-transaction state, apply directly via psycopg2:

```bash
psql "$POSTGRES_URI" -f src/database/migrations/121_edgar_8k_filings.sql
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
POSTGRES_URI=$DATABASE_URL pytest tests/database/test_migration_121.py -v
```
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add src/database/migrations/121_edgar_8k_filings.sql tests/database/test_migration_121.py
git commit -m "feat(edgar-8k): migration 121 — edgar_8k_filings table"
```

---

### Task 2: Add `EDGARClient.fetch_document` (additive)

**Files:**
- Modify: `src/ingestion/edgar_client.py` (additive: new async method; existing `get`, `get_submissions`, `get_company_facts`, `get_filing_index` UNCHANGED)
- Test: `tests/ingestion/test_edgar_client_fetch_document.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/ingestion/test_edgar_client_fetch_document.py
import asyncio
from unittest.mock import patch, AsyncMock, MagicMock
import pytest

from src.ingestion.edgar_client import EDGARClient


def _run(coro):
    return asyncio.run(coro)


def test_fetch_document_returns_bytes_on_200():
    async def go():
        async with EDGARClient() as c:
            with patch.object(c, '_session') as fake_session:
                resp = MagicMock()
                resp.status = 200
                resp.read = AsyncMock(return_value=b'<html>...8-K body...</html>')
                fake_session.get = MagicMock(
                    return_value=_async_cm(resp)
                )
                out = await c.fetch_document(
                    'https://www.sec.gov/Archives/edgar/data/320193/000032019326000011/aapl-20260430.htm'
                )
                assert out == b'<html>...8-K body...</html>'
    _run(go())


def test_fetch_document_returns_none_on_404():
    async def go():
        async with EDGARClient() as c:
            with patch.object(c, '_session') as fake_session:
                resp = MagicMock()
                resp.status = 404
                resp.read = AsyncMock(return_value=b'')
                fake_session.get = MagicMock(return_value=_async_cm(resp))
                out = await c.fetch_document('https://www.sec.gov/Archives/edgar/data/X/Y/Z.htm')
                assert out is None
    _run(go())


def test_fetch_document_uses_user_agent_header():
    async def go():
        async with EDGARClient() as c:
            with patch.object(c, '_session') as fake_session:
                resp = MagicMock()
                resp.status = 200
                resp.read = AsyncMock(return_value=b'ok')
                fake_session.get = MagicMock(return_value=_async_cm(resp))
                await c.fetch_document('https://example.invalid/x.htm')
                # session.get must have been called with our URL
                args, kwargs = fake_session.get.call_args
                assert args[0] == 'https://example.invalid/x.htm'
    _run(go())


def _async_cm(resp):
    """Build an object that works as `async with` returning resp."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /root/openclaw && pytest tests/ingestion/test_edgar_client_fetch_document.py -v
```
Expected: FAIL with `AttributeError: 'EDGARClient' object has no attribute 'fetch_document'`.

- [ ] **Step 3: Add the method**

Read `src/ingestion/edgar_client.py` first to confirm the `_session` attribute name and the existing `get()` shape. Then append the method INSIDE the `EDGARClient` class (before the closing `__aexit__` if there is one at the bottom, otherwise just before the class ends):

```python
# Add to EDGARClient class in src/ingestion/edgar_client.py

    async def fetch_document(self, url: str) -> Optional[bytes]:
        """Fetch an arbitrary URL and return raw bytes (not JSON-decoded).

        Used for HTML/text primary documents like 8-K filings, which the
        JSON-decoding `get()` cannot handle. Applies the same User-Agent,
        rate-limiting, and exponential backoff as `get()`. Returns None
        on non-200 or after exhausting retries.
        """
        await self._throttle()  # same rate limiter as get()
        for delay in [0.0] + RETRY_DELAYS:
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                async with self._session.get(url) as resp:
                    if resp.status == 200:
                        return await resp.read()
                    if resp.status in (429,) or 500 <= resp.status < 600:
                        # retry on transient failures
                        continue
                    # 4xx other than 429 — don't retry
                    return None
            except (aiohttp.ClientError, asyncio.TimeoutError):
                continue
        return None
```

Make sure `Optional` is imported (`from typing import Optional`) and `asyncio` and `aiohttp` are imported at the top of the file — they almost certainly are already, since `get()` uses them. If not, add them.

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /root/openclaw && pytest tests/ingestion/test_edgar_client_fetch_document.py -v
```
Expected: 3 passed.

- [ ] **Step 5: Smoke test against the real SEC server**

```bash
cd /root/openclaw && python3 -c "
import asyncio
from src.ingestion.edgar_client import EDGARClient

async def main():
    async with EDGARClient() as c:
        body = await c.fetch_document(
            'https://www.sec.gov/Archives/edgar/data/320193/000032019326000011/aapl-20260430.htm'
        )
        print('OK' if body and b'8-K' in body[:5000] else 'EMPTY OR WRONG')
        print(f'bytes: {len(body) if body else 0}')

asyncio.run(main())
"
```
Expected: `OK` and a few-thousand byte count.

- [ ] **Step 6: Commit**

```bash
git add src/ingestion/edgar_client.py tests/ingestion/test_edgar_client_fetch_document.py
git commit -m "feat(edgar-8k): EDGARClient.fetch_document for raw HTML primary docs"
```

---

### Task 3: `edgar_items.py` — descriptions map + parser

**Files:**
- Create: `src/ingestion/edgar_items.py`
- Create: `tests/ingestion/fixtures/edgar/sample_8k_5_02.html`
- Create: `tests/ingestion/fixtures/edgar/sample_8k_9_01_only.html`
- Create: `tests/ingestion/fixtures/edgar/sample_8k_multi.html`
- Test: `tests/ingestion/test_edgar_items.py`

- [ ] **Step 1: Create the fixture HTML files**

Three synthetic but realistic 8-K HTML snippets. Place them in `tests/ingestion/fixtures/edgar/`:

`sample_8k_5_02.html`:
```html
<html><body>
<h1>Form 8-K</h1>
<p><b>Item 5.02</b> Departure of Directors or Certain Officers; Election of Directors;
Appointment of Officers; Compensatory Arrangements of Certain Officers.</p>
<p>On May 27, 2026, the Company announced the resignation of its Chief Financial Officer.</p>
</body></html>
```

`sample_8k_9_01_only.html`:
```html
<html><body>
<h1>Form 8-K</h1>
<p>ITEM 9.01 Financial Statements and Exhibits.</p>
<p>(d) Exhibits.</p>
</body></html>
```

`sample_8k_multi.html`:
```html
<html><body>
<h1>Form 8-K</h1>
<table>
  <tr><td>Item 2.02</td><td>Results of Operations and Financial Condition</td></tr>
  <tr><td>Item 9.01</td><td>Financial Statements and Exhibits</td></tr>
</table>
<p>Item 2.02 Results of Operations and Financial Condition.</p>
<p>On May 27, 2026, the Company issued a press release...</p>
<p>Item 9.01 Financial Statements and Exhibits.</p>
</body></html>
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/ingestion/test_edgar_items.py
from pathlib import Path
import pytest

from src.ingestion.edgar_items import (
    ITEM_DESCRIPTIONS,
    parse_items_from_document,
)


FIXTURE_DIR = Path(__file__).parent / 'fixtures' / 'edgar'


def _load(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


def test_descriptions_map_has_28_items():
    """Pin the count so accidental deletions trigger CI failure."""
    assert len(ITEM_DESCRIPTIONS) == 28


def test_descriptions_map_has_core_items():
    for k in ('1.01', '2.02', '4.02', '5.02', '7.01', '8.01', '9.01'):
        assert k in ITEM_DESCRIPTIONS
        assert ITEM_DESCRIPTIONS[k]  # non-empty


def test_parse_well_formed_5_02_header():
    items = parse_items_from_document(_load('sample_8k_5_02.html'))
    assert items == ['5.02']


def test_parse_uppercase_item_header():
    items = parse_items_from_document(_load('sample_8k_9_01_only.html'))
    assert items == ['9.01']


def test_parse_multi_item_filing_dedupes_and_preserves_order():
    items = parse_items_from_document(_load('sample_8k_multi.html'))
    assert items == ['2.02', '9.01']


def test_parse_empty_input_returns_empty_list():
    assert parse_items_from_document(b'') == []


def test_parse_no_items_returns_empty_list():
    html = b'<html><body><p>Some 8-K body with no Item headers at all.</p></body></html>'
    assert parse_items_from_document(html) == []


def test_parse_unknown_item_number_filtered():
    html = b'<html><body><p>Item 99.99 Made-Up Section</p></body></html>'
    # 99.99 is not in ITEM_DESCRIPTIONS -> filtered
    assert parse_items_from_document(html) == []


def test_parse_handles_non_utf8_bytes():
    """Defensive: bytes with invalid UTF-8 should not raise."""
    html = b'\xff\xfe<html>Item 5.02 something</html>'
    items = parse_items_from_document(html)
    # We accept either '5.02' (decoded with errors='replace') or [] (failed decode)
    # but it MUST NOT raise.
    assert items == ['5.02'] or items == []


def test_parse_string_input_also_works():
    """Accept str or bytes (caller convenience)."""
    items = parse_items_from_document('Item 5.02 Departure of Directors')
    assert items == ['5.02']


def test_parse_tag_stripping():
    html = b'<html><b>Item 5.02</b> Departure of Officers</html>'
    assert parse_items_from_document(html) == ['5.02']


def test_parse_case_insensitive():
    assert parse_items_from_document(b'item 5.02 Departure') == ['5.02']
    assert parse_items_from_document(b'ITEM 5.02 Departure') == ['5.02']
    assert parse_items_from_document(b'Item 5.02 Departure') == ['5.02']
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd /root/openclaw && pytest tests/ingestion/test_edgar_items.py -v
```
Expected: ALL FAIL with `ModuleNotFoundError`.

- [ ] **Step 4: Write the implementation**

```python
# src/ingestion/edgar_items.py
"""SEC 8-K Item number extraction + canned descriptions.

Pure module, no I/O. Reads `bytes` (or `str`) HTML and extracts
the list of declared 8-K Items via regex.
"""
from __future__ import annotations

import re
from typing import Union


ITEM_DESCRIPTIONS: dict[str, str] = {
    # Section 1 — Registrant's Business and Operations
    '1.01': 'Entry into a Material Definitive Agreement',
    '1.02': 'Termination of a Material Definitive Agreement',
    '1.03': 'Bankruptcy or Receivership',
    '1.04': 'Mine Safety — Reporting of Shutdowns and Patterns of Violations',
    '1.05': 'Material Cybersecurity Incidents',
    # Section 2 — Financial Information
    '2.01': 'Completion of Acquisition or Disposition of Assets',
    '2.02': 'Results of Operations and Financial Condition',
    '2.03': 'Creation of a Direct Financial Obligation',
    '2.04': 'Triggering Events That Accelerate or Increase a Direct Financial Obligation',
    '2.05': 'Costs Associated with Exit or Disposal Activities',
    '2.06': 'Material Impairments',
    # Section 3 — Securities and Trading Markets
    '3.01': 'Notice of Delisting or Failure to Satisfy a Continued Listing Rule',
    '3.02': 'Unregistered Sales of Equity Securities',
    '3.03': 'Material Modification to Rights of Security Holders',
    # Section 4 — Matters Related to Accountants and Financial Statements
    '4.01': "Changes in Registrant's Certifying Accountant",
    '4.02': 'Non-Reliance on Previously Issued Financial Statements',
    # Section 5 — Corporate Governance and Management
    '5.01': 'Changes in Control of Registrant',
    '5.02': ('Departure of Directors or Certain Officers; Election of Directors;'
             ' Appointment of Officers'),
    '5.03': 'Amendments to Articles of Incorporation or Bylaws',
    '5.04': "Temporary Suspension of Trading Under Registrant's Employee Benefit Plans",
    '5.05': "Amendments to the Registrant's Code of Ethics",
    '5.06': 'Change in Shell Company Status',
    '5.07': 'Submission of Matters to a Vote of Security Holders',
    '5.08': 'Shareholder Director Nominations',
    # Section 7 — Regulation FD
    '7.01': 'Regulation FD Disclosure',
    # Section 8 — Other Events
    '8.01': 'Other Events',
    # Section 9 — Financial Statements and Exhibits
    '9.01': 'Financial Statements and Exhibits',
}

UNPARSED_PLACEHOLDER = 'UNPARSED'
UNPARSED_DESCRIPTION = 'Item extraction failed'

_TAG_RE = re.compile(r'<[^>]+>')
_ITEM_RE = re.compile(r'(?:^|\s)ITEM\s+(\d+\.\d+)', re.IGNORECASE)


def parse_items_from_document(html: Union[str, bytes]) -> list[str]:
    """Extract 8-K Item numbers from a primary document.

    Filters against ITEM_DESCRIPTIONS so unknown numbers (e.g., from
    accidental matches in narrative text) are dropped.

    Returns deduped, ordered list. Empty list when no recognized
    Items are found OR input fails to decode (defensive).
    """
    if not html:
        return []

    if isinstance(html, bytes):
        try:
            text = html.decode('utf-8', errors='replace')
        except (UnicodeDecodeError, AttributeError):
            return []
    else:
        text = html

    cleaned = _TAG_RE.sub(' ', text)
    raw = _ITEM_RE.findall(cleaned)

    seen: set[str] = set()
    out: list[str] = []
    for n in raw:
        if n in seen:
            continue
        if n not in ITEM_DESCRIPTIONS:
            continue
        seen.add(n)
        out.append(n)
    return out
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd /root/openclaw && pytest tests/ingestion/test_edgar_items.py -v
```
Expected: 12 passed.

- [ ] **Step 6: Commit**

```bash
git add src/ingestion/edgar_items.py tests/ingestion/test_edgar_items.py \
        tests/ingestion/fixtures/edgar/
git commit -m "feat(edgar-8k): edgar_items — ITEM_DESCRIPTIONS map + parser"
```

---

### Task 4: `edgar_8k.py` — core async ingester

**Files:**
- Create: `src/ingestion/edgar_8k.py`
- Test: `tests/ingestion/test_edgar_8k.py`

This is the biggest task. The ingester:
1. Takes `tickers: list[str]` and `lookback_hours: int`.
2. Maps tickers to CIKs via the cached loader.
3. For each ticker, calls `EDGARClient.get_submissions(cik)`, filters to 8-Ks in window.
4. For each new filing, fetches the primary document and parses Items.
5. Composes a `market_news` row and the per-Item `edgar_8k_filings` rows.
6. Upserts both tables.
7. Returns a `dict[str, int]` of `{ticker: new_filings_count}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/ingestion/test_edgar_8k.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /root/openclaw && pytest tests/ingestion/test_edgar_8k.py -v
```
Expected: ALL FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

```python
# src/ingestion/edgar_8k.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /root/openclaw && pytest tests/ingestion/test_edgar_8k.py -v
```
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add src/ingestion/edgar_8k.py tests/ingestion/test_edgar_8k.py
git commit -m "feat(edgar-8k): async ingester with dual-write to market_news + edgar_8k_filings"
```

---

### Task 5: CLI entry point `scripts/ingest_edgar_8k.py`

**Files:**
- Create: `scripts/ingest_edgar_8k.py`
- (No separate test file — covered by integration test in Task 4 + the CLI is a thin wrapper.)

- [ ] **Step 1: Write the script**

```python
#!/usr/bin/env python3
"""SEC EDGAR 8-K ingester CLI entry point.

Reads currently-held equity positions, calls the ingester, exits 0
on success. Master gate OPENCLAW_EDGAR_8K_INGEST=1; otherwise no-op.

Usage:
    python3 -m scripts.ingest_edgar_8k --lookback-hours 24
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from src.ingestion.edgar_8k import ingest_8k_filings
from src.pipeline.premarket_helpers import (
    is_trading_day_in_et,
    load_open_equity_positions,
)


log = logging.getLogger(__name__)


def _main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument('--lookback-hours', type=int, default=24)
    parser.add_argument('--tickers', nargs='*',
                        help='override held-position lookup (debugging only)')
    args = parser.parse_args(argv)

    if os.environ.get('OPENCLAW_EDGAR_8K_INGEST', '0') != '1':
        log.info('OPENCLAW_EDGAR_8K_INGEST=0; exiting silently')
        return 0

    if not is_trading_day_in_et():
        log.info('not a trading day in ET; exiting silently')
        return 0

    if args.tickers:
        tickers = args.tickers
    else:
        positions = load_open_equity_positions()
        tickers = [p['symbol'] for p in positions]

    if not tickers:
        log.info('no tickers to ingest; exiting')
        return 0

    max_n = int(os.environ.get('OPENCLAW_EDGAR_8K_MAX_TICKERS_PER_RUN', '50'))
    if len(tickers) > max_n:
        log.warning('truncating %d tickers to max %d', len(tickers), max_n)
        tickers = tickers[:max_n]

    results = asyncio.run(ingest_8k_filings(tickers, args.lookback_hours))
    total_new = sum(results.values())
    log.info('ingested 8-Ks: total new=%d, per-ticker=%s', total_new, results)
    return 0


if __name__ == '__main__':
    sys.exit(_main(sys.argv[1:]))
```

- [ ] **Step 2: Smoke test the gate hierarchy from a shell**

```bash
cd /root/openclaw
# Master gate OFF (default) — exits silently
python3 -m scripts.ingest_edgar_8k --lookback-hours 24 2>&1 | grep "exiting silently"
echo "exit code: $?"
```
Expected: matches the silent-exit message, exit code 0.

- [ ] **Step 3: Smoke test with the gate on, tickers override, against the real SEC API**

```bash
cd /root/openclaw
OPENCLAW_EDGAR_8K_INGEST=1 python3 -m scripts.ingest_edgar_8k \
    --lookback-hours 168 \
    --tickers AAPL MSFT
```
Expected: logs a `total new=N` line, exit code 0. Verify rows landed:

```bash
psql "$POSTGRES_URI" -c "SELECT ticker, COUNT(*) FROM edgar_8k_filings WHERE ticker IN ('AAPL','MSFT') GROUP BY ticker"
psql "$POSTGRES_URI" -c "SELECT primary_ticker, title FROM market_news WHERE primary_ticker IN ('AAPL','MSFT') AND publisher='SEC EDGAR' ORDER BY published_at DESC LIMIT 5"
```

- [ ] **Step 4: Commit**

```bash
git add scripts/ingest_edgar_8k.py
git commit -m "feat(edgar-8k): CLI entry point with master gate + calendar guard"
```

---

### Task 6: Backfill script `scripts/backfill_edgar_8k.py`

**Files:**
- Create: `scripts/backfill_edgar_8k.py`

The backfill is the same code path as live ingestion but with a wider lookback (7 days default). It does NOT honor the master gate — it's operator-invoked.

- [ ] **Step 1: Write the script**

```python
#!/usr/bin/env python3
"""One-shot 7-day EDGAR 8-K backfill.

Operator-invoked. Does NOT honor OPENCLAW_EDGAR_8K_INGEST (the operator
is explicitly running the script; that's authorization enough).

Usage:
    python3 -m scripts.backfill_edgar_8k --days 7
    python3 -m scripts.backfill_edgar_8k --days 30 --tickers GLW AAPL MSFT
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from src.ingestion.edgar_8k import ingest_8k_filings
from src.pipeline.premarket_helpers import load_open_equity_positions


log = logging.getLogger(__name__)


def _main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=int, default=7)
    parser.add_argument('--tickers', nargs='*',
                        help='override held-position lookup; mostly for backfilling '
                             'a known target like GLW for the post-mortem')
    args = parser.parse_args(argv)

    if args.tickers:
        tickers = args.tickers
    else:
        positions = load_open_equity_positions()
        tickers = [p['symbol'] for p in positions]

    if not tickers:
        log.warning('no tickers — nothing to backfill')
        return 0

    log.info('backfilling %d tickers, lookback=%dd: %s',
             len(tickers), args.days, tickers)
    results = asyncio.run(ingest_8k_filings(tickers, args.days * 24))
    total_new = sum(results.values())
    log.info('backfill complete: total new=%d, per-ticker=%s', total_new, results)
    return 0


if __name__ == '__main__':
    sys.exit(_main(sys.argv[1:]))
```

- [ ] **Step 2: Smoke test**

```bash
cd /root/openclaw
python3 -m scripts.backfill_edgar_8k --days 7 --tickers AAPL
psql "$POSTGRES_URI" -c "SELECT ticker, COUNT(*) FROM edgar_8k_filings WHERE ticker = 'AAPL' GROUP BY ticker"
```
Expected: at least 1 row written (Apple files frequently); script exits 0.

- [ ] **Step 3: Commit**

```bash
git add scripts/backfill_edgar_8k.py
git commit -m "feat(edgar-8k): one-shot backfill script"
```

---

### Task 7: systemd units

**Files:**
- Create: `docs/openclaw-edgar-8k@.service`
- Create: `docs/openclaw-edgar-8k-0715.timer`
- Create: `docs/openclaw-edgar-8k-0845.timer`

Mirror the templated-service pattern from the panic scanner.

- [ ] **Step 1: Write the service**

```ini
# docs/openclaw-edgar-8k@.service
[Unit]
Description=OpenClaw EDGAR 8-K ingester (lookback=%i hours)
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=oneshot
User=claudebot
Group=claudebot
WorkingDirectory=/root/openclaw
EnvironmentFile=/root/openclaw/.env
ExecStart=/usr/bin/python3 -m scripts.ingest_edgar_8k --lookback-hours %i
StandardOutput=journal
StandardError=journal
```

- [ ] **Step 2: Write the timers**

```ini
# docs/openclaw-edgar-8k-0715.timer
[Unit]
Description=Fire EDGAR 8-K ingester at 07:15 ET
Requires=openclaw-edgar-8k@24.service

[Timer]
OnCalendar=Mon..Fri *-*-* 07:15:00 America/New_York
Persistent=false
Unit=openclaw-edgar-8k@24.service

[Install]
WantedBy=timers.target
```

```ini
# docs/openclaw-edgar-8k-0845.timer
[Unit]
Description=Fire EDGAR 8-K ingester at 08:45 ET
Requires=openclaw-edgar-8k@24.service

[Timer]
OnCalendar=Mon..Fri *-*-* 08:45:00 America/New_York
Persistent=false
Unit=openclaw-edgar-8k@24.service

[Install]
WantedBy=timers.target
```

- [ ] **Step 3: Validate**

```bash
cd /root/openclaw
systemd-analyze verify \
    docs/openclaw-edgar-8k@.service \
    docs/openclaw-edgar-8k-0715.timer \
    docs/openclaw-edgar-8k-0845.timer 2>&1 | head -20
```
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add docs/openclaw-edgar-8k@.service \
        docs/openclaw-edgar-8k-0715.timer \
        docs/openclaw-edgar-8k-0845.timer
git commit -m "feat(edgar-8k): systemd units (templated service + 07:15/08:45 ET timers)"
```

---

### Task 8: GLW post-mortem re-run (analytical payoff)

**Files:**
- Modify: `docs/superpowers/specs/2026-05-28-premarket-panic-scan-design.md` (append a new sub-section to §11 — do not alter the rest)

This is the validation step the whole spec exists for. We've populated `market_news` with 8-K data; now we re-run the GLW replay and see if it catches the 2026-05-28 GLW drop.

- [ ] **Step 1: Backfill GLW specifically**

```bash
cd /root/openclaw
python3 -m scripts.backfill_edgar_8k --days 7 --tickers GLW
```

- [ ] **Step 2: Check what landed**

```bash
psql "$POSTGRES_URI" -c "
SELECT filing_date, item_number, item_description
  FROM edgar_8k_filings
 WHERE ticker = 'GLW'
   AND filing_date >= '2026-05-21'
 ORDER BY filing_date DESC
"
psql "$POSTGRES_URI" -c "
SELECT published_at, title
  FROM market_news
 WHERE primary_ticker = 'GLW' AND publisher = 'SEC EDGAR'
   AND published_at >= '2026-05-27T00:00:00Z'
 ORDER BY published_at DESC
"
```

Note what 8-Ks GLW filed in that window, if any. If GLW filed an 8-K with bearish Items (5.02, 4.02, 2.05, 2.06, 1.02), the scanner SHOULD catch it. If GLW filed no 8-Ks, the drop had a non-EDGAR cause and the next spec is tape data.

- [ ] **Step 3: Re-run the replay**

```bash
cd /root/openclaw
python3 -m scripts.replay_premarket_panic --ticker GLW \
    --as-of 2026-05-28T07:30:00-04:00 --with-sonnet \
  > /tmp/glw_replay_0730_after_edgar.json 2>&1
cat /tmp/glw_replay_0730_after_edgar.json

python3 -m scripts.replay_premarket_panic --ticker GLW \
    --as-of 2026-05-28T09:00:00-04:00 --with-sonnet \
  > /tmp/glw_replay_0900_after_edgar.json 2>&1
cat /tmp/glw_replay_0900_after_edgar.json
```

If `--with-sonnet` fails (budget / network), drop it and rerun without.

- [ ] **Step 4: Append a "Post-EDGAR-integration result" sub-section to the panic-scanner spec**

Append (do NOT touch sections 1-10 or the existing GLW post-mortem text):

```markdown
### 11.1. Post-EDGAR-integration replay (2026-05-28)

After shipping the EDGAR 8-K ingester (`2026-05-28-edgar-8k-ingester-design.md`),
re-ran the GLW replay against the now-populated market_news. EDGAR backfill
window: 7 days.

**EDGAR backfill found for GLW (filing_date ≥ 2026-05-21):**
- <fill in: filing_date | item_number | item_description rows, or "no 8-Ks in window">

**07:30 ET replay (post-EDGAR):**
- news_count: <fill in>
- panic_score: <fill in>
- advisory_would_fire: <yes/no>
- Sonnet verdict: <fill in>

**09:00 ET replay (post-EDGAR):**
- news_count: <fill in>
- panic_score: <fill in>
- advisory_would_fire: <yes/no>
- Sonnet verdict: <fill in>

**Conclusion:**
<1-2 paragraphs: did EDGAR close the gap? If GLW filed a material 8-K and
the scanner now fires advisory_would_fire=true with a bearish_* verdict,
the integrated system works end-to-end. If GLW filed no 8-K, the price
move had a non-EDGAR cause and the next follow-up is the pre-market tape
spec.>
```

Fill in the actual values from the JSON files.

- [ ] **Step 5: Stage the replay JSONs as runs/ artifacts**

```bash
mkdir -p /root/openclaw/docs/superpowers/runs/
cp /tmp/glw_replay_0730_after_edgar.json /root/openclaw/docs/superpowers/runs/2026-05-28-glw-replay-0730-post-edgar.json
cp /tmp/glw_replay_0900_after_edgar.json /root/openclaw/docs/superpowers/runs/2026-05-28-glw-replay-0900-post-edgar.json
```

- [ ] **Step 6: Commit**

```bash
git add docs/superpowers/specs/2026-05-28-premarket-panic-scan-design.md
git add /root/openclaw/docs/superpowers/runs/2026-05-28-glw-replay-0730-post-edgar.json
git add /root/openclaw/docs/superpowers/runs/2026-05-28-glw-replay-0900-post-edgar.json
git commit -m "docs(edgar-8k): GLW post-mortem replay re-run with EDGAR data"
```

---

## Self-review

**Spec coverage:**
- §1 goals → Tasks 1-7 cover the ingester; Task 8 covers the GLW re-run validation goal.
- §2 architecture → Tasks 4 (core ingester), 5 (CLI), 7 (systemd) match the spec's data-flow diagram.
- §3 components inventory → every file in the spec has a task (or is explicitly UNCHANGED).
- §4 schema → Task 1.
- §5 market_news row contract → Task 4 (`_compose_title`, `_compose_summary`, `_upsert_market_news_row`).
- §6 Item extraction → Tasks 2 (`fetch_document`) + 3 (`edgar_items`).
- §7 env-var gates → Task 5 (`OPENCLAW_EDGAR_8K_INGEST`, `_MAX_TICKERS_PER_RUN`).
- §8 error handling → Task 4 (`_process_ticker` per-ticker try/except, provider_health on failure, parse-failure UNPARSED row).
- §9 testing strategy → covered across Tasks 1-4.
- §10 coverage monitoring → Task 4 (`_record_provider_health` on every run). The optional `edgar_8k_freshness` doctor check is deliberately deferred per spec §10 ("acceptable to defer").
- §11 rollout → Task 8 is the operator-driven rollout finale; systemd installation is operator's manual step after merge.
- §12 open questions → all three resolved at the top of this plan.

**Placeholder scan:** Task 8 has `<fill in: ...>` placeholders for the post-mortem section — these are intentional (operator fills from JSON output), like the panic-scanner plan's Task 11. No other placeholders.

**Type consistency:**
- `ingest_8k_filings(tickers, lookback_hours) -> dict[str, int]` — stable across Tasks 4, 5, 6.
- `parse_items_from_document(html) -> list[str]` — stable Tasks 3, 4.
- `_compose_title(items: list[str], ticker: str) -> str` — Task 4.
- `EDGARClient.fetch_document(url) -> Optional[bytes]` — Task 2, used in Task 4.
- `_load_ticker_to_cik() -> dict[str, str]` (async) — imported into Task 4.
- `load_open_equity_positions() -> list[dict]` with `symbol` key — already shipped, used in Tasks 5, 6.
- `is_trading_day_in_et() -> bool` — already shipped, used in Task 5.

**Verification reminders for the implementer:**
- Task 2: verify `EDGARClient`'s internals use `self._session` and `aiohttp` is imported at module top before adding `fetch_document`.
- Task 4: the test fixtures use the LIVE date format `YYYY-MM-DD` for `filing_date`. Confirm against a real submissions response before pinning.
- Task 5/6: confirm `is_trading_day_in_et` works the same way it did in the panic scanner deployment (timestamp-from-broker primary, wall-clock fallback).
