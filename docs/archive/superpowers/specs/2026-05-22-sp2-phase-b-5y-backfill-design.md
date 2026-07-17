# SP-2 Phase B: 5-Year Backfill — Design

**Spec date:** 2026-05-22
**Author:** BotJohn (extension of SP-2 umbrella)
**Status:** Pending operator review (cannot start before Phase A merges + 7-day soak)
**Parent:** `docs/superpowers/specs/2026-05-22-sp2-universe-expansion-design.md` §4.2
**Predecessors:** SP-2 Phase A (`feat/sp2-phase-a-universe-machinery`, PR #8)
**Branch:** `feat/sp2-phase-b-5y-backfill` (off main, after Phase A merges)

---

## 1. Context

Phase A landed predicate machinery against a *current* `ticker_metadata_snapshots` table — there is one row per ticker per day from `2026-05-22` onward. Backtests therefore see today's universe at every historical bar (acceptable for SP500 because membership is reasonably stable, **not acceptable** for any predicate keyed on `market_cap`, `adv_usd_20d`, `in_r1000`, or `in_r3000` because those vary materially).

Phase B writes the historical snapshot rows back to **2021-05-22** (5 years), plus extends the per-ticker price coverage to ~3,000 most-liquid US equities (Phase A's collector only loads the SP500 envelope per day; the master `prices.parquet` has 5y on the SP500 but is thin outside it).

Without Phase B, Phase C's universe-recs are meaningless because the candidate backtests would all see the same metadata at every bar.

### 1.1 Anchored facts (2026-05-22)

- Master parquets currently in `data/master/`:
  - `prices.parquet` — daily OHLCV, 5y × ~500 tickers (SP500 + benchmarks).
  - `options_eod.parquet` — daily contract-level EOD, ~30d × ~500 tickers (post-SP-1 self-archive started 2026-05-22).
  - `financials.parquet`, `macro.parquet`, `insider.parquet`, `earnings.parquet`, `prices_30m.parquet`, `historical_regimes.parquet` — orthogonal to this phase.
- `ticker_metadata_snapshots` (migration 111) — Phase A live writer started 2026-05-22; zero rows before that date.
- `data_quarantine` (migration 114) — Phase A landed it but Phase B is the first user.
- `data/.staging/` and `data/.checkpoints/` do **not** exist. Phase B creates them under the `.gitignore`'d `data/` tree.
- Rate limiter: `src/pipeline/rate_limiter.py` exposes the singleton `get_rate_limiter()` with `acquire("fmp")` / `acquire("alpaca")`. Phase B uses it; doesn't build its own.
- Provider plan budgets (verified 2026-05-22 via `provider_health` rows):
  - **FMP Starter** — 300 req/min hard, 250k req/day soft. Used for `historical-market-capitalization`, `profile`, `historical-price-full` (5y fallback if Alpaca thin).
  - **Alpaca AAT Plus** — 10,000 req/min hard. Used for `data stocks bars` (5y daily bars), `data option chain` (eligibility probe), `data option bars` (EOD self-archive).
- Existing backfill style: `scripts/backfill_options_eod_cutover_gap.py` — argparse, subprocess wrappers, idempotent append via `_append_parquet` keyed `(date, contract_symbol)`. Phase B mirrors this style.

### 1.2 Decisions locked

| Question | Decision |
|---|---|
| Targets in scope | `prices` (5y daily bars), `metadata` (monthly snapshots back to 2021-05-22 + intra-month interpolation for `in_sp500`/`in_r1000`/`in_r3000`), `options` (EOD bars for `options_eligible` subset, 90d back from 2026-05-22 only — full 5y is SP-3 scope). |
| Universe of tickers to backfill | "Top 3000 by current ADV (USD)" — derived from a one-shot probe at Phase B start. Frozen as `data/.backfill_universe_v1.txt`, never re-computed mid-run. |
| Chunking unit | `(ticker, year)` for prices; `(snapshot_month)` for metadata; `(date, ticker)` for options. Per-chunk Redis checkpoint. |
| Master-parquet write semantics | `pyarrow.parquet.write_to_dataset(..., existing_data_behavior='delete_matching')` partitioned by `(year, ticker)` — atomic at partition level. **Only used in PROMOTE step, never elsewhere.** This is the documented exception to append-only (CLAUDE.md core invariant) and is permitted **only when the affected `(date, symbol)` partition has zero existing rows** OR when the operator explicitly approves a v2 backfill via `OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1`. |
| Throttle posture | Conservative: FMP semaphore = 4 concurrent / 200ms inter-call; Alpaca semaphore = 12 concurrent / 80ms inter-call. Throttle envelope set so a backfill can run 24×7 without affecting live cycle. |
| Resumability | Redis-checkpointed per chunk: `backfill:5y:{target}:{ticker}:{year}` with `(status, started_at, rows, sha256, last_error)`. Re-run skips `status='promoted'`; retries `status ∈ {'failed', 'timeout', 'quarantined-cleared'}`. |
| Quarantine recovery | Master writes that slip past validation cannot be `DELETE`d (core invariant). The ONLY recovery path is `data_quarantine` rows + read-time consumer filtering, followed by `v2` re-fetch that writes a corrected row at the same `(date, symbol)` with `source_tag='backfill_5y_v2'` (requires `OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1`). |
| Discord channel | `#backfill-log` — daily digest + per-chunk failure alerts. Created by operator in Phase B preflight. |
| Estimated wall-clock | prices ≈ 5d, metadata ≈ 2d, options ≈ 3d. Sequential or parallel (operator choice via `--target`). |

---

## 2. Architecture

### 2.1 Stage → Validate → Promote loop

This is the single load-bearing invariant of Phase B. Bad rows never reach master parquet.

```
              ┌──────────────────────────────────────────────┐
              │  scripts/backfill_universe_5y.py             │
              │     --target {prices|metadata|options}       │
              │     --resume   (Redis-checkpoint aware)      │
              │     --dry-run  (no master writes)            │
              │     --tickers AAPL,MSFT (override)           │
              │     --years 2021,2022 (override)             │
              └──────────────────────────────────────────────┘
                            │
                            ▼
   ┌─────────────── STAGE ─────────────────┐
   │ Per (ticker, year) chunk:              │
   │   1. Redis SET backfill:5y:...         │
   │        status='in_progress', started_at│
   │   2. Fetch from provider (rate-limited)│
   │   3. Write parquet to                  │
   │        data/.staging/<job_id>/         │
   │   4. Increment data_provider_health    │
   └────────────────────────────────────────┘
                            │
                            ▼
   ┌─────────────── VALIDATE ──────────────┐
   │   schema_match(master)                 │
   │   row_count_plausible(ticker,year)     │
   │   no_null_pk()                         │
   │   date_range_in_window()               │
   │   spot_check_vs_live(5 random rows)    │
   │                                        │
   │   PASS → status='validated'            │
   │   FAIL → status='quarantined'          │
   │          + INSERT data_quarantine row  │
   │          + alert #backfill-log         │
   │          + SKIP promote                │
   └────────────────────────────────────────┘
                            │
                            ▼
   ┌─────────────── PROMOTE ───────────────┐
   │  pyarrow.parquet.write_to_dataset(    │
   │    staged_df,                          │
   │    master_path,                        │
   │    partition_cols=['year','symbol'],   │
   │    existing_data_behavior=             │
   │      'delete_matching',                │
   │  )                                     │
   │  source column tagged                  │
   │    'backfill_5y_v1'                    │
   │  Redis SET status='promoted'           │
   │  Remove staging file                   │
   └────────────────────────────────────────┘
```

### 2.2 Per-target flow

#### 2.2.1 `--target prices`

**Source:** Alpaca AAT Plus `data stocks bars --symbol <T> --timeframe 1Day --start <year-01-01> --end <year-12-31>` (rate-limited via `acquire("alpaca")`).

**Chunk key:** `(ticker, year)`. ~3000 tickers × 5 years = ~15,000 chunks. At 80ms/call + sequential per-ticker = ~20 min/year × 5 = ~1.7h theoretical; with 12-concurrent semaphore the wall is closer to ~3-5 days because each ticker fetch returns ~252 rows and the per-call overhead dominates. Plan for 5 days.

**Master invariant interaction:** `prices.parquet` is keyed `(symbol, date)`. Backfill writes ONLY where the `(symbol, date)` partition has zero rows for the target ticker. Existing SP500 prices stay untouched. Verified by:
```python
existing = pq.read_table(MASTER_PRICES, filters=[('symbol','=',T),('year','=',Y)], columns=['date']).to_pandas()
to_write = staged_df[~staged_df['date'].isin(existing['date'])]
```
If `len(to_write) < len(staged_df)`, write the diff only. Duplicate `(symbol, date)` rows ARE a validation failure — the `delete_matching` semantic must not silently overwrite SP500 history.

**Validation:**
- Schema: columns = `['symbol','date','open','high','low','close','volume','adj_close']`, dtypes exact match against master.
- Row count: `>= 200` trading days per full year unless `listed_date > year_start`.
- Nulls: no nulls in `(symbol, date, close)`.
- Date range: all dates within `[year-01-01, year-12-31]`.
- Spot check: 5 random `(symbol, date)` re-fetched via Alpaca single-bar call; `abs(staged.close - refetch.close) / refetch.close < 0.001`.

#### 2.2.2 `--target metadata`

**Source:** Composite per `(snapshot_month, ticker)`:
- `alpaca_tradable_universe.first_seen_at / last_seen_at` — listed/delisted status at month-end. Reconstructed from the daily snapshots captured 2026-04 onward; before that, the field is approximated by reverse-walking FMP `profile.ipoDate` and `delistedDate`.
- FMP `historical-market-capitalization?symbol=<T>&from=<month-end>&to=<month-end>` — `market_cap` at month-end. Phase A's probe (`docs/superpowers/specs/sp2-fmp-mktcap-probe.md`) confirmed this returns usable data for the Starter tier; if a per-ticker call 403s, fall back to `prices × shares_out` from FMP `profile`.
- `prices.parquet` (post 2.2.1 backfill) — derive `adv_usd_20d` from rolling 20-day mean of `adj_close × volume` ending at month-end. **Requires `prices` target to be promoted first.**
- FMP `profile` (cached weekly) — `sector`, `industry` (documented proxy: current values projected to history).
- Alpaca `data option chain --underlying-symbol <T> --limit 1` (cached per quarter) — `options_eligible` at the snapshot-month level. Probe is run at the boundary; not back-cast monthly within a quarter.
- `in_sp500` — derived from a static historical-membership list at `data/sp500_historical_membership_v1.csv` (one-shot author Task in Phase B; sourced from a single authoritative reference per the file's header) — falls back to *current* membership if the historical CSV is missing (documented bias, matches Phase A's default predicate behavior).
- `in_r1000` / `in_r3000` — computed at snapshot-write time as top-1000 / top-3000 by `market_cap` rank within the snapshot month, intersected with `tradable=True` and `status='active'`.

**Chunk key:** `(snapshot_month, ticker)` — but the natural batch is one full month at a time (~3000 tickers per month write) because `in_r1000`/`in_r3000` are a ranking operation that needs the full ticker set.

So:
- Stage = build a `pandas.DataFrame` for one snapshot month (one row per ticker).
- Validate at the month level: row count ~ 3000 ± 5%; market_cap distribution plausible (top-10 cap > $1T, median > $1B); no duplicates on `(snapshot_date, symbol)`.
- Promote = bulk `INSERT … ON CONFLICT (snapshot_date, symbol) DO NOTHING` (NOT `DO UPDATE` — historical snapshots are append-only; if a row exists at that month-end, it was already promoted and the re-run should noop).

**Resumability:** Redis key `backfill:5y:metadata:{YYYY-MM}` — single key per month; `status='promoted'` means the entire month-end snapshot has landed.

**Estimated wall:** 5y × 12 months × ~3000 tickers × ~2 FMP calls per ticker (market_cap + profile cache hit ratio ~85%) = ~25,000 FMP calls. At 4-concurrent / 200ms inter-call = ~21 min/month × 60 = ~21h. Plan for 2 days.

#### 2.2.3 `--target options`

**Scope reduction:** Full 5y options backfill is **out of scope for Phase B** (deferred to SP-3 alongside crypto/commodity expansion). Phase B backfills ONLY the gap between Polygon revocation (2026-05-XX, see SP-1 cutover) and Alpaca self-archive start (2026-05-22) — typically 0–14 days — for the `options_eligible` subset. This is the same shape as the already-shipped `scripts/backfill_options_eod_cutover_gap.py`; Phase B refactors that script into the `backfill_universe_5y.py --target options` mode for consistency but keeps the date window narrow.

**Chunk key:** `(date, ticker)`. Each chunk: enumerate today's chain for ticker, filter to contracts with `expiry >= date`, batch `alpaca data option bars` 100 contracts at a time for that date.

**Validation:** Reuses `scripts/backfill_options_eod_cutover_gap.py` validation (schema match against `options_eod.parquet`, no NaN greeks where filterable, OCC symbol parseability).

**Estimated wall:** ≤ 14 days × 3000 tickers × ~50 contracts each ≈ 2M alpaca calls. At 12-concurrent / 80ms = ~3.7h. Plan for 1 day with buffer.

### 2.3 Quarantine + recovery flow

The append-only invariant on master parquet means: **if a bad row gets PROMOTEd, we cannot delete it.** Validation is the only structural defense. When (not if) something slips:

```
1. Operator notices anomaly (dashboard spot, doctor regression, downstream NaN crash)
2. INSERT INTO data_quarantine
     (master_table='prices.parquet', symbol='XYZ', affected_date='2022-08-15',
      source_tag='backfill_5y_v1', reason='close price 12x reasonable',
      flagged_by='operator:OPERATOR')
3. All downstream consumers MUST filter quarantined (symbol, date) at read time:
   - src/strategies/universe_resolver.py.coverage_floor()
   - src/backtest/unified_backtest.py._load_prices()
   - src/pipeline/collector.js parquet reader path
4. To recover the data: re-run backfill with:
     OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1 \
       scripts/backfill_universe_5y.py \
         --target prices --tickers XYZ --years 2022 \
         --source-tag backfill_5y_v2 --supersede-quarantine
   - Stage + validate as normal
   - Promote step DELETEs (symbol, date) from master for the v1 source_tag (atomic write_to_dataset)
   - data_quarantine.superseded_by_source_tag = 'backfill_5y_v2', superseded_at = NOW()
```

Phase B Task 9 covers the consumer-side quarantine filter wiring. Without that wiring, a quarantine row exists in the DB but is structurally invisible — defeating the whole point.

### 2.4 Doctor + system_checks additions

```
src/maintenance/doctor.py
  + _check_backfill_progress (slow=True)
    Reads Redis backfill:5y:* keys, counts status distribution.
    WARN if quarantined > 0; FAIL if quarantined > 100 (data poisoning indicator).
  + _check_backfill_universe_coverage (slow=True)
    Reads ticker_metadata_snapshots row count per (year, month).
    WARN if any month back to 2021-05 has < 2500 rows (≥ 3000 expected ± 15%).

src/system_checks/checks/backfill_progress.py
  @check(name='backfill_progress', tags=['storage','strategies'], requires=['db','redis'])
  Returns (PASS, "all chunks promoted") | (WARN, "<N> quarantined") | (FAIL, "no progress for >24h while in_progress > 0")

src/system_checks/checks/ticker_metadata_history_depth.py
  @check(name='ticker_metadata_history_depth', tags=['strategies'], requires=['db'])
  Returns (PASS, "5y depth confirmed") if min(snapshot_date) <= 2021-06-01
       | (WARN, "depth <X months>") otherwise.
```

### 2.5 Dashboard tiles

```
src/channels/dashboard/server.js (:7870 operator)
  + GET /api/backfill-progress
    SELECT … FROM Redis (via shared cache adapter) GROUP BY (target, status)
    UI tile: stacked bar (in_progress / validated / promoted / quarantined) per target,
             updated 30s SSE.

src/channels/api/server.js (:3000 user dashboard) — Data Health tab
  + GET /api/pipelines/backfill-history
    SELECT min(snapshot_date), max(snapshot_date), count(*) FROM ticker_metadata_snapshots
    GROUP BY date_trunc('month', snapshot_date) ORDER BY 1
    UI panel: timeline of monthly snapshot row counts (target ~3000/month).
```

---

## 3. Components

### 3.1 New files

```
scripts/backfill_universe_5y.py                          ~700 LoC
  Argparse driver: --target {prices|metadata|options}
                   --resume / --dry-run / --tickers / --years
                   --source-tag (default 'backfill_5y_v1')
                   --supersede-quarantine (paired with overwrite gate)
  Stage / Validate / Promote loops per target.
  Redis checkpointing via src/database/redis.py (existing shared client).
  Throttling via src/pipeline/rate_limiter.py.
  Daily digest writer to #backfill-log (uses src/channels/discord/notify.js endpoint).

scripts/build_backfill_universe.py                       ~80 LoC
  One-shot probe: fetches alpaca_tradable_universe WHERE active+tradable,
  ranks by current ADV (computed from prices.parquet) and writes top-3000
  to data/.backfill_universe_v1.txt. Frozen artifact; committed to git.

scripts/probe_sp500_historical_membership.py             ~60 LoC
  One-shot: writes data/sp500_historical_membership_v1.csv from an
  authoritative source (Wikipedia CSV scrape with date stamps). Single file
  header documents the source URL + scrape date. Committed.

src/pipeline/backfillers/universe_metadata.py            ~300 LoC
  Pure module (no CLI). build_month_snapshot(snapshot_date) -> pd.DataFrame.
  Used by backfill_universe_5y.py and by ticker_metadata_writer.py
  (so today's row is constructed by the same code path as historical rows).

src/pipeline/backfillers/universe_prices.py              ~200 LoC
  Pure module. fetch_ticker_year(symbol, year) -> pd.DataFrame.

src/pipeline/quarantine_filter.py                        ~80 LoC
  Read-time filter shared by all parquet consumers:
    filter_quarantined(df, master_table) -> df_clean
  Caches the data_quarantine set per (master_table) for 5min.

src/database/migrations/115_backfill_audit.sql           ~30 LoC
  Append-only audit log of every (target, chunk_key, status, started_at, ended_at,
  rows_written, source_tag, error_text) — durable record of every chunk
  Redis ever held. Survives Redis flush.

tests/test_backfill_universe_5y.py                       ~250 LoC
tests/test_quarantine_filter.py                          ~150 LoC
tests/test_universe_metadata_builder.py                  ~180 LoC
tests/test_backfill_idempotency.py                       ~150 LoC

src/system_checks/checks/backfill_progress.py            ~70 LoC
src/system_checks/checks/ticker_metadata_history_depth.py ~50 LoC

docs/sp2-backfill-runbook.md                             ~150 LoC
  Operator runbook: pre-flight, kick-off command, monitoring, what to do
  on quarantine, recovery sequence.
```

### 3.2 Modified files

```
src/pipeline/ticker_metadata_writer.py
  - Extract today's snapshot construction into universe_metadata.build_month_snapshot
    (DRY with backfill). Behavior preserved bit-for-bit.

src/strategies/universe_resolver.py
  - coverage_floor() filters out quarantined (symbol, date) via quarantine_filter.

src/backtest/unified_backtest.py
  - _load_prices() filters quarantined.

src/backtest/regime_blended_backtest.py
src/backtest/quick_backtest.py
src/backtest/intraday_regime_backtest.py
src/backtest/regime_performance_analyzer.py
  - Same _load_prices filter.

src/pipeline/collector.js
  - On parquet read path, applies quarantine filter via Python subprocess
    (src/pipeline/quarantine_filter.py --json --table prices.parquet).
    Cached in-process for cycle duration.

src/channels/dashboard/server.js          # add /api/backfill-progress tile
src/channels/api/server.js                # add backfill-history panel
src/channels/api/routes_pipelines.js      # add /backfill-history route

src/maintenance/doctor.py
  + _check_backfill_progress (slow=True)
  + _check_backfill_universe_coverage (slow=True)

src/system_checks/checks/__init__.py
  + import backfill_progress
  + import ticker_metadata_history_depth
```

### 3.3 `.env` changes

```
ADD:
  OPENCLAW_BACKFILL_5Y_ACTIVE=0           # gate the backfill driver from accidentally running in cron
  OPENCLAW_BACKFILL_ALLOW_OVERWRITE=0     # safety gate on --source-tag v2+ recovery
  BACKFILL_FMP_CONCURRENCY=4
  BACKFILL_FMP_INTERVAL_MS=200
  BACKFILL_ALPACA_CONCURRENCY=12
  BACKFILL_ALPACA_INTERVAL_MS=80
  DISCORD_BACKFILL_LOG_WEBHOOK=...        # one-time operator paste

REMOVE: (none)
```

### 3.4 Schema (migration 115)

```sql
-- 115_backfill_audit.sql
-- Append-only durable log of every backfill chunk attempt.
-- Redis is for ops; this is for forensics + audit.

CREATE TABLE IF NOT EXISTS backfill_audit (
  id            BIGSERIAL PRIMARY KEY,
  target        TEXT NOT NULL,                    -- 'prices' | 'metadata' | 'options'
  chunk_key     TEXT NOT NULL,                    -- canonical: 'AAPL:2022' or '2022-08:metadata'
  started_at    TIMESTAMPTZ NOT NULL,
  ended_at      TIMESTAMPTZ,
  status        TEXT NOT NULL,                    -- 'in_progress'|'validated'|'promoted'|'quarantined'|'failed'
  rows_written  INTEGER,
  source_tag    TEXT NOT NULL,                    -- 'backfill_5y_v1' default
  sha256        TEXT,                             -- of the staged parquet (validation hash)
  error_text    TEXT,
  CONSTRAINT backfill_audit_chunk_status UNIQUE (target, chunk_key, source_tag, started_at)
);
CREATE INDEX idx_backfill_audit_status ON backfill_audit(target, status);
CREATE INDEX idx_backfill_audit_recent ON backfill_audit(started_at DESC);
```

### 3.5 Memory + docs updates

```
/root/.claude/projects/-root/memory/project_sp2_phase_b_backfill.md  (NEW after shipped)
  Phase B shipped state, staging-validate-promote pattern, quarantine recovery flow.

/root/.claude/projects/-root/memory/feedback_master_parquet_append_only.md (UPDATE existing
  feedback-never-delete-master-data.md — add Phase B's documented exception:
  delete_matching only legal in PROMOTE step on zero-existing partitions; v2+
  requires OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1 + quarantine row supersede).

/root/.claude/projects/-root/memory/MEMORY.md   index update.

CLAUDE.md "Recent Changes"  SP-2 Phase B entry post-deploy.

ARCHITECTURE.md  Section "Per-Strategy Universe Resolution" extended with
  historical-snapshot back-depth + quarantine recovery flow.

docs/sp2-backfill-runbook.md  Operator runbook (referenced in Task list).
```

---

## 4. Data Flow

### 4.1 Initial backfill (one-shot operator kickoff)

```
T=0  Operator runs preflight checklist (§6.3 of this spec).
T+5min  scripts/build_backfill_universe.py → data/.backfill_universe_v1.txt
                                              (commit to git, push, deploy)
T+10min scripts/probe_sp500_historical_membership.py → data/sp500_historical_membership_v1.csv
                                              (commit, push, deploy)
T+15min Operator kickoff:
   nohup python3 scripts/backfill_universe_5y.py --target prices --resume \
     > /var/log/backfill_5y_prices.log 2>&1 &
   (after prices completes: --target metadata)
   (after metadata completes: --target options if desired this cycle, otherwise defer)

T+5d  Prices done; verify via doctor _check_backfill_progress, no quarantines.
T+7d  Metadata done; verify ticker_metadata_history_depth check.
T+8d  Options gap closed (if run).
T+9d  Operator runs Phase C readiness check: spot 50 random (ticker, month)
       and confirm point-in-time correctness against an independent reference.
```

### 4.2 Day-N (steady state, Phase B shipped)

```
06:35 ET  ticker_metadata_writer (daily, Phase A)
          NEW: now uses universe_metadata.build_month_snapshot in shared mode
               (date=today). Behavior preserved.

No new cron jobs. Phase B's backfill driver is operator-invoked; not a timer.
Doctor checks run on every cycle and surface quarantine drift.
```

### 4.3 Resume / partial-failure flow

```
1. Backfill stops (operator Ctrl-C, VPS reboot, OOM).
2. Re-invocation with --resume:
     - For each (target, chunk_key) in the target's full chunk plan:
         redis_status = GET backfill:5y:{target}:{chunk_key}
         if redis_status == 'promoted': skip
         if redis_status == 'quarantined': skip (manual review)
         if redis_status == 'in_progress' AND started_at older than 1h: re-stage
         else: re-stage
3. Promote step is idempotent at master-parquet level (delete_matching on zero-existing).
4. Audit log records every attempt; orphaned 'in_progress' rows are flagged
   by _check_backfill_progress (no progress >24h).
```

---

## 5. Phase Ordering Within Phase B

This phase has a hard ordering between targets because `metadata` depends on `prices` for `adv_usd_20d`.

```
1. Build backfill universe (data/.backfill_universe_v1.txt)            (Task 1)
2. SP500 historical membership CSV                                      (Task 2)
3. Schema migration 115 + quarantine filter wiring                      (Task 3-5)
4. Backfill driver scaffolding (argparse, Redis ckpt, throttle)        (Task 6)
5. --target prices implementation + tests                               (Task 7)
6. --target metadata implementation (uses prices) + tests               (Task 8)
7. --target options (refactor from gap script) + tests                  (Task 9)
8. Doctor + system_checks + dashboard tiles                             (Task 10-11)
9. Smoke + runbook + docs/memory                                        (Task 12-13)
10. PR + operator one-shot backfill kickoff                             (Task 14)
```

---

## 6. Error Handling + Rollback

### 6.1 Failure-mode matrix

| Failure | Detection | Response | Severity |
|---|---|---|---|
| Provider 429 / 5xx during STAGE | per-chunk try/except + exponential backoff (1s/5s/30s/300s) | Chunk re-queued; after 4 attempts → `status='failed'`; #backfill-log alert. | LOW |
| Schema mismatch in staged parquet | VALIDATE step | Chunk `status='quarantined'`; `data_quarantine` row written (covering the staged date range with reason='schema_mismatch'); SKIP promote. | MEDIUM |
| Row count plausibility fail | VALIDATE step | Same as schema mismatch — quarantine. | MEDIUM |
| Spot-check live-vs-staged > 0.1% delta | VALIDATE step | Same as schema mismatch — quarantine. | HIGH |
| Promote writes a bad row that slipped validation | Per-cycle anomaly check OR downstream NaN crash | **NOT REVERSIBLE.** Operator runs quarantine recovery (§2.3). | HIGH (latent) |
| Redis flush mid-run | re-invocation rescans from chunk 0 | Idempotent re-promote (delete_matching on zero-existing is noop). Audit log preserves history. | LOW |
| Disk pressure during STAGE | Per-chunk free-space precheck (fail if `<5 GB` free) | Driver halts cleanly; alert; operator clears. | LOW |
| FMP day-budget exhausted | Driver detects 429 + quota header | Driver pauses 6h, resumes; alert. | LOW |
| `prices` target promoted but `metadata` started prematurely (operator error) | `metadata` startup precondition check | Driver refuses to start; clear error message. | LOW |

### 6.2 Rollback ladder

```
LEVEL 1 — Halt the backfill, no data changes
  kill <pid_of_backfill_5y>
  Redis chunks frozen in their current state.
  System unaffected (backfill is operator-invoked, not on a timer).
  Wall: ≤5s. Reversible.

LEVEL 2 — Quarantine identified bad rows
  See §2.3 recovery flow.
  Wall: minutes per affected (symbol, date) pair.

LEVEL 3 — Disable consumers from reading backfilled data entirely
  Set OPENCLAW_BACKFILL_5Y_ACTIVE=0 and a NEW companion gate
    OPENCLAW_PARQUET_FILTER_BACKFILL_ROWS=1
  src/pipeline/quarantine_filter.py treats ALL rows with source_tag LIKE 'backfill_5y_%'
    as quarantined at read time. System reverts to pre-Phase-B coverage
    (SP500 prices + post-Phase-A live snapshots only).
  Wall: ≤30s (env flip + restart).

LEVEL 4 — Full rebuild from scratch
  After fix:
    DELETE FROM backfill_audit;
    Redis FLUSHDB on the backfill: namespace
    Run driver with --source-tag backfill_5y_v2 + OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1
  Wall: full 5-7 days as initial run.
```

### 6.3 Pre-deploy operator checklist

```
[ ] Phase A in production ≥ 7 days, no resolver incidents
[ ] PR #8 merged + deployed
[ ] Disk: at least 40 GB free under /root/openclaw/data/ (backfill projection
        ≈ 30 GB across all targets)
[ ] FMP Starter daily-quota headroom confirmed (current usage < 50% of 250k/day)
[ ] Alpaca AAT Plus min-tier check (alpaca account info shows algo_trader_plus)
[ ] Redis reachable: redis-cli PING == PONG
[ ] data/.backfill_universe_v1.txt exists and is committed (Task 1)
[ ] data/sp500_historical_membership_v1.csv exists and is committed (Task 2)
[ ] Discord #backfill-log channel created; DISCORD_BACKFILL_LOG_WEBHOOK set
[ ] Dry-run backfill on 50 tickers × 1 year completes cleanly:
       scripts/backfill_universe_5y.py --target prices --dry-run \
         --tickers AAPL,MSFT,...50 \
         --years 2025
[ ] Doctor preflight green
[ ] Operator declares maintenance window in #general (backfill runs background;
    live cycle unaffected, but bandwidth/CPU spike visible)
```

---

## 7. Testing + Validation

### 7.1 Unit tests

```
tests/test_universe_metadata_builder.py
  - build_month_snapshot returns one row per ticker in universe
  - market_cap nullable (None) when FMP returns 404, doesn't crash
  - in_r1000/in_r3000 ranks computed correctly on fixture
  - DRY check: today's row written by writer matches output of build_month_snapshot(today)

tests/test_backfill_universe_5y.py
  - --dry-run never writes to master parquet
  - --resume skips promoted chunks
  - Quarantined chunk does NOT promote on re-run unless source_tag changes
  - Schema validation rejects mismatched column dtypes
  - Spot-check rejects rows with > 0.1% live delta
  - Promote step refuses overwrite without OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1
  - audit row written for every chunk attempt

tests/test_backfill_idempotency.py
  - Re-run same Redis state → noop master writes
  - Failed chunk re-runs cleanly with reset status
  - Promote to existing partition (zero-existing) succeeds
  - Promote to existing partition (non-zero existing) refuses (raises) without overwrite gate

tests/test_quarantine_filter.py
  - filter_quarantined drops (symbol, date) pairs marked quarantined
  - superseded_at != NULL → row not filtered (recovery applied)
  - 5-min cache invalidates after TTL
  - All five backtest engines integrate filter (parametrized test)
```

### 7.2 Integration: tests/test_sp2_phase_b_smoke.py

```
1. doctor.py --required-only --json — all green including new checks
2. scripts/backfill_universe_5y.py --target prices --tickers AAPL --years 2024 --dry-run
   - 1 ticker, 1 staging file, no master writes, audit row written
3. Inject a quarantine row for (AAPL, 2024-08-15); verify:
   - resolver.coverage_floor(AAPL, 2024-08-15) returns False
   - unified_backtest._load_prices skips the row
   - collector.js startup logs "quarantine filter loaded N rows"
4. system_checks --tag storage --check backfill_progress → PASS on fresh state
5. system_checks --tag strategies --check ticker_metadata_history_depth → WARN
   on a fresh DB with no historical snapshots
```

### 7.3 Pre-deploy soak (Phase B)

Phase B has two soak phases:

**Soak A: code-deploy only (no backfill yet)** — 3 days.
- Doctor + system_checks green.
- Quarantine filter loaded; no rows filtered (empty quarantine).
- Live cycle latency unchanged ± 5%.

**Soak B: backfill run + post-completion** — 14 days.
- Daily digest in #backfill-log.
- Quarantine count ≤ 50 rows total across all targets.
- Spot-audit by operator: 20 random `(symbol, date)` rows compared against an authoritative reference, < 1 mismatch acceptable.
- Phase C readiness gate: `ticker_metadata_history_depth` reports PASS at `≥ 5y`.

If any soak criterion fails 2 days running, rollback ladder Level 3 (disable filter).

### 7.4 Out of scope for Phase B

- Full 5y options-EOD backfill (~SP-3 alongside crypto/commodity expansion).
- Tick-level data (out of provider scope on AAT Plus).
- Fundamentals back-depth beyond what `financials.parquet` already holds.
- Re-fetching prices that already exist in master from a different source (would violate append-only).
- Resampled bar generation (`prices_30m.parquet`) — orthogonal pipeline.
- Wikipedia historical SP500 membership scraper as a recurring job — one-shot only.

---

## 8. References

- Parent spec: `docs/superpowers/specs/2026-05-22-sp2-universe-expansion-design.md` §4.2 + §6.1 (backfill failure mode) + §7.4 (Phase B validation)
- Phase A plan: `docs/superpowers/plans/2026-05-22-sp2-phase-a-universe-machinery.md`
- Phase A spec: `docs/superpowers/specs/2026-05-22-sp2-universe-expansion-design.md` §3.4 (migrations 111-114, including 114 `data_quarantine` which Phase B is the first user of)
- Existing rate limiter: `src/pipeline/rate_limiter.py` (singleton, asyncio)
- Existing backfill style reference: `scripts/backfill_options_eod_cutover_gap.py`
- Master parquet helper: `src/pipeline/backfillers/alpaca_options.py:_append_parquet`
- CLAUDE.md core invariant: master parquets append-only (Phase B's `delete_matching` exception documented above is the only permitted relaxation)
- Memory: `feedback_never_delete_master_data.md` (will be UPDATED to document the exception)
- Memory: `feedback_silent_failure_pattern.md` (split-source freshness applies to quarantine filter cache TTL)
- Memory: `feedback_lifecycle_silent_strip.md` (regression — make sure any new StrategyRecord fields land in all three lockstep sites if added)
- Memory: `feedback_universe_predicate_contract.md` (Phase A — predicates must not read clock; Phase B's historical snapshots are what makes that contract meaningful)
