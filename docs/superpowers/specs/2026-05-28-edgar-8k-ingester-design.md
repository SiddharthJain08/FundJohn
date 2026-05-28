# EDGAR 8-K Ingester — Design

**Status:** spec approved, plan pending
**Date:** 2026-05-28
**Motivating incident:** The pre-market sentiment panic scanner shipped earlier
today (see `2026-05-28-premarket-panic-scan-design.md`) could not have caught
the 2026-05-28 GLW open-drop because `market_news` had zero GLW rows in the
pre-market window. 8-K filings (officer departures, guidance cuts, non-reliance,
M&A) bypass Alpaca News almost entirely. This spec adds an ingester that pulls
8-Ks for currently-held positions from SEC EDGAR into the same `market_news`
table the scanner reads, plus a new structured table for per-Item analytics.

---

## 1. Goals & non-goals

### Goals

- Close the news-ingestion coverage gap that prevented the panic scanner from
  catching the GLW-class incident.
- Surface SEC 8-K filings for currently-held equity positions into
  `market_news` so the panic scanner picks them up with zero scanner-code
  change.
- Persist structured per-Item data into a new `edgar_8k_filings` table so the
  analytics path can query "all officer-departure filings in the last 30 days
  across the portfolio" without reparsing.
- Reuse the existing production-grade `src/ingestion/edgar_client.py`
  (rate-limited, User-Agent enforced, retry-aware) — no new HTTP client code.
- Reuse the existing CIK cache built by `src/pipeline/backfillers/edgar.py`.
- Provide a one-shot 7-day backfill so the GLW post-mortem can be re-run
  against real EDGAR data.
- Do not touch the panic scanner, the daily LangGraph cycle, the sentiment
  step, the `alpaca_news` ingester, the existing `edgar.py` backfiller, or any
  active `feat/sp[1-5]-*` branch.

### Non-goals

- Full Item-text parsing (Level C from brainstorming). Defer until Item-name
  FinBERT scoring is measured to be insufficient.
- Other forms (10-K, 10-Q, S-1, Form 4). The existing backfiller already pulls
  these into `filings.parquet`; if any need to flow into `market_news`, ship
  as a separate spec.
- Real-time SEC RSS push subscriptions. Twice-daily fires with a 24-hour
  lookback are enough for the panic-scanner use case.
- Cross-ticker M&A 8-Ks as joined rows. Each named ticker that we hold gets
  its own `market_news` row referencing the same accession.
- Backfill of `edgar_8k_filings` beyond the activation window. Forward-only
  after the initial 7-day import.

---

## 2. Architecture summary

A standalone ingester runs twice per trading morning (07:15 ET and 08:45 ET)
under its own systemd timers. It loads currently-held equity positions, maps
each ticker to its SEC CIK, asks the existing `EDGARClient` for 8-K filings in
the last 24 hours, fetches each new filing's primary document to extract the
Item-number list (e.g., `Item 5.02`, `Item 9.01`), composes a meaningful
headline using a hardcoded Item-description map, and dual-writes one row to
`market_news` plus one row per Item to a new `edgar_8k_filings` table.

The 7-day backfill is a separate one-shot operator-invoked script that reuses
the same code path with a wider window. The ingester is a sidecar — nothing
in the pre-market scanner, the daily cycle, the sentiment step, or any other
consumer is modified.

### Data flow

```
07:15 ET timer fires (and again at 08:45 ET)
  │
  └── scripts/ingest_edgar_8k.py --lookback-hours 24
        ├── load_open_equity_positions()      # existing helper (panic scanner)
        ├── _load_ticker_to_cik()             # existing helper (edgar.py)
        ├── For each ticker:
        │     ├── EDGARClient.fetch_filings(cik, form_types=['8-K'])
        │     ├── Filter to filings since now - lookback_hours
        │     ├── For each new filing (not yet in edgar_8k_filings):
        │     │     ├── Fetch primary document HTML via EDGARClient
        │     │     ├── parse_items_from_document(html)  # regex
        │     │     ├── Look up Item descriptions
        │     │     ├── INSERT into market_news (one row, uuid=accession)
        │     │     └── INSERT INTO edgar_8k_filings (one row per Item;
        │     │            ON CONFLICT (accession, item_number) DO NOTHING)
        │     └── Log per-ticker outcomes
        └── Record run to provider_health (edgar entry)

One-shot backfill (operator-invoked):
  scripts/backfill_edgar_8k.py --days 7
    → same code path, lookback = 7 * 24h
```

---

## 3. Components inventory

| File | Status | Responsibility |
|------|--------|----------------|
| `src/ingestion/edgar_8k.py` | NEW | Core ingester. ~200 LOC. Exposes `ingest_8k_filings(tickers, lookback_hours) -> dict[str, int]`. Pure function on top of `EDGARClient`. |
| `src/ingestion/edgar_items.py` | NEW | The `ITEM_DESCRIPTIONS` map (8-K Item number → canned label) and `parse_items_from_document(html) -> list[str]`. Isolated for unit testing. |
| `scripts/ingest_edgar_8k.py` | NEW | CLI entry point. Wraps the ingester with the held-positions loader, the master gate, and the calendar guard. |
| `scripts/backfill_edgar_8k.py` | NEW | One-shot backfill. CLI: `--days 7` default, `--tickers <override>`. Same code path as live, wider window. |
| `src/database/migrations/121_edgar_8k_filings.sql` | NEW | Schema in §4. |
| `src/ingestion/edgar_client.py` | UNCHANGED | Already production-grade; just gets a new caller. |
| `src/pipeline/backfillers/edgar.py` | UNCHANGED | `_load_ticker_to_cik` reused as-is via sibling-module import. |
| `src/pipeline/premarket_helpers.py` | UNCHANGED | `load_open_equity_positions()` and `is_trading_day_in_et()` reused. |
| `docs/openclaw-edgar-8k@.service` | NEW | Templated systemd service; `%i` = lookback hours. |
| `docs/openclaw-edgar-8k-0715.timer` | NEW | systemd timer, `Mon..Fri 07:15 America/New_York`. |
| `docs/openclaw-edgar-8k-0845.timer` | NEW | systemd timer, `Mon..Fri 08:45 America/New_York`. |
| `tests/ingestion/test_edgar_items.py` | NEW | Unit tests for the Item parser + descriptions map. |
| `tests/ingestion/test_edgar_8k.py` | NEW | Integration test with mocked `EDGARClient` + mocked Postgres. |
| `tests/database/test_migration_121.py` | NEW | Migration schema contract test. |

**Explicit non-touches:** `pipeline_orchestrator.py`, `src/agent/graphs/daily-cycle.js`, `src/pipeline/run_premarket_scan.py`, `src/pipeline/run_sentiment_step.py`, `src/ingestion/alpaca_news.py`, `src/pipeline/backfillers/edgar.py`, every `feat/sp[1-5]-*` branch.

---

## 4. Database schema — Migration 121

```sql
-- 121_edgar_8k_filings.sql
-- EDGAR 8-K filings, one row per Item.

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

Schema notes:

- **One row per (accession, item_number).** A typical 8-K with Items 5.02 +
  9.01 produces 2 rows; one with no Items produces 1 row marked
  `item_number='UNPARSED'`. Composite UNIQUE makes the writer's
  `ON CONFLICT DO NOTHING` idempotent across re-runs.
- **`item_number='UNPARSED'` is intentional**, not a bug. When the HTML parser
  cannot extract Items (rare filing-format edge case), we still record that an
  8-K was filed — the panic scanner still gets news-volume signal, and the row
  is queryable as "8-Ks we could not classify" for monitoring.
- **`market_news_uuid` is a soft pointer back to `market_news.uuid`** (no FK
  because `market_news.uuid` is TEXT and may not always be UUID-formatted).
- **No `items` array column** because we use one row per Item. For "all Items
  on this filing in one go": `SELECT array_agg(item_number) FROM
  edgar_8k_filings WHERE accession = $1`.
- **Index on `item_number`** because the analytical queries are "show all
  Item 5.02 filings" (officer departures) or "all Item 4.02" (non-reliance —
  strongest bearish signal).
- **No FK to `universe_config(ticker)`** — held positions can include tickers
  no longer in the live universe.

---

## 5. `market_news` row contract

The ingester writes one row per 8-K filing using the existing pattern from
`alpaca_news.py`:

```sql
INSERT INTO market_news
  (uuid, primary_ticker, title, publisher, url, published_at, summary, related_tickers)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (uuid) DO NOTHING
```

Per-field mapping:

| `market_news` column | Source |
|---|---|
| `uuid` | SEC accession number (e.g., `0000024741-26-000123`). Stable, unique, immutable. |
| `primary_ticker` | The held ticker for which we resolved the CIK. |
| `title` | Composed: `"8-K — Item 5.02 (Officer Departure), Item 9.01 (Financials) — GLW"`. When no Items parse: `"8-K filed (Items unparsed) — GLW"`. |
| `publisher` | Literal `"SEC EDGAR"`. |
| `url` | The filing's `primary_doc_url`. |
| `published_at` | SEC's `accepted_at` timestamp when available, else `filing_date` at 00:00 UTC. |
| `summary` | Concatenation of Item descriptions, e.g., `"Item 5.02: Departure of Directors or Certain Officers; Election of Directors; Appointment of Officers. Item 9.01: Financial Statements and Exhibits."`. |
| `related_tickers` | `[ticker]` (single-element array). |

This row is what the panic scanner's `score_news_for_tickers` will see when it
queries the pre-market window. The composed `title` is what FinBERT scores
(meaningful negative signal when the Items are bearish: 5.02, 4.02, 1.02, 2.05,
2.06, 3.01).

---

## 6. Item-number extraction

### Strategy

SEC's 8-K form has a stable, well-defined set of Items (defined in 17 CFR
249.308). Most filers use one of three predictable header formats in the
primary document:

```
ITEM 5.02 Departure of Directors or Certain Officers...
Item 5.02. Departure of Directors or Certain Officers...
Item 5.02 — Departure of Directors or Certain Officers...
```

`parse_items_from_document(html: str) -> list[str]`:

1. Strip HTML tags with `re.sub(r'<[^>]+>', ' ', html)` (no BeautifulSoup
   dependency for MVP — SEC filings are mostly well-formed; we don't need a
   real parser).
2. Apply `re.findall(r'(?:^|\s)ITEM\s+(\d+\.\d+)', cleaned_text, re.IGNORECASE)`.
3. Deduplicate while preserving order (TOC entries duplicate body headers).
4. Filter against `ITEM_DESCRIPTIONS` — drop anything that is not a known SEC
   8-K Item. This defensive filter rejects accidental matches like
   "Item 1.01 of the Agreement" buried in narrative text — actual headers
   dominate by frequency in well-formed filings, and the whitelist drops
   stragglers that don't map to known Items.
5. Return the list. Empty list → caller writes the `UNPARSED` row.

### The `ITEM_DESCRIPTIONS` map

Hardcoded in `src/ingestion/edgar_items.py` (not a config file because the
SEC spec changes rarely and downstream code may want to import specific Items
by name; updates ship as code changes):

```python
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
    '5.02': 'Departure of Directors or Certain Officers; Election of Directors; Appointment of Officers',
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
```

---

## 7. Env-var gates

All gates default OFF and follow the SP-* rollout pattern.

| Gate | Default | Effect |
|------|---------|--------|
| `OPENCLAW_EDGAR_8K_INGEST` | `0` | Master gate. systemd timers fire but the script exits 0 silently unless this is `1`. |
| `OPENCLAW_EDGAR_8K_LOOKBACK_HOURS` | `24` | Lookback for the live ingester. The backfill script overrides this from its `--days` CLI argument. |
| `OPENCLAW_EDGAR_8K_MAX_TICKERS_PER_RUN` | `50` | Defensive cap on a single run. Held-position count is well under 50, but caps a runaway loop if the position loader misbehaves. |
| `OPENCLAW_EDGAR_USER_AGENT` | `'FundJohn/OpenClaw contact@fundjohn.ai'` | SEC requires a non-empty UA. Existing `edgar_client.py` reads this. Documented here so the operator knows it MUST be a real contact per SEC policy. |

The ingester does NOT need a separate "auto-act" gate — it never submits
orders, just writes to two tables.

---

## 8. Error handling

- **CIK lookup miss** (ticker not in SEC's `company_tickers.json` — ADRs,
  preferred shares, recently-IPO'd names): log a per-ticker warning, skip,
  continue. No row written. The optional `edgar_8k_freshness` doctor check
  (§10) surfaces persistent misses.
- **`EDGARClient` raises** (timeout, 429 backoff exhausted, 5xx): caught at
  the per-ticker boundary, logged, the script continues to the next ticker.
  The next timer fire (08:45 same morning or tomorrow's 07:15) retries.
- **Item parse fails on a filing's primary doc** (unrecognized HTML layout,
  404, gzip surprise): write the `edgar_8k_filings` row with
  `item_number='UNPARSED'` and `item_description='Item extraction failed'`.
  The `market_news` row is still written (title falls back to
  `"8-K filed (Items unparsed) — TICKER"`) so the scanner gets the news-volume
  signal. Parse-failure rate is logged as a metric.
- **`market_news` insert conflict** (UUID already present — re-running on the
  same filing): `ON CONFLICT (uuid) DO NOTHING`. No-op, expected on every
  subsequent run.
- **`edgar_8k_filings` insert conflict** (accession+item already present):
  `ON CONFLICT (accession, item_number) DO NOTHING`. No-op.
- **SEC API completely down** (all per-ticker calls fail): the ingester logs
  the systemic failure, exits 0 (NOT exit 2 — we do not want systemd to mark
  the unit failed for an external dependency outage; the panic scanner will
  gracefully see fewer news rows). Provider-health table records the outage
  for the operator dashboard.
- **Backfill script failure mid-run:** per-ticker idempotency means re-running
  picks up where it left off. No checkpointing needed. The script can be
  killed and restarted safely.
- **Holiday / weekend timer fire:** same calendar guard as the panic scanner —
  call `is_trading_day_in_et()` from `premarket_helpers.py`, exit 0 silently
  if false. `Mon..Fri` already filters weekends; the helper catches US market
  holidays.

---

## 9. Testing strategy

**Unit tests** — `tests/ingestion/test_edgar_items.py` (~12 tests):

- Well-formed `ITEM 5.02` header → returns `['5.02']`.
- Multi-Item filing → deduped, ordered list.
- Lowercase `item 5.02` matches (case-insensitive).
- HTML-wrapped header (e.g., `<b>Item 5.02</b>`) matches after tag strip.
- TOC + body repetition deduped.
- Unknown Item number (e.g., `Item 99.99`) filtered out by whitelist.
- False-positive guard: `Item 1.01 of the Agreement` inside narrative text —
  the test pins behaviour (current implementation accepts this match; the
  whitelist drops it if `1.01` is not in the doc, otherwise it stands as a
  documented known false-positive class).
- Empty input → `[]`.
- Garbled HTML / non-UTF-8 bytes → returns `[]` (defensive, no exception).
- `ITEM_DESCRIPTIONS` map covers all SEC-spec Items present in three real
  filings sampled across the last 30 days. Three fixture HTML files committed
  to `tests/ingestion/fixtures/edgar/`.

**Integration tests** — `tests/ingestion/test_edgar_8k.py` (~6 tests):

- End-to-end with mocked `EDGARClient` (no real SEC calls in CI) + recorded
  fixture HTML for primary docs + mocked Postgres.
- Ticker without CIK mapping → skipped silently, no DB row.
- Filing already in `edgar_8k_filings` (same accession) → `ON CONFLICT`
  no-op, no duplicate.
- Parse fails for one ticker, succeeds for another → both get `market_news`
  rows (the failed one with title `"8-K filed (Items unparsed) — TICKER"`),
  the failed ticker's `edgar_8k_filings` row uses `item_number='UNPARSED'`.
- Filing outside lookback window → not fetched / not written.
- Master gate OFF → exits 0, no DB writes, asserted.

**Schema test** — `tests/database/test_migration_121.py`:

- Mirrors `tests/database/test_migration_120.py` exactly: column set, indexes,
  composite UNIQUE constraint, table exists.

Target: ~20 new tests, all green before the master gate flips ON.

---

## 10. Coverage monitoring

- The ingester writes a heartbeat to the existing `provider_health` table on
  each successful run (the `edgar` provider entry already exists per the
  grounding inventory). The operator dashboard tile that already shows
  provider health automatically surfaces EDGAR freshness. No new dashboard
  work needed.
- Optional new doctor check `edgar_8k_freshness` (slow=True) verifies: at
  least one row in `edgar_8k_filings` was written in the last 24 hours when
  the master gate is on. Wired into the existing `doctor.py` check registry.
  **In scope but acceptable to defer** if the implementer prefers — the
  provider_health write is the load-bearing piece.

---

## 11. Rollout plan

1. **Land code + migration + tests** on a new branch `feat/edgar-8k-ingest`
   off `main` (after the panic-scanner branch merges). All gates OFF. CI
   green.
2. **Apply migration 121** to live DB.
3. **Run the one-shot 7-day backfill** with the master gate flipped just for
   that command:
   ```bash
   OPENCLAW_EDGAR_8K_INGEST=1 python3 scripts/backfill_edgar_8k.py --days 7
   ```
   Verify per-ticker counts in `edgar_8k_filings`. Spot-check 3-5 rows to
   confirm Item parsing was correct.
4. **Re-run the GLW post-mortem** against the now-populated `market_news`:
   ```bash
   python3 -m scripts.replay_premarket_panic --ticker GLW \
       --as-of 2026-05-28T09:00:00-04:00 --with-sonnet
   ```
   Update section 11 of the panic-scanner spec with the new verdict. If GLW
   filed a material 8-K and the scanner now catches it → the integrated
   system works end-to-end. If GLW did NOT file an 8-K → the price move had
   a non-EDGAR cause and the next follow-up is tape data (Alpaca pre-market
   bars).
5. **Install the systemd timers** and flip `OPENCLAW_EDGAR_8K_INGEST=1` in
   `.env`. `systemctl daemon-reload` and enable the timers. Twice-daily
   fires begin.
6. **Soak for ~5-10 trading days.** Inspect: per-day filing count, per-Item
   distribution, parse-failure rate, SEC outage incidents (provider_health).
   Adjust the parse regex if real filings expose layouts not covered by the
   fixtures.

Each step has a clean revert: set the gate back to `0` and remove the timers.
The ingested data stays (master-data convention is append-only) and is benign
to downstream consumers.

---

## 12. Open questions (to be resolved in the plan)

- The exact path from SEC's submissions JSON to a filing's primary document
  URL. The plan must verify by hitting one real CIK's submissions endpoint
  during implementation. The conventional path is
  `https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_dashes}/{primaryDocument}`
  — confirm against the live API before pinning.
- Whether `EDGARClient` currently exposes a `fetch_url(url) -> bytes`
  primitive for the primary-document fetch, or whether the new code needs
  to add one. The plan must read `edgar_client.py` and decide.
- Backfill duration in practice: the spec assumes ~20 held tickers × ~1-2
  filings each × ~2 HTTP calls per filing ≈ 80 SEC calls (~10 seconds at 10
  req/s). Confirm by running the script once in dry-run before flipping the
  master gate, and tune `OPENCLAW_EDGAR_8K_MAX_TICKERS_PER_RUN` if needed.
