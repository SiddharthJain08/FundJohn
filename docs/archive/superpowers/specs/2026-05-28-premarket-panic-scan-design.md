# Pre-Market Sentiment Panic Scan — Design

**Status:** spec approved, plan pending
**Date:** 2026-05-28
**Motivating incident:** GLW long position dropped ~4% at the 2026-05-28 open in an
environment without a regime-driven SP500 drawdown. The system had no pre-market
look at per-position news, so the loss was unavoidable under the current daily
cadence. This spec adds a sidecar scanner that uses the existing D1 sentiment
ingester during pre-market hours and surfaces ticker-specific panic signals
before the open.

---

## 1. Goals & non-goals

### Goals

- Detect per-ticker panic-sell signals against currently-held equity positions
  before market open, twice per trading morning.
- Reuse the existing D1 sentiment surface (`market_news`, Alpaca News, Reddit,
  StockTwits, FinBERT-Tone) — no new ingesters, no new external data sources.
- Provide an advisory Discord alert path that is always on once the master gate
  is flipped, and a strict, default-OFF auto-close path that uses the existing
  liquidator audit trail.
- Persist every scan result for audit and forward back-testing of scanner
  precision against realized open-to-open / open-to-close moves.
- Ship a one-shot replay tool to answer "would the scanner have flagged GLW on
  2026-05-28?" against historical Postgres data already in the system.
- Do not touch any active SP-* branch, the daily LangGraph orchestrator, the
  regime-blended sizer, the regime-redeploy path, the backtests, or the crypto
  exec lane.

### Non-goals

- Pre-market tape data (Alpaca bars 04:00–09:30 ET). Rejected during
  brainstorming; revisit only if post-soak precision is poor.
- EDGAR 8-K ingestion. Same.
- After-hours scans (e.g., 17:00 ET for AMC earnings). Same architecture would
  work but is deferred.
- Crypto and options positions. Equity-only by scope. Filtered at position load.
- Historical back-fill of realized PnL for alerts that do not exist yet.

---

## 2. Architecture summary

A standalone pre-market scanner runs twice per trading morning (07:30 ET and
09:00 ET) under its own systemd timer. It loads the current broker positions,
asks the existing sentiment helpers for *just those tickers* over a pre-market
window (yesterday 18:00 ET → now), scores them with FinBERT, hands the survivors
to a Sonnet 4.6 confirmer, persists every result to a new audit table, and posts
a Discord summary. A default-OFF auto-close gate, when flipped, asks the
existing liquidator to flatten only the tickers Sonnet flagged with
`severity ≥ 4 ∧ verdict ∈ {bearish_news_driven, bearish_idiosyncratic}`.

The scanner is a sidecar. It does not enter `pipeline_orchestrator.py` or
`src/agent/graphs/daily-cycle.js`. The auto-close path reuses the same
`alpaca_liquidations` audit trail as the operator-only force-liquidation script.

### Data flow

```
07:30 ET timer (and again at 09:00 ET)
  │
  └── run_premarket_scan.py --scan-ts <hh:mm>
        ├── _load_broker_positions()                 # alpaca CLI wrapper
        │     → ['GLW', 'AAPL', ...]
        ├── For each ticker:
        │     ├── fetch market_news rows where published_at >= prior 18:00 ET
        │     ├── fetch Reddit/StockTwits raw posts (parametrized scrapers)
        │     ├── FinBERT score headlines             # existing scorer
        │     └── compute panic_score                 # deterministic rule engine
        ├── For each ticker with panic_score >= ADVISORY_THRESHOLD:
        │     └── Sonnet 4.6 confirmer                # budget-capped, JSON
        ├── INSERT into premarket_panic_alerts
        ├── Post Discord summary to #premarket-watch  # silent if zero flagged
        └── If OPENCLAW_PREMARKET_AUTOCLOSE=1 AND strict gate met:
              regime_liquidator.close_subset(tickers, reason='PREMARKET_PANIC')

16:05 ET separate timer:
  backfill_premarket_realized_pnl.py
    → updates realized_open_to_open_pct / realized_open_to_close_pct
```

---

## 3. Components inventory

| File | Status | Purpose |
|------|--------|---------|
| `src/pipeline/run_premarket_scan.py` | NEW | Entry point. ~250 LOC. CLI: `--scan-ts`, `--dry-run`, `--tickers` (override for debugging). |
| `src/sentiment/premarket_scorer.py` | NEW | Pure rule engine. `panic_score(news_count, finbert_neg_ratio, mean_score, social_count, bear_ratio) -> float`. Easy to unit test. |
| `src/sentiment/sonnet_premarket_confirmer.py` | NEW | Sonnet 4.6 client wrapper. Mirrors `regime_blended_sizer_live` confirmer shape. JSON output schema. Budget cap `--max-budget-usd 0.50`. |
| `src/execution/regime_liquidator.py` | CHANGED (additive) | Add `close_subset(tickers: list[str], reason: str)`. Existing `--force` flatten path untouched. |
| `src/ingestion/news_finbert_scorer.py` | CHANGED (additive) | Expose `score_news_for_tickers(tickers, since_ts)`. Existing daily caller signature preserved. |
| `src/ingestion/social_scrapers.py` (Reddit + StockTwits) | CHANGED (additive) | Add optional `tickers` and `since_ts` kwargs. Default behaviour unchanged. |
| `src/database/migrations/120_premarket_panic_alerts.sql` | NEW | Schema in §4. |
| `scripts/replay_premarket_panic.py` | NEW | One-shot replay for GLW post-mortem and future calibration. Read-only, no DB writes. |
| `scripts/backfill_premarket_realized_pnl.py` | NEW | EOD job at 16:05 ET. Fills realized columns where NULL. Forward-only. |
| `docs/openclaw-premarket-scan.{service,timer}` | NEW | systemd units. Two `OnCalendar` entries: `Mon..Fri 07:30 America/New_York` and `Mon..Fri 09:00 America/New_York`. |
| `docs/openclaw-premarket-realized-backfill.{service,timer}` | NEW | systemd units. Single `OnCalendar` entry at `Mon..Fri 16:05 America/New_York`. |

**Explicit non-touches:** `pipeline_orchestrator.py`, `src/agent/graphs/daily-cycle.js`, any `feat/sp[1-5]-*` branch, `regime_blended_sizer_live.py`, every backtest module, all SP-3.1 crypto code, the regime-redeploy path.

---

## 4. Database schema — Migration 120

```sql
CREATE TABLE premarket_panic_alerts (
    id                          BIGSERIAL PRIMARY KEY,
    scan_ts                     TIMESTAMPTZ NOT NULL,
    scan_label                  TEXT NOT NULL,               -- '07:30' or '09:00'
    trading_day                 DATE NOT NULL,               -- ET-anchored
    ticker                      TEXT NOT NULL,
    held_qty                    NUMERIC NOT NULL,            -- signed; matches broker
    avg_entry_price             NUMERIC,
    -- Rule-based signal
    news_count_window           INT NOT NULL DEFAULT 0,
    news_finbert_neg_ratio      NUMERIC,                     -- 0..1
    news_finbert_mean_score     NUMERIC,                     -- -1..1
    social_post_count_window    INT NOT NULL DEFAULT 0,
    social_bear_ratio           NUMERIC,
    panic_score                 NUMERIC NOT NULL,            -- composite, 0..100
    advisory_fired              BOOLEAN NOT NULL DEFAULT FALSE,
    -- Sonnet confirmer (NULL if rule gate did not pass)
    sonnet_verdict              TEXT,                        -- bullish|neutral|bearish_news_driven|bearish_idiosyncratic|llm_error
    sonnet_severity             INT,                         -- 1..5
    sonnet_rationale            TEXT,
    sonnet_evidence_uuids       UUID[],                      -- pointers into market_news.uuid
    sonnet_cost_usd             NUMERIC,
    -- Auto-close (NULL unless gate was ON and strict criteria met)
    autoclose_fired             BOOLEAN NOT NULL DEFAULT FALSE,
    autoclose_liquidation_id    BIGINT REFERENCES alpaca_liquidations(id),
    -- Realized outcome (backfilled at 16:05 ET)
    realized_open_to_open_pct   NUMERIC,                     -- (next_open - this_open) / this_open
    realized_open_to_close_pct  NUMERIC,                     -- (close - open) / open
    realized_backfilled_at      TIMESTAMPTZ,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX premarket_panic_alerts_day_ticker
    ON premarket_panic_alerts(trading_day, ticker);
CREATE INDEX premarket_panic_alerts_scan_ts
    ON premarket_panic_alerts(scan_ts);
```

Schema notes:

- One row per `(ticker, scan)` — both 07:30 and 09:00 scans persist, allowing
  comparison of early-warning vs late-warning precision.
- `held_qty` and `avg_entry_price` are snapshotted at scan time (not joined
  later) because broker state mutates.
- `panic_score` is always written; `sonnet_*` columns are NULL when the rule
  gate did not pass. NULL distinguishes "Sonnet said no" (`sonnet_verdict =
  'bullish'/'neutral'`) from "Sonnet was not asked" (`sonnet_verdict IS NULL`).
- `sonnet_evidence_uuids` points into `market_news.uuid`. Reddit/StockTwits do
  not have stable per-post UUIDs in the current schema, so social evidence is
  captured as quoted snippets inside `sonnet_rationale` rather than as UUID
  pointers. Acceptable for audit because Sonnet's rationale always quotes the
  relevant text inline.
- No FK on `ticker` to any universe table — open positions can include tickers
  no longer in the live universe (delisted-but-not-yet-flat).
- `autoclose_liquidation_id` is an FK into the existing `alpaca_liquidations`
  table, so the operator can drill from a panic alert to the actual fill record.

---

## 5. Env-var gates

All gates default OFF and follow the SP-* rollout pattern.

| Gate | Default | Effect |
|------|---------|--------|
| `OPENCLAW_PREMARKET_SCAN` | `0` | Master gate. systemd timer is installed but the service refuses to run unless this is `1`. Lets the migration and code ship without anything firing in production. |
| `OPENCLAW_PREMARKET_CONFIRMER` | `0` | Calls Sonnet on rule-flagged tickers. If OFF, rule-based `panic_score` is still computed, persisted, and posted to Discord; `sonnet_*` columns are NULL. Lets the operator observe rule precision before paying for LLM. |
| `OPENCLAW_PREMARKET_AUTOCLOSE` | `0` | Strict gate for auto-flatten. Service refuses to start if `OPENCLAW_PREMARKET_AUTOCLOSE=1` and `OPENCLAW_PREMARKET_CONFIRMER=0` — auto-close requires a valid Sonnet verdict. |
| `OPENCLAW_PREMARKET_DISCORD_WEBHOOK_NAME` | `premarket-watch` | Channel name to look up in `agent_registry.webhook_urls`. Falls back to `trade-reports` if the name is missing. |
| `OPENCLAW_PREMARKET_ADVISORY_THRESHOLD` | `35` | `panic_score` floor for an advisory Discord post. Tunable post-soak. |
| `OPENCLAW_PREMARKET_AUTOCLOSE_MIN_SEVERITY` | `4` | Sonnet severity floor for auto-close (in addition to verdict ∈ `{bearish_news_driven, bearish_idiosyncratic}`). |
| `OPENCLAW_PREMARKET_MAX_TICKERS_PER_SCAN` | `25` | Hard cap on Sonnet calls per scan. Defensive; typical position count is well under 25. |

Gate hierarchy is enforced at service startup, not at runtime, so a
misconfigured environment fails fast and loud rather than degrading silently.

---

## 6. Error handling

- **Sonnet call fails** (timeout, 429, budget exceeded): persist the row with
  `sonnet_verdict = 'llm_error'` and `sonnet_rationale = <error message>`.
  Advisory Discord post still fires based on rule-based score. Auto-close does
  **not** fire without a valid Sonnet verdict — this is a hard precondition.
- **Alpaca CLI fails on `position list`**: log error, abort cleanly with
  exit code 2 (`CycleAbort` semantics under `OPENCLAW_STRICT_EXIT_CODES=1`).
  No partial state written.
- **Market closed today** (holiday): the systemd timer's `OnCalendar` already
  restricts to Mon–Fri; additionally call the market-calendar helper used by
  `redeploy_pipeline.py` and exit 0 silently on US holidays. No Discord noise.
- **Sentiment ingester partial failure** (e.g., Reddit 403 from the 2026-05-20
  incident): degrade gracefully. Compute `panic_score` from whatever source did
  return, mark `news_count_window` / `social_post_count_window` honestly, and
  note the degraded source in the Discord post. Do not fail the scan.
- **Auto-close fails on a ticker** (DTBP guard rejects, ticker halted): log per-
  ticker failure and write `autoclose_fired = false` for that row. Post the
  failure to Discord. Other tickers in the same scan still get closed. Mirrors
  the existing `close_remaining_positions.py` per-position try/except.
- **Pre-market order routing**: orders submitted at 07:30 or 09:00 ET are in
  Alpaca's extended-hours window. OPG / MOO is known-flaky on paper (~7% fills,
  per the 2026-05-18 incident), so `close_subset` must use limit orders with
  `extended_hours=true` and `time_in_force=day`. The exact limit-price strategy
  (e.g., NBBO-cross or last-trade ± a buffer) is deferred to the plan but must
  be specified there, not improvised at runtime.
- **Backfill job fails at 16:05 ET**: realized columns stay NULL. The next-day
  run skips already-backfilled rows; failed rows are retried on the following
  EOD. Idempotent and no doubled writes.

---

## 7. Testing strategy

- **Unit tests** (`tests/sentiment/test_premarket_scorer.py`): table-driven
  tests of `panic_score` covering zero-news baseline, single bearish headline,
  sustained negative news flood, mixed bull/bear social, NaN handling, and
  degraded-source mode. Threshold boundary tests at 34/35/99. ~15 fixtures.
- **Integration tests** (`tests/pipeline/test_premarket_scan.py`): end-to-end
  with Postgres and recorded `alpaca position list` fixtures, Sonnet mocked.
  Assertions: row written; Discord webhook called; auto-close **not** called
  when gate OFF; auto-close **called once with the correct ticker subset** when
  gate ON and severity threshold met.
- **Gate-hierarchy test**: `OPENCLAW_PREMARKET_AUTOCLOSE=1` +
  `OPENCLAW_PREMARKET_CONFIRMER=0` → service refuses to start (exit 2, clear
  error in stderr).
- **Holiday-skip test**: mock the market calendar to return closed; assert
  exit 0, no row written, no Discord post.
- **Sonnet contract test**: schema-validate the JSON Sonnet returns against the
  expected shape; reject and persist `llm_error` if it does not match. Pinned-
  prompt golden fixture so any prompt drift fails CI.
- **Liquidator subset test** (`tests/execution/test_close_subset.py`):
  `close_subset(['GLW', 'AAPL'], reason='PREMARKET_PANIC')` only closes those
  two; leaves others untouched. Recorded fixture with five holdings.
- **Migration test**: applies and rolls back cleanly under the existing
  migration test harness.

Target: ~35–40 new tests, all green before any gate flips ON.

---

## 8. GLW replay tool

`scripts/replay_premarket_panic.py --ticker GLW --as-of 2026-05-28T09:00:00-04:00`

Behaviour:

1. Pull `market_news` rows where `(primary_ticker = 'GLW' OR 'GLW' = ANY(related_tickers)) AND published_at BETWEEN <as-of - 15h> AND <as-of>` — the prior 18:00 ET → 09:00 ET window the live scanner uses.
2. Pull Reddit/StockTwits raw rows for the same window from whatever raw
   storage the scrapers persist into. If the scrapers are stream-only and do
   not persist raw posts, this is a documented limitation and the replay only
   uses news for now. (To be verified during implementation.)
3. Run the **same** `premarket_scorer` rule engine, and (if `--with-sonnet`)
   the **same** confirmer prompt.
4. Print a single-pane verdict: `panic_score`, would-have-fired-advisory,
   Sonnet verdict, Sonnet rationale, list of headlines with FinBERT scores.
5. Does **not** write to `premarket_panic_alerts`. Replay-only; no
   contamination of live precision metrics.

For GLW specifically this answers the operator's question this week: *did the
news exist by 09:00 ET on 2026-05-28 to justify pre-market liquidation?* The
verdict drives the threshold tuning and decides whether pure sentiment is
sufficient or whether tape / EDGAR follow-ups are warranted.

---

## 9. Rollout plan

1. **Land code + migration + tests on `main`** with all gates OFF. Verify the
   nightly daily-cycle cron is unaffected and CI is green.
2. **Run GLW replay** against historical Postgres data. Establish the baseline
   verdict.
3. **Soak Phase 1 — rules only**: flip `OPENCLAW_PREMARKET_SCAN=1`. Two-shot
   scans persist rule-based rows; Discord posts whenever `panic_score ≥ 35`.
   No Sonnet cost, no liquidations. Soak ~5–10 trading days. Inspect false-
   positive / false-negative rates against realized price action.
4. **Soak Phase 2 — add confirmer**: flip `OPENCLAW_PREMARKET_CONFIRMER=1`.
   Adds ~$0.10/day. Sonnet rationale appears in Discord. Operator manually
   decides each morning whether to close. Soak ~5–10 trading days. Build a
   calibration table.
5. **Soak Phase 3 — auto-close armed**: flip `OPENCLAW_PREMARKET_AUTOCLOSE=1`
   only after at least one operator-confirmed match where the operator did
   manually close at the recommendation. Soak with a low position count first
   if practical.

Each phase has a clear revert: set the gate back to `0` and reload systemd.
Each phase emits Discord summaries so the operator can monitor.

---

## 10. Open questions (to be resolved in the plan)

- The replay tool depends on whether Reddit and StockTwits raw posts are
  persisted to Postgres in the current scrapers, or whether they are scored in-
  stream and discarded. If discarded, the replay's social coverage is limited
  to whatever made it into `ticker_sentiment_daily.social_top_themes` for the
  target day. The plan must verify this and either accept the limitation or add
  a minimal raw-post table.
- The exact composite formula for `panic_score` is left to the plan. The unit
  test fixtures will pin it once the formula is chosen. Initial sketch:
  `panic_score = 60 * finbert_neg_ratio + 30 * (news_count_window / news_count_baseline_p90) + 10 * social_bear_ratio`, clipped to `[0, 100]`. To be validated against the GLW replay before the threshold default of `35` is committed.
- The Sonnet prompt template lives in `src/sentiment/sonnet_premarket_confirmer.py`. The plan must define its golden fixture for CI pinning.
- `close_subset` pre-market order submission: limit-price strategy (NBBO-cross
  vs last ± buffer vs Alpaca's `position close` default) must be specified in
  the plan, including the explicit `extended_hours=true` and `time_in_force=day`
  fields on the order payload. OPG/MOO is forbidden per the 2026-05-18 incident.
- Equity-only position filter: the plan must specify the exact predicate used
  to drop crypto and options from the scanned set — most likely
  `instrument_class = 'equity'` against the strategies/positions join, but the
  precise table and column reference is to be confirmed at implementation time.

---

## 11. GLW post-mortem result (2026-05-28)

Replay run on 2026-05-28 against the production `market_news` table.

**Sanity counts (pre-market window 2026-05-27 18:00 ET -> 2026-05-28 09:00 ET):**
- GLW news rows in `market_news`: 0
- Earliest: N/A (no rows)
- Latest: N/A (no rows)

Note: The production `market_news` table contains 563 total rows spanning
2026-04-27 to 2026-05-27 UTC. The one GLW row that exists in the table is
dated 2026-05-19 10:09 UTC (title: "Corning Leans Into AI Infrastructure
Growth As Valuation And Risks Stand Out") -- a week-old article, well outside
the pre-market window. No GLW news was ingested during the 2026-05-27
18:00 ET -> 2026-05-28 09:00 ET window.

**07:30 ET replay** (mode: with-sonnet):
- news_count: 0
- finbert_neg_ratio: 0.0
- panic_score: 0.0
- advisory_would_fire: false
- Sonnet verdict: not run (no news to pass to confirmer)
- Sonnet severity: not run
- Sonnet rationale: not run
- Headlines surfaced: (none)

**09:00 ET replay** (mode: with-sonnet):
- news_count: 0
- finbert_neg_ratio: 0.0
- panic_score: 0.0
- advisory_would_fire: false
- Sonnet verdict: not run (no news to pass to confirmer)
- Sonnet severity: not run
- Sonnet rationale: not run
- Headlines surfaced: (none)

**Conclusion:**

The scanner would NOT have caught GLW on 2026-05-28. The production
`market_news` table contained zero GLW articles in the 15-hour pre-market
window (2026-05-27 18:00 ET to 2026-05-28 09:00 ET), so both the 07:30 ET
and 09:00 ET replays returned a panic_score of 0.0 and advisory_would_fire
= false. This is a news-ingestion coverage gap, not a scoring or threshold
failure: the scanner's logic is sound, but the input data was absent entirely.

This outcome strongly warrants the follow-up specs suggested in the design
document. Specifically, an EDGAR 8-K tap (or pre-market tape / earnings
transcript wire) is the most likely source of the adverse news that caused
the ~4% open-day drop -- such disclosures arrive hours before open via the
SEC EDGAR RSS feed and would appear in `market_news` only if an 8-K ingestor
is running. Recommend prioritising: (1) an EDGAR 8-K ingestor that polls the
SEC EDGAR RSS for current-report filings by held tickers, and (2) a diagnostic
rule that flags when a held ticker has zero news ingested in the prior 24 h,
as a coverage-gap alert rather than a silence-is-safe assumption.

---

### 11.1. Post-EDGAR-integration replay (2026-05-28)

After shipping the EDGAR 8-K ingester (`2026-05-28-edgar-8k-ingester-design.md`),
re-ran the GLW replay against the now-populated market_news. EDGAR backfill
window: 7 days.

**EDGAR backfill found for GLW (filing_date >= 2026-05-21):**
- no 8-Ks in window (backfill returned total new=0; edgar_8k_filings table
  contains 0 rows for GLW across all dates)

**market_news rows for GLW since 2026-05-27 (publisher='SEC EDGAR'):**
- no rows

**07:30 ET replay (post-EDGAR):**
- news_count: 0
- finbert_neg_ratio: 0.0
- panic_score: 0.0
- advisory_would_fire: false
- Sonnet verdict: not run (no news to pass to confirmer)
- Sonnet severity: not run
- Sonnet rationale: not run

**09:00 ET replay (post-EDGAR):**
- news_count: 0
- finbert_neg_ratio: 0.0
- panic_score: 0.0
- advisory_would_fire: false
- Sonnet verdict: not run (no news to pass to confirmer)
- Sonnet severity: not run
- Sonnet rationale: not run

**Conclusion:**

GLW filed NO 8-K with the SEC in the 7-day window ending 2026-05-28. The
`edgar_8k_filings` table (populated by the new ingester) returned zero rows for
GLW for dates >= 2026-05-21, and the `market_news` table likewise has no SEC
EDGAR rows for the ticker. With both Alpaca news and EDGAR 8-Ks absent from the
pre-market window, the post-EDGAR replay produces identical results to the
pre-EDGAR baseline: news_count=0, panic_score=0.0, advisory_would_fire=false at
both 07:30 and 09:00 ET.

The EDGAR integration is working correctly — it ingested from SEC's EDGAR API
and confirmed no material 8-K filing (Items 5.02 leadership change, 4.02
auditor concerns, 2.05 workforce reduction, etc.) was made by GLW in the look-
back window. The ~4% open-day drop therefore originated from a non-EDGAR source:
the most likely candidates are an analyst downgrade issued pre-market via
a research terminal wire (not public RSS), options market positioning / large
block print, or sector rotation in Corning's optical-networking segment on
tariff/AI-spend news. The next follow-up spec is pre-market tape data (Alpaca
bars 04:00-09:30 ET) so the scanner can detect price-action panic and surface
unusual volume/gap signals regardless of news availability.
