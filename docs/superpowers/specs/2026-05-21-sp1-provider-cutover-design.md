# SP-1: Daily Pipeline Provider Cutover — Design

**Spec date:** 2026-05-21
**Author:** BotJohn brainstorm session
**Status:** Approved for implementation
**Parent program:** Data-provider overhaul (5 sub-projects: SP-1 through SP-5)
**Scope:** SP-1 only. See `2026-05-21-data-provider-overhaul-handoff.md` for SP-2/3/4/5.

---

## 1. Context

The operator purchased Alpaca Algo Trader Plus (AAT Plus) and cancelled the Polygon Options Starter subscription. Massive S3 flatfiles — confirmed Polygon-affiliated via `src/ingestion/massive_client.py:4` — lose access alongside Polygon. Goal: move the daily pipeline to Alpaca-primary + FMP-secondary, bound yfinance to a single endpoint family (CBOE volatility indices, possibly forward earnings calendar), strip Polygon and Yahoo from the registry entirely, wire the AAT Plus News API into the existing sentiment pipeline, and build a self-archive replacement for historical options EOD.

### Anchored facts (SP-0 audit + live probe)

- **AAT Plus delivers populated greeks** on actively-traded contracts. Probe (2026-05-21):
  - SPY 2026-06-18 ATM strikes 740–749: delta 0.45–0.57, full set, IV-derived.
  - AAPL 2026-06-18 ATM strikes 290–305: delta 0.52–0.77, full set.
  - GME 2026-06-18 near-ATM: full set.
  - Zero-greek strikes restricted to 0-DTE/expired OR deep-ITM with zero recent volume.
- **FMP tier**: Starter (~300 req/min), hardcoded in `preferences.json`. Bulk `earning_calendar` returns 403; per-ticker `historical/earning_calendar/{ticker}` works (historical only).
- **Polygon status**: free equity tier still functional, but the operator wants a clean break — Polygon is treated as fully revoked for design purposes.
- **Massive S3 flatfiles**: Polygon-affiliated, lost in the same revocation. `src/ingestion/massive_client.py` and `src/ingestion/massive_ws.py` go away with Polygon.
- **yfinance load-bearing paths**: CBOE vol indices (`fetch_vol_indices.py`, `ingest_vol_indices.py`, `backfill_vix.py`) AND forward earnings calendar (`ingest_earnings_calendar.py`). FMP Starter does not cover either bulk path.

### Decisions locked in brainstorm

| Question | Decision |
|---|---|
| Vol indices replacement | Keep yfinance bounded to CBOE vol indices (single module, CI-lint-enforced). |
| Options-chain cutover posture | Verified-then-decide → probe confirmed greeks populate → clean swap with downstream filter, no parity-shadow. |
| SP-1 scope | Narrow swap + News API + thin observability. Universe expansion, asset-class expansion, streaming WS, research uplift = out of scope (SP-2 through SP-5). |
| FMP tier | Starter (300 req/min) — design works within this cap. |
| Polygon final state | Fully stripped from registry. |
| Historical options EOD replacement | Path 3 — self-archive forward via daily Alpaca EOD chain snapshot + one-shot cutover-gap backfill. |
| Cutover approach | Big-bang single-PR deploy on a Saturday + Monday soak hardening. |

---

## 2. Architecture

### Provider matrix after cutover

| Concern | Before | After |
|---|---|---|
| Equity real-time quotes | Polygon P1 → FMP P2 → Alpaca P3 → Yahoo P4 | **Alpaca P1 → FMP P2** |
| Equity 30-minute bars | Polygon `ingest_prices_30m.py` | **Alpaca `data bars --timeframe 30Min`** |
| Equity daily bars | Polygon snapshot+aggs | **Alpaca `data bars --timeframe 1Day`** |
| Options chain + greeks + IV | Polygon (default) → Alpaca (gated OFF) → yfinance BS-synthetic | **Alpaca `data option chain`** + downstream greeks-validity filter; **no fallback** |
| Historical options EOD | Massive S3 flatfiles | **NEW**: Daily EOD self-archive job (`scripts/archive_options_eod.py` via `pipeline.backfillers.alpaca_options`) appending to `options_eod.parquet`; one-shot cutover-gap backfill script for missing dates |
| CBOE vol indices | yfinance | **yfinance — sole bounded interface** behind `src/ingestion/cboe_vol_indices.py`; CI lint enforces module-as-sole-importer |
| Forward earnings calendar | yfinance `Ticker(t).calendar` | **TBD by probe at PR-build**: FMP per-ticker forward endpoints (candidates: `/v3/earnings-calendar-confirmed`, `/v4/earning-calendar-confirmed`, per-ticker variants of `/v3/earning_calendar`) if Starter allows; otherwise bounded yfinance extension in `cboe_vol_indices.py` (lint allowlist updated to permit `get_forward_earnings_calendar()`) |
| Earnings surprises (historical) | FMP | Unchanged |
| Fundamentals / ratios / financials | FMP backfiller | Unchanged |
| Insider trades | FMP flags + SEC EDGAR canonical | Unchanged |
| Universe ref | FMP `/available-traded/list` | Unchanged (S&P 500 still — universe expansion is SP-2) |
| Sector ETF data | Polygon snapshot (generic) | Alpaca snapshot (generic) |
| Macro daily series (TLT/HYG/GLD/DXY) | FMP primary, yfinance fallback | **FMP only** |
| Order execution / reconciliation / screener / corp-actions | Alpaca CLI | Unchanged |
| **News (NEW)** | Reddit/StockTwits/news_finbert RSS → `ticker_sentiment_daily` | **Add Alpaca News API** as additional source → new `alpaca_*` columns |
| **Doctor preflight (NEW)** | Polygon + FMP + Alpaca auth checks | **Drop Polygon; add AAT Plus tier verification + options archive freshness + vol indices freshness** |
| **Dashboard "Data Health" tile (NEW)** | None | Operator Control Room (:7870) tile reading `data_provider_health` |

### Key trade-off

After cutover, the equity quote chain has only one fallback (FMP), and the options chain has no fallback. If Alpaca options goes down mid-cycle, the daily cycle aborts cleanly rather than degrade to synthetic data. This is deliberate — synthetic-greeks fallback was a silent-degradation risk (see `feedback_silent_failure_pattern.md`).

---

## 3. Components

### Deletions (~1,200 LoC net removed)

```
src/ingestion/quote_sources/polygon.py
src/ingestion/quote_sources/yahoo.py
src/ingestion/massive_client.py
src/ingestion/massive_ws.py
src/pipeline/backfillers/polygon.py
src/agent/tools/mcp/polygon.js
src/agent/tools/mcp/yahoo.js
workspaces/default/tools/polygon.py            (auto-gen removed)
workspaces/default/tools/yahoo.py              (auto-gen removed)
src/pipeline/collector.js:690-718              (yfinance BS-synthetic greeks fallback)
src/pipeline/collector.js OPTIONS_DATA_SOURCE  (dispatch logic — always alpaca now)
```

### Refactors

```
src/ingestion/fetch_vol_indices.py
  → RENAMED to src/ingestion/cboe_vol_indices.py
  → Normalized surface: get_vix(), get_vvix(), get_vix3m(), get_vix9d()
  → Possibly: get_forward_earnings_calendar() if FMP probe fails
  → SOLE allowed yfinance importer; CI lint enforces

src/ingestion/ingest_vol_indices.py
src/ingestion/backfill_vix.py
src/pipeline/backfillers/yfinance.py
  → All import from new cboe_vol_indices module (no direct yfinance imports)

src/pipeline/collector.js
  → Options chain section rewritten: direct call to alpaca data option chain,
    paginated, with greeks-validity filter at the consumer boundary
  → Quote-source priority swap: Alpaca P1 → FMP P2
  → ~150 LoC removed, ~80 LoC added

src/ingestion/quote_sources/alpaca.py
  → Bumped from priority=3 to priority=1
  → Surface unchanged; tested under load

src/agent/config/servers.json
  → polygon entry REMOVED
  → yahoo entry REMOVED (bounded vol-indices is internal, not a subagent tool)
  → alpaca entry expanded with option_chain + news capabilities

src/agent/config/subagent-types.json
  → polygon + yahoo stripped from every subagent tools array
  → alpaca tools list expanded for botjohn, tradejohn, paperhunter where relevant
```

### Additions

```
src/pipeline/backfillers/alpaca_options.py                   ~250 LoC
  Daily EOD self-archive: iterate universe, paginate full chain per ticker,
  append to options_eod.parquet partitioned by date, dedupe on
  (date, contract_symbol), Redis checkpoint per ticker, semaphore=8,
  30-min soft budget.

scripts/backfill_options_eod_cutover_gap.py                  ~150 LoC
  One-shot. Read last archived date in options_eod.parquet; for each
  missing date in window, enumerate contract symbols via current chain
  (filtered to expiry ≥ that date), batch `alpaca data option bars` 100 at
  a time, write to parquet.

src/ingestion/alpaca_news.py                                 ~180 LoC
  Consumes `alpaca data news --symbols <chunked, 50 per call> --start 24h-ago`.
  FinBERT-scores each article via :7872. Writes to ticker_sentiment_daily
  new columns. Replaces news_rss_ingest for ticker-attributed news; RSS path
  stays for general-market news without ticker tags.

src/database/migrations/109_alpaca_news_columns.sql
  ALTER TABLE ticker_sentiment_daily ADD COLUMN:
    alpaca_news_count_24h INT,
    alpaca_news_finbert_pos INT,
    alpaca_news_finbert_neu INT,
    alpaca_news_finbert_neg INT,
    alpaca_news_mean_score NUMERIC,
    alpaca_news_top_headlines JSONB

src/database/migrations/110_data_provider_health.sql
  CREATE TABLE data_provider_health (
    provider TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    success_count INT DEFAULT 0,
    error_count INT DEFAULT 0,
    last_error TEXT,
    last_error_at TIMESTAMPTZ,
    window_start TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (provider, endpoint, window_start)
  );

(Separately, the `data_source` column on options_eod.parquet is a parquet-file
column added by the new alpaca_options.py writer — parquet schema evolves
naturally, no migration needed. Existing rows have NULL data_source which is
treated as 'polygon_massive' by consumers.)

src/maintenance/doctor.py                                    MODIFY
  REMOVE: _check_polygon_auth, _check_massive_credentials
  ADD:    _check_alpaca_aat_plus_tier (probes SPY 30-DTE chain + 1 news fetch)
  ADD:    _check_options_archive_freshness (options_eod.parquet ≤2 trading days old)
  ADD:    _check_cboe_vol_indices_freshness (vol_indices ≤1 trading day old)

src/channels/dashboard/server.js                             MODIFY
  Add /api/data-health endpoint reading data_provider_health.
  Add Data Health tile with per-provider success/error counts + traffic light.

src/strategies/implementations/_greeks_filter.py             ~40 LoC
  filter_valid_greeks(snapshot, *, allow_zero_delta=False, alert=True)
  Returns snapshot only if delta != 0 OR allow_zero_delta=True;
  logs+counter-increment + #data-alerts when meaningful contract returns
  zero greeks (anomaly path).

CI lint (added to .pre-commit-config.yaml + GHA workflow)
  Fail build if `import yfinance` outside src/ingestion/cboe_vol_indices.py
  Fail build if `import polygon`, MASSIVE_ACCESS_KEY_ID, OPTIONS_DATA_SOURCE,
    or references to deleted modules appear anywhere in src/ or scripts/

tests/test_alpaca_news.py
tests/test_options_archive.py
tests/test_greeks_filter.py
tests/test_cboe_vol_indices.py
tests/test_doctor_cutover.py
tests/test_collector_cutover.py
tests/test_cutover_smoke.py                                  end-to-end smoke

docs/superpowers/specs/2026-05-21-sp1-provider-cutover-design.md  (this file)
```

### .env changes

```
REMOVE:
  POLYGON_API_KEY
  MASSIVE_ACCESS_KEY_ID
  MASSIVE_SECRET_KEY
  OPTIONS_DATA_SOURCE
  OPENCLAW_OPTIONS_BACKFILL_DAYS

ADD:
  ALPACA_NEWS_INGEST=1                  (default ON after cutover)
  ALPACA_OPTIONS_ARCHIVE=1              (default ON after cutover)
  ALPACA_DATA_TIER=algo_trader_plus     (informational)
  FMP_FORWARD_EARNINGS_PROBE=1          (one-shot; removed after PR-build)
  ALPACA_SOAK_MODE_UNTIL=<deploy+7d>    (tightened thresholds for 7 days post-deploy)
```

### Subagent tool surface

| Subagent | Before | After |
|---|---|---|
| botjohn | fmp, polygon, alpaca, sec_edgar, tavily, yahoo | fmp, alpaca, sec_edgar, tavily |
| tradejohn | (none) | (none) |
| paperhunter | WebFetch, WebSearch, Read | unchanged |
| strategycoder | (none) | (none) |
| mastermind | (none) | (none) |
| datawiring | (none) | (none) |

### Memory + docs updates (same PR)

```
/root/.claude/projects/-root/memory/feedback_alpaca_options_zero_greeks.md
  → "Resolved <deploy-date> by AAT Plus. Greeks populate on actively-traded
    contracts. Zero-greek strikes = 0-DTE or no-flow contracts; filter handles."

/root/openclaw/CLAUDE.md "Recent Changes"
  → SP-1 entry with deploy date + PR link

/root/openclaw/ARCHITECTURE.md
  → Provider matrix table updated

/root/.claude/projects/-root/memory/project_av_purge_and_gates.md
  → Polygon joins AlphaVantage in purged-providers section
```

**Net diff estimate:** ~1,200 lines deleted, ~800 lines added, ~150 lines refactored. Net smaller codebase by ~250 LoC.

---

## 4. Data Flow

### Daily cycle (10:00 ET, Mon–Fri) — collect stage

```
Equity quotes/bars:    Alpaca P1 → FMP P2 (hard fail past P2; doctor catches)
Options chain:         alpaca data option chain (paginated, filtered via _greeks_filter)
                       Written to in-cycle dataframe for signals stage.
                       NOT yet appended to options_eod.parquet (archive is post-close).
CBOE vol indices:      cboe_vol_indices.get_vix/vvix/vix3m/vix9d() (bounded yfinance)
Macro series:          FMP only (no yfinance fallback)
Fundamentals:          FMP (unchanged)
Forward earnings:      FMP per-ticker OR yfinance bounded extension (PR-build probe decides)
```

### Daily cycle — sentiment stage

```
Existing: Reddit (Atom RSS), StockTwits (polite UA), news_rss_ingest (general)
NEW:      Alpaca News API
            data news --symbols <50 per call> --start 24h-ago
            FinBERT score via :7872 (existing service)
            Aggregate per-ticker → alpaca_news_count_24h, finbert pos/neu/neg, mean_score
            Top-3 by |score| → JSONB

All sources merged into ticker_sentiment_daily.
```

### New post-close job (16:30 ET / 20:30 UTC, Mon–Fri) — options EOD self-archive

```
systemd timer: openclaw-options-archive.timer
ExecStart:     python3 -m pipeline.backfillers.alpaca_options --date $TODAY

For each ticker in alpaca_tradable_universe WHERE active=true:
  1. alpaca data option chain --underlying-symbol <T> --limit 100
  2. Paginate via --page-token until exhausted
  3. Flatten to rows: (date, underlying, contract_symbol, strike, expiry, type,
                       open, high, low, close, volume, vwap, transactions,
                       delta, gamma, theta, vega, rho, bid, ask, iv_implied,
                       data_source='alpaca_aat_plus')
  4. Keep ALL contracts (data is data; consumers filter at read time)
  5. Append-dedupe on (date, contract_symbol) → options_eod.parquet
  6. data_provider_health: alpaca/options_chain success++

Checkpoint: redis SET options_archive:done:{date} (24h TTL)
Idempotent: re-run reads checkpoint, skips completed tickers
Concurrency: semaphore=8
Rate limit: AAT Plus 10k/min — well under cap (~4k calls for 200 tickers)
Soft budget: 30 min wall; on exceed → partial commit + #data-alerts
```

### One-shot cutover-gap backfill

```
python3 scripts/backfill_options_eod_cutover_gap.py \
    --from-date $POLYGON_REVOCATION_DATE \
    --to-date $YESTERDAY

1. Read options_eod.parquet → find last archived date
2. For each missing date D:
   a. Pull current chain to enumerate contract symbols with expiry ≥ D
   b. Batch alpaca data option bars 100 at a time for date D
   c. Flatten + append to options_eod.parquet (data_source='alpaca_aat_plus_backfill')
3. Final report: rows per date, completeness vs expected universe
```

### CBOE vol indices flow

```
Bot startup AND daily cycle "collect":
  from src.ingestion.cboe_vol_indices import get_vix, get_vvix, get_vix3m, get_vix9d
  yfinance.download(["^VIX", "^VVIX", "^VIX3M", "^VIX9D"])
  Persist to vol_indices.parquet (append-dedupe on date)
  data_provider_health: yfinance/cboe_vol_indices success++

Optional: get_forward_earnings_calendar() if PR-build probe shows FMP Starter
  per-ticker forward endpoint doesn't work.
```

### Greeks-validity filter (strategy consumer boundary)

```python
# src/strategies/implementations/_greeks_filter.py

def filter_valid_greeks(snapshot, *, allow_zero_delta=False, alert=True):
    """Drop contracts where greeks are degenerate. Log+alert on anomalies."""
    dte = (snapshot.expiry - today()).days
    if snapshot.greeks.delta == 0:
        if dte == 0 or snapshot.volume == 0:
            return None  # expected: 0-DTE or untraded
        if not allow_zero_delta:
            if alert:
                _alert_zero_greeks_anomaly(snapshot)
            return None
    return snapshot

# Usage:
from ._greeks_filter import filter_valid_greeks
chain = [c for c in alpaca_chain if filter_valid_greeks(c) is not None]
```

Imported by: `S5_max_pain`, `S15_*`, `S21_*`, `S_HV_*`, and any new options-using strategy.

### Doctor preflight order

```
1. _check_alpaca_auth                  (existing)
2. _check_alpaca_aat_plus_tier         (NEW)
3. _check_fmp_auth                     (existing)
4. _check_postgres                     (existing)
5. _check_redis                        (existing)
6. _check_data_freshness_prices        (existing)
7. _check_options_archive_freshness    (NEW)
8. _check_cboe_vol_indices_freshness   (NEW)
9. _check_regime_freshness             (existing)
10. _check_systemd_services            (existing)

Exit codes: 0=pass, 1=warn, 2=fail-abort
```

### Dashboard "Data Health" tile (:7870)

Surfaces rolling 24h `data_provider_health`:
- Per-provider success/error count + last error message
- Color: green (>99% success), yellow (95–99%), red (<95%)
- Click-through to last 50 errors for that provider+endpoint

---

## 5. Error Handling + Rollback

### Failure-mode matrix

| Failure | Detection | Response | Severity |
|---|---|---|---|
| Alpaca data API outage mid-cycle | HTTP 5xx / timeout in collector | Hard fail. Cycle aborts. No synthetic fallback. Discord `#data-alerts` + doctor red. | HIGH |
| AAT Plus subscription lapse / billing | Doctor `_check_alpaca_aat_plus_tier` 401/403 | Doctor exit 2 → cycle refuses start. `ExecStartPre` fails. Operator action required. | HIGH |
| FMP 403 / 429 | Semaphore + 3-retry exp-backoff | Soft retry; persistent → hard fail (no fallback). 5-min cache often masks brief outages. | MEDIUM |
| Options archive partial failure | Per-ticker Redis checkpoint + soft-budget timer | Partial commit to parquet (completed tickers only). #data-alerts post with missing tickers + replay command. Idempotent re-run. | MEDIUM |
| Greeks filter rejects too many contracts | `data_provider_health.error_count` per ticker | Alert at >5%, doctor warn at >10%, hard fail at >25%. AAT Plus regression — escalate. | HIGH |
| Alpaca News API fails / rate-limits | Per-batch try/except; sentiment step non-fatal | Skip Alpaca news for cycle; other sources still feed `ticker_sentiment_daily`. `alpaca_*` columns null. | LOW |
| yfinance breaks (vol indices) | `cboe_vol_indices` raise; doctor staleness check | HMM stale up to 1 day = carry-forward; >2 days = doctor exit 2 + cycle abort. Standby plan: emergency Tiingo add. | HIGH (latent) |
| Cutover-gap backfill missing contracts | Per-contract status log | Manual review; acceptable as long as visible. | LOW |
| Migration 109/110 partial apply | Transactional; either applies or rolls back | Standard rollback. Code checks for column existence and degrades. | LOW |

### Pre-deploy hardening

1. `OPENCLAW_STRICT_EXIT_CODES=1` already ON — cycle refuses start on doctor exit 2.
2. `ALPACA_SOAK_MODE_UNTIL=<deploy+7d>`: alert thresholds 2x tighter for 7 days post-deploy (>2% error alerts vs >5%); auto-reverts.
3. `daily_health_digest.js` includes data-provider summary for 7 days post-deploy.
4. Discord channel routing: data warnings → `#data-alerts`; cycle-critical → `#botjohn-log`.

### Feature-flag degradation (sub-rollback)

```
ALPACA_NEWS_INGEST=0           → disables news consumer; rest holds
ALPACA_OPTIONS_ARCHIVE=0       → suspends EOD archive; chain still pulled for signals
ALPACA_SOAK_MODE_UNTIL=<date>  → extend tightened thresholds
```

### Rollback ladder

```
LEVEL 1 — Feature-flag degradation (single subsystem misbehaving):
  Edit .env on VPS; systemctl restart johnbot.service. ≤30s.

LEVEL 2 — Targeted module revert:
  git revert <specific-file-commits> OR emergency forward-fix PR.
  Test Saturday; deploy same day if non-trading.

LEVEL 3 — Full PR revert (catastrophic):
  1. git revert <SP1-merge-SHA> on main, push.
  2. ssh VPS, git pull.
  3. Restore .env from /root/.env.pre-sp1.bak (pre-deploy backup).
  4. Re-subscribe Polygon Options Starter (operator action; new paid sub).
  5. systemctl restart johnbot.service.
  6. doctor.py --required-only — verify green.
  7. Migrations 109, 110 stay (append-only; old code ignores new columns).
  8. data_provider_health table stays.
  9. options_eod.parquet rows from Alpaca stay (tagged data_source='alpaca_aat_plus';
     mixed-source becomes a metadata concern, not a deletion concern).

LEVEL 4 — Partial recovery (PR reverted, Polygon not yet restored):
  Equity quotes work (free Polygon OR Alpaca P1 still in chain depending on
  revert state — verify reverted code's quote priority).
  Options chain UNAVAILABLE → strategies needing options return no signals.
  Existing positions still managed (executor + reconcile don't need data feeds).
  Daily cycle's options stage skipped via OPENCLAW_SKIP_OPTIONS_PHASE=1
  (existing flag, untouched by SP-1).
```

### Decision criteria

```
Level 1: any single-subsystem alert that doesn't block live trading
Level 2: one subsystem broken AND blocks live trading AND fix-forward
         faster than revert
Level 3: ≥2 subsystems broken; or one subsystem broken with no fix-forward
         in <2h; or doctor exits 2 and cause unclear after 30min triage
Level 4: Level 3 chosen but Polygon re-subscribe pending
```

### Pre-deploy operator checklist

```
[ ] Polygon Options Starter API key archived to /root/.env.pre-sp1.bak (BEFORE merge)
[ ] Massive credentials archived to same file
[ ] AAT Plus billing healthy and won't lapse in next 30 days
[ ] Saturday deploy window blocked off (no other infra changes)
[ ] Pre-deploy doctor run green on staging .env
[ ] FMP forward-earnings probe completed; lint allowlist set correctly
[ ] tests/test_cutover_smoke.py green locally + CI
[ ] Operator paging on (Discord #botjohn-log subscribed) for first Monday
[ ] Rollback procedure dry-run done at least once (revert applied + reverted on a branch)
[ ] Polygon dashboard re-subscribe flow screenshotted to confirm Level-3 SLA
```

### Open question for spec doc

Polygon Options Starter, once cancelled, may not be instantly re-subscribable on the same account. If re-subscribe requires API key regeneration or has >15 min SLA, Level 3 rollback timing changes — affects the Level 2 vs Level 3 threshold. Pre-merge operator task: screenshot the Polygon re-subscribe flow.

---

## 6. Testing + Validation

### Unit tests

```
tests/test_greeks_filter.py
  - Accepts ATM 30-DTE (full greeks)
  - Rejects 0-DTE expired (greeks=0, dte=0)
  - Rejects deep-ITM zero-volume (greeks=0, volume=0)
  - REJECTS + ALERTS on greeks=0 ATM 30-DTE (anomaly)
  - allow_zero_delta=True bypasses for 0-DTE strategies
  - data_provider_health counters increment

tests/test_alpaca_news.py
  - Mock CLI 5 articles → 5 inserts
  - Empty response → row with zero counts
  - 429 → exp-backoff × 3 → graceful degrade
  - Multi-ticker article counted per attributed ticker
  - JSONB top_headlines stores top-3 by |score|

tests/test_options_archive.py
  - Idempotency: re-run skips done tickers
  - Pagination: 3-page response concatenates
  - Dedupe: re-run no duplicates
  - Soft-budget abort: partial commit + alert
  - Concurrency: semaphore=8 honored
  - Universe-bounded: non-universe ticker skipped

tests/test_cboe_vol_indices.py
  - get_* return DataFrame with expected schema
  - Single transient error → retry succeeds
  - Two failures → raise; caller decides
  - CI lint module-as-sole-importer

tests/test_doctor_cutover.py
  - AAT Plus tier check passes/fails correctly
  - options_archive_freshness exit 1 at 2d, exit 2 at 4d
  - All checks <5s wall

tests/test_collector_cutover.py
  - Equity quote: Alpaca P1 succeeds → no FMP call
  - Equity quote: Alpaca 5xx → FMP fallback success
  - Options chain: greeks present, filter passes
  - OPTIONS_DATA_SOURCE branching absent
```

### Integration: tests/test_cutover_smoke.py

```
1. doctor.py --required-only --json — all green
2. PIPELINE_DRY_RUN=1 python3 -m execution.pipeline_orchestrator
   - collect ≤5min, returns universe-bounded chain + quotes + macro
   - sentiment ≤2min, alpaca_news_* populated
   - signals runs; no NaN/None propagation from greeks=0
   - handoff JSON valid
   - trade in DRY mode emits proposals only
   - alpaca/reconcile no real orders, no DB writes
   - report posts to dry-run channel
   - health digest includes data_provider_health
3. python3 -m pipeline.backfillers.alpaca_options --date $TODAY --dry-run
   - 5 tickers, /tmp parquet, schema valid
4. system_checks regression suite — pipeline/broker/regime tags pass
```

### CI gates

```
- pytest tests/ (full suite)
- pytest src/system_checks/
- node test/graph-smoke.js
- node test/paperhunter-smoke.js
- Pre-commit lints (yfinance, polygon, massive, OPTIONS_DATA_SOURCE)
- No regression in prior 1075+ tests
```

### Pre-deploy dry-run sequence (Saturday)

```
1. Apply migrations 109, 110 on prod DB
2. cp /root/openclaw/.env /root/.env.pre-sp1.bak (chmod 600)
3. Operator screenshots Polygon re-subscribe flow
4. Branch checkout + full pytest — green, no flake retries
5. git pull on VPS; systemctl restart johnbot.service
6. PIPELINE_DRY_RUN=1 python3 -m execution.pipeline_orchestrator --reason sp1-dry-run
   - Inspect logs for 200s, greeks rejection ≤5%/ticker, news for ≥5 of top-10
   - Doctor exit 0
7. python3 -m pipeline.backfillers.alpaca_options --date $LAST_TRADING_DAY
   - 200+ tickers, 100k+ rows, greeks present ≥95%
8. Operator approval: #data-alerts + #pipeline-feed silent/green → PR ready
   ABORT if anything unexpected; fix-forward before Monday.
```

### Sunday soak

```
- Saturday brain + Sunday paper-expansion run normally (no dependency on changes)
- Doctor cron continues
- #data-alerts monitored
- Anything red → investigate, fix-forward or pull PR before Monday open
```

### Monday live trading day 1

```
T-30min: doctor preflight exit 0; operator standby in #botjohn-log
T-0 (10:00 ET):
  - collect step in-time; SPY/AAPL/TSLA greeks sample populated
  - ticker_sentiment_daily.alpaca_* populated for ≥50% of universe
T+5min (signals):
  - signal count per strategy within ±30% of 7-day mean
  - Greeks-using strategies (S5, S15, S21, S_HV_*) not silently zero
T+15min (trade):
  - sizer + TradeJohn confirmer normal; no NaN/None proposals
T+25min (alpaca/reconcile):
  - Orders submit; fills reconcile correctly
T+30min (report):
  - daily_health_digest green per provider
16:30 ET: first scheduled options archive job
  - Wall ≤30min; 200+ tickers archived; greeks present ≥95%
  - #data-alerts: "options_eod archive complete..." summary

T+24h Tuesday: soak thresholds still tight
After 2 consecutive green Mondays: soak mode expires, thresholds revert.
```

### Light parity validation (no shadow)

```
1. Schema parity: existing options_eod.parquet (polygon_massive) + new rows
   (alpaca_aat_plus) share schema. data_source column tags the source.
2. Greeks-range sanity (test + daily anomaly report for week 1):
   delta ∈ [-1, 1], gamma ≥ 0, vega ≥ 0, theta ≤ 0 for long.
3. News volume comparison:
   Pre-cutover ticker_sentiment_daily.news_count_24h 30-day mean vs
   post-cutover alpaca_news_count_24h day-7. Expected: Alpaca ≥ pre-cutover.
   If <50%: investigate Alpaca news coverage gap.
4. Per-strategy signal-count regression in src/system_checks/ daily for 14 days.
   Fail if any strategy 7-day-avg drifts >50% from pre-cutover 30-day avg
   (excluding MONITORING/DEPRECATED).
```

### Out of scope (deferred to SP-2/3/4/5)

- Universe expansion (S&P 500 → Alpaca tradable universe) — SP-2
- Asset-class expansion (crypto, commodities, broader options) — SP-3
- New strategy archetypes leveraging news + new asset classes — SP-4
- Latency benchmarking + WebSocket streaming layer — SP-5
- Multi-asset backtest engine — SP-3

---

## 7. References

- SP-0 audit findings: this session's brainstorm transcript
- Memory: `feedback_alpaca_options_zero_greeks.md` (to be updated post-deploy)
- Memory: `feedback_silent_failure_pattern.md` (informs the no-synthetic-fallback decision)
- Memory: `project_av_purge_and_gates.md` (Polygon joins purged-providers)
- Memory: `reference_alpaca_cli.md` (CLI invocation patterns)
- CLAUDE.md core invariant: master parquets append-only
- Probe data: SPY/AAPL/GME chain probes on 2026-05-21 confirming greeks populate on AAT Plus
- Handoff doc: `2026-05-21-data-provider-overhaul-handoff.md` (SP-2/3/4/5)
