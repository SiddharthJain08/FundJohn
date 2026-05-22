# SP-2: Universe Expansion — Design

**Spec date:** 2026-05-22
**Author:** BotJohn brainstorm session
**Status:** Pending operator review
**Parent program:** Data-provider overhaul (5 sub-projects: SP-1 through SP-5)
**Predecessors:** SP-1 (provider cutover, shipped 2026-05-22)
**Scope:** SP-2 only. SP-3 (asset-class expansion) and SP-4 (research uplift) build on this.

---

## 1. Context

Post-SP-1 the system trades a hardcoded ~500-ticker S&P 500 universe (`src/pipeline/universe.js`).
The Alpaca AAT Plus subscription delivers data on ~8,000+ US equities, and the operator's
intent is to let each strategy carve out its own *profitable* slice of that universe — not
to flip the whole codebase to a fixed broader index. The collector should ingest the
**union of all live strategies' resolved universes** and nothing more (minimum-maximum
information principle), so cost and storage scale with what strategies actually need.

The mechanism is a Python predicate per strategy, evaluated point-in-time against historical
ticker metadata for backtests and against today's snapshot for live trading. Existing
strategies start on a `in_sp500` default predicate (byte-identical behavior post-deploy)
and migrate to explicit profit-optimal predicates via a Mastermind-driven re-evaluation
pass. New strategies emit a predicate at creation time via the PaperHunter/StrategyCoder
research workflow.

### Anchored facts (2026-05-22 system state)

- 51 live + 51 candidate strategies in `src/strategies/manifest.json`.
- `alpaca_tradable_universe` (migration 092) refreshed daily by
  `src/maintenance/refresh_tradable_universe.py` — Alpaca's broker-broad asset list
  with `(symbol, asset_class, exchange, status, tradable, shortable, marginable,
  fractionable, easy_to_borrow, first_seen_at, last_seen_at)`.
- `universe_config` table is operator-curated `(ticker, name, category, active)` for
  dashboard display — survives SP-2 unchanged.
- `universe.js` hardcoded SP500 + benchmarks + sector ETFs — survives as the **fallback
  default for the `in_sp500` predicate** and as the benchmark/sector source for
  regime modelling.
- `StrategyRecord` dataclass at `src/strategies/lifecycle.py:103`; `from_manifest` at
  `:153`; `to_dict` at `:480`. All three update sites must change in lockstep with any
  new top-level field (`feedback_lifecycle_silent_strip.md` — May-12 incident).
- Latest migration: 110 (`data_provider_health`, SP-1). Next: 111, 112, 113.
- `unified_backtest.py` currently receives a universe at start-of-run; it does NOT
  re-resolve per bar. This is the biggest look-ahead surface SP-2 must close.
- Master parquet append-only invariant (CLAUDE.md core invariant): no
  delete/drop/truncate paths allowed on `prices.parquet`, `options_eod.parquet`,
  `financials.parquet`, `macro.parquet`, `insider.parquet`, `earnings.parquet`,
  `prices_30m.parquet`, `historical_regimes.parquet`. Bad backfill cannot be
  retroactively removed — only flagged.

### Decisions locked in brainstorm (2026-05-22)

| Question | Decision |
|---|---|
| Universe ceiling | **Liquidity-defined (no fixed ceiling)**. Universe = whatever passes per-strategy predicates against current/historical metadata snapshots. |
| Slice contract | **Python predicate per strategy** — `universe_filter(meta: TickerMetadata, as_of: date) -> bool`. No central enum/dict. |
| Backfill scope | **5 years × ~3,000 most-liquid tickers** for `prices.parquet` + monthly `ticker_metadata_snapshots`. Options EOD self-archive only for options-eligible subset. |
| Existing-strategy migration | **Default-preserve + opt-in upgrade**. Phase A injects default predicate (`meta.in_sp500`) — behavior byte-identical. Phase C re-evaluates explicit predicates per strategy. |
| Re-eval mechanism | **Mastermind-driven** (new `mode=universe-recs`, Opus 4.7 1M, Saturday). Opus picks from a **finite candidate-predicate set (~12 well-known slices)** that is pre-backtested per strategy — bounded blast radius, fully auditable. |

---

## 2. Architecture

### 2.1 The predicate

```python
# Signature every strategy implements (or inherits default)
def universe_filter(meta: TickerMetadata, as_of: date) -> bool: ...

# Default for the 102 existing strategies (lifecycle auto-injects on first read
# when universe_filter_ref is None)
DEFAULT_UNIVERSE_FILTER = lambda meta, _as_of: bool(meta.in_sp500)
```

**`TickerMetadata` frozen dataclass** (`src/strategies/universe_meta.py`):

| Field | Type | Source | Notes |
|---|---|---|---|
| `symbol` | str | `alpaca_tradable_universe.symbol` | PK |
| `asset_class` | str | `alpaca_tradable_universe.asset_class` | `us_equity` for SP-2 (others = SP-3) |
| `exchange` | str | `alpaca_tradable_universe.exchange` | NYSE, NASDAQ, ARCA, ... |
| `status` | str | `alpaca_tradable_universe.status` | `active` only enters resolver |
| `tradable` | bool | `alpaca_tradable_universe.tradable` | predicate filter input |
| `shortable` | bool | `alpaca_tradable_universe.shortable` | predicate filter input |
| `fractionable` | bool | `alpaca_tradable_universe.fractionable` | predicate filter input |
| `easy_to_borrow` | bool | `alpaca_tradable_universe.easy_to_borrow` | predicate filter input |
| `market_cap` | numeric \| None | **FMP `historical-market-capitalization`** (verified by `scripts/probe_fmp_historical_market_cap.py` as a Phase A precondition); fallback: `prices.parquet[adj_close] × shares-outstanding` from FMP `profile` | nullable; predicates that require it must handle None |
| `adv_usd_20d` | numeric \| None | **Derived from `prices.parquet`** (rolling 20-day mean of `adj_close × volume`) | Implies prices backfill must complete before ADV materializes |
| `sector` | str \| None | **FMP `profile` (current) projected to history** | DOCUMENTED PROXY: sector classification is current-state for all historical dates. Sector-rotation strategies must accept this proxy; switching to EDGAR SIC codes is a future enhancement (out of scope for SP-2). |
| `industry` | str \| None | Same caveat as sector | |
| `options_eligible` | bool | Alpaca chain probe (cached weekly): `alpaca data option chain --underlying-symbol <T> --limit 1` returns ≥1 contract within 60 DTE | Boolean cached in snapshot row |
| `in_sp500` | bool | **Hardcoded list in `universe.js` projected to history** | DOCUMENTED PROXY: today's SP500 list applied retroactively. Acceptable bias for the default predicate since the alternative (Wikipedia historical lists) is messy and the default's only purpose is byte-identical Phase A behavior. |
| `in_r1000` | bool | Constructed: top-1000 by market_cap at snapshot date | Computed at snapshot-write time |
| `in_r3000` | bool | Constructed: top-3000 by market_cap at snapshot date | Computed at snapshot-write time |
| `listed_date` | date \| None | `alpaca_tradable_universe.first_seen_at` (proxy; true IPO date is FMP `profile.ipoDate`) | |
| `delisted_date` | date \| None | Set when `alpaca_tradable_universe.status` flips to `inactive`; uses last `last_seen_at` | |

**Field-by-field source discipline:** if a field has no defensible source on the
current provider stack, it does NOT enter `TickerMetadata`. The table above is the
exhaustive set for SP-2. SP-3 may add `crypto_pair`, `asset_class='crypto'`, etc.

### 2.2 Look-ahead defense (the contract)

A predicate that reads "today" instead of `as_of` produces look-ahead bias in backtests.
SP-2 enforces correctness via a layered contract:

1. **Signature lint** (`scripts/lint_universe_predicates.py`, GHA blocking):
   the predicate's callable must have exactly two parameters named `meta` and `as_of`.
2. **Import lint**: the strategy module section containing `universe_filter` cannot
   import `datetime.date.today`, `datetime.datetime.now`, `datetime.datetime.utcnow`,
   `time.time`, `os.environ`, or any module that re-exports them. AST scan + first-order
   callee scan. (Closures or method calls inside `meta`/`as_of`-derived values are fine.)
3. **Sandbox check at lifecycle promotion**: when a strategy's predicate changes,
   `LifecycleStateMachine.transition` evaluates the predicate twice — once with the
   actual system clock and once with `freezegun` set to 2020-01-01 — against a fixed
   `TickerMetadata` fixture. If results differ, transition refuses with a structured
   error.
4. **Resolver-level `as_of` ceiling**: `UniverseResolver.resolve(strategy_id, as_of)`
   refuses any `as_of > today()`. Backtest loops cannot accidentally pass future dates.

### 2.3 `UniverseResolver` service

`src/strategies/universe_resolver.py`:

```python
class UniverseResolver:
    def __init__(self, db_conn, prices_parquet, options_parquet):
        self._cache: dict[tuple[str, date], list[str]] = {}

    def resolve(self, strategy_id: str, as_of: date) -> list[str]:
        """Apply strategy's predicate to ticker_metadata_snapshots ≤ as_of.
        Filters out tickers without sufficient price coverage.
        Caches per (strategy_id, as_of)."""

    def union_universe(self, as_of: date, states: tuple[str, ...] = ("live",)) -> list[str]:
        """Union across all strategies in given lifecycle states.
        Drives collector budget."""

    def coverage_floor(self, ticker: str, as_of: date) -> bool:
        """True if prices.parquet has ≥ MIN_BARS_FOR_INCLUSION (default: 60)
        bars before as_of for ticker. Resolver excludes tickers that fail."""
```

Coverage gating prevents the predicate from returning tickers that would crash
backtests on missing data — a silent-failure prevention layer.

### 2.4 `union_universe` is the system's information envelope

| Consumer | Pre-SP-2 (today) | Post-SP-2 |
|---|---|---|
| `src/pipeline/collector.js` | `getUniverse('all')` — SP500+benchmarks+sectors | `union_universe(today(), states=("live",)) ∪ BENCHMARKS ∪ SECTOR_ETFS` |
| `src/pipeline/backfillers/alpaca_options.py` (SP-1) | Iterates `alpaca_tradable_universe WHERE active` | Iterates `union_universe(today(), states=("live",))` filtered to `options_eligible=True` |
| `src/ingestion/alpaca_news.py` (SP-1) | Iterates active universe | Iterates `union_universe(today(), states=("live",))` |
| `src/pipeline/run_sentiment_step.py` | SP500 | `union_universe(today(), states=("live", "candidate"))` (candidates get sentiment for backtest fidelity, but no orders) |
| `src/agent/graphs/daily-cycle.js` | passes hardcoded universe to signals | passes resolved per-strategy universe to each strategy's signal generator |
| `src/backtest/unified_backtest.py` | universe fixed at run-start | resolves per-bar (Section 2.5) |

Benchmarks (SPY/QQQ/IWM/DIA) and sector ETFs (XL*) are **always collected** regardless
of predicate output — regime/HMM models depend on them.

### 2.5 Backtest engine `as_of` integration

This is the largest look-ahead surface and SP-2 closes it explicitly.

`src/backtest/unified_backtest.py` currently shape:

```python
def run(strategy, start, end, universe):  # universe fixed at start
    for bar_date in trading_days(start, end):
        signals = strategy.generate(bar_date, universe)
        ...
```

Post-SP-2 shape:

```python
def run(strategy, start, end, resolver: UniverseResolver | None = None):
    if resolver is None:
        resolver = UniverseResolver(...)  # production default
    for bar_date in trading_days(start, end):
        universe = resolver.resolve(strategy.id, as_of=bar_date)
        signals = strategy.generate(bar_date, universe)
        ...
```

Backtests now get a different universe per bar — matching exactly what
LIVE would see if rolled back in time. Strategies that hardcode tickers internally
(today: most of them) keep working since the resolver returns a list and they
intersect/use it as they did before.

`regime_blended_backtest.py`, `quick_backtest.py`, `intraday_regime_backtest.py`,
and `regime_performance_analyzer.py` all share the same loop pattern — Phase A
touches all four sites.

---

## 3. Components

### 3.1 New files (Phase A)

```
src/strategies/universe_meta.py                          ~80 LoC
  TickerMetadata frozen dataclass; from_row(row) loader.

src/strategies/universe_resolver.py                      ~250 LoC
  UniverseResolver.resolve(), union_universe(), coverage_floor().
  In-memory LRU cache. Pulls from ticker_metadata_snapshots.

src/strategies/universe_default.py                       ~30 LoC
  DEFAULT_UNIVERSE_FILTER = lambda meta, _as_of: bool(meta.in_sp500)
  Plus 12 canonical candidate predicates (sp500, r1000, r3000,
  options_eligible_only, large_cap, mid_cap, small_cap_liquid,
  large_cap_options, mid_cap_options, no_adr, no_otc, top500_by_adv).
  Used by Phase C Mastermind candidate set.

src/strategies/universe_lint.py                          ~150 LoC
  AST scanner: signature check + import ban for the predicate module.

scripts/lint_universe_predicates.py                      ~40 LoC
  CLI wrapper for CI gate.

src/pipeline/ticker_metadata_writer.py                   ~200 LoC
  Daily writer (post-cycle, 18:30 ET) that constructs today's
  TickerMetadata row from alpaca_tradable_universe + FMP profile cache
  + prices.parquet (for adv_usd_20d) + options_eligible probe cache,
  upserts into ticker_metadata_snapshots.

src/database/migrations/111_ticker_metadata_snapshots.sql
src/database/migrations/112_strategy_universe_recommendations.sql
src/database/migrations/113_universe_resolution_audit.sql
src/database/migrations/114_data_quarantine.sql
  See §3.4 for schemas.

scripts/probe_fmp_historical_market_cap.py               ~80 LoC
  One-shot Phase A probe. Calls FMP `historical-market-capitalization`
  for AAPL/MSFT/SMCI/RIVN across multi-year window; if all return 200
  with usable payloads, sets PRIMARY market_cap source = FMP endpoint
  in ticker_metadata_writer.py. If 403/insufficient, sets FALLBACK
  source = derive from prices.parquet × FMP profile shares-outstanding.
  Result written to docs/superpowers/specs/sp2-fmp-mktcap-probe.md and
  committed before Phase A merge. Run once; deleted after Phase A ships.

src/agent/curators/universe_recommender.js               ~400 LoC
  Phase C: Mastermind `mode=universe-recs` driver.
  Per strategy: pre-backtests against the 12 candidate predicates,
  packs results into an Opus 4.7 1M prompt, parses choice, writes to
  strategy_universe_recommendations, posts to #universe-recs Discord.

src/agent/curators/run_mastermind.js                     MODIFY
  Add --mode universe-recs branch.

docs/universe-recs.service                              ~25 LoC
docs/universe-recs.timer                                ~15 LoC
  Saturday 20:00 ET (after comprehensive-review 18:00, position-recs 19:00).

scripts/backfill_universe_5y.py                          ~600 LoC
  Phase B: idempotent + resumable backfill driver.
  See §5 for design.

scripts/promote_ticker_metadata_snapshots.py             ~80 LoC
  Phase B: stage → validate → promote pattern for monthly snapshots.

tests/test_universe_resolver.py                          ~250 LoC
tests/test_universe_predicates.py                        ~150 LoC
tests/test_universe_lint.py                              ~100 LoC
tests/test_ticker_metadata_writer.py                     ~120 LoC
tests/test_backfill_idempotency.py                       ~150 LoC
tests/test_backtest_as_of.py                             ~200 LoC
tests/test_lifecycle_universe_filter_ref.py              ~120 LoC
  Regression-guard against the silent-strip pitfall.

src/system_checks/check_universe_resolution.py           ~80 LoC
src/system_checks/check_metadata_snapshot_freshness.py   ~60 LoC
  Tagged 'pipeline', 'strategies'.
```

### 3.2 Modified files (Phase A)

```
src/strategies/lifecycle.py
  - StrategyRecord gains universe_filter_ref: str | None field
  - from_manifest reads it from metadata.universe_filter_ref
    (None if absent → default predicate)
  - to_dict writes it back to metadata.universe_filter_ref
  - transition() invokes sandbox check (§2.2 #3) if predicate changed
  - load_predicate(record) helper resolves ref → callable

src/strategies/manifest.json
  - No structural change; per-strategy `metadata.universe_filter_ref` added
    by re-eval phase. Phase A leaves all entries on default (field absent).

src/backtest/unified_backtest.py
src/backtest/quick_backtest.py
src/backtest/regime_blended_backtest.py
src/backtest/intraday_regime_backtest.py
src/backtest/regime_performance_analyzer.py
  - Accept optional resolver parameter
  - Resolve universe per bar using resolver.resolve(strategy.id, as_of=bar_date)

src/pipeline/collector.js
  - Read pre-resolved union from Redis key `universe:union:{YYYY-MM-DD}:{states}`
    (TTL 4h, written by ticker_metadata_writer post its 06:35 ET run via
    UniverseResolver.union_universe + Redis SET; falls back to a single
    `python3 -m strategies.universe_resolver --json` subprocess on cache miss)
  - Replace getUniverse('all') with cached_union ∪ BENCHMARKS ∪ SECTOR_ETFS
  - Document fallback: if resolver + cache both fail, fall back to
    getUniverse('all') with WARN to #data-alerts and doctor will exit 1 next preflight

src/pipeline/backfillers/alpaca_options.py
  - Iterate union_universe(today, ['live']) filtered to options_eligible=True
  - Replaces "iterate alpaca_tradable_universe WHERE active"

src/ingestion/alpaca_news.py
  - Iterate union_universe(today, ['live']) instead of active universe

src/pipeline/run_sentiment_step.py
  - Iterate union_universe(today, ['live', 'candidate'])

src/agent/graphs/daily-cycle.js
  - Pass per-strategy resolved universe through state.universe[strategy_id]
  - Signal-generator node consumes state.universe[<self>]

src/maintenance/doctor.py
  - Add _check_metadata_snapshot_freshness (≤2 trading days old → warn,
    ≤4 days → exit 2)
  - Add _check_union_universe_size (live union ≥ 200 tickers; <200 warn,
    <50 exit 2 — sanity floor against runaway-predicate misfire)

src/maintenance/refresh_tradable_universe.py
  - On completion, trigger ticker_metadata_writer for today

src/channels/dashboard/server.js (operator dashboard :7870)
  - New "Universe Slice" tile: per-strategy resolved size today + 30d trend
  - Link-through to predicate file + last 5 resolution sizes

src/channels/api/server.js (user-facing dashboard :3000)
  - Pipeline Diagnostics tab gains a "Universe Inflation" panel showing
    union_universe size vs. raw alpaca_tradable_universe size

.pre-commit-config.yaml
.github/workflows/*.yml
  - Add lint_universe_predicates.py as a gate
```

### 3.3 `.env` changes

```
ADD:
  OPENCLAW_UNIVERSE_RESOLVER=1          (default ON post-deploy; kill switch)
  OPENCLAW_UNIVERSE_RECS=0              (Phase C gate; flipped ON after Phase B settles)
  OPENCLAW_BACKFILL_5Y_CHECKPOINT_DIR=/root/openclaw/data/.checkpoints/backfill_5y
  UNIVERSE_RESOLVER_MIN_LIVE_TICKERS=200 (doctor sanity floor)

REMOVE: (none)
```

### 3.4 Schema (migrations 111-113)

```sql
-- 111_ticker_metadata_snapshots.sql

CREATE TABLE IF NOT EXISTS ticker_metadata_snapshots (
  snapshot_date     DATE NOT NULL,
  symbol            TEXT NOT NULL,
  asset_class       TEXT NOT NULL,
  exchange          TEXT,
  status            TEXT NOT NULL,
  tradable          BOOLEAN NOT NULL DEFAULT FALSE,
  shortable         BOOLEAN NOT NULL DEFAULT FALSE,
  fractionable      BOOLEAN NOT NULL DEFAULT FALSE,
  easy_to_borrow    BOOLEAN NOT NULL DEFAULT FALSE,
  market_cap        NUMERIC,
  adv_usd_20d       NUMERIC,
  sector            TEXT,
  industry          TEXT,
  options_eligible  BOOLEAN NOT NULL DEFAULT FALSE,
  in_sp500          BOOLEAN NOT NULL DEFAULT FALSE,
  in_r1000          BOOLEAN NOT NULL DEFAULT FALSE,
  in_r3000          BOOLEAN NOT NULL DEFAULT FALSE,
  listed_date       DATE,
  delisted_date     DATE,
  source_tag        TEXT NOT NULL,    -- 'live_daily' | 'backfill_5y' | 'manual'
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (snapshot_date, symbol)
);
CREATE INDEX idx_meta_snapshots_symbol_date ON ticker_metadata_snapshots(symbol, snapshot_date DESC);
CREATE INDEX idx_meta_snapshots_date_active ON ticker_metadata_snapshots(snapshot_date) WHERE status='active' AND tradable=TRUE;

-- Master invariant: rows append-only; columns may be added.

-- 112_strategy_universe_recommendations.sql

CREATE TABLE IF NOT EXISTS strategy_universe_recommendations (
  id                BIGSERIAL PRIMARY KEY,
  strategy_id       TEXT NOT NULL,
  recommended_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  current_predicate TEXT,                          -- import path or 'default'
  candidate_predicate TEXT NOT NULL,               -- proposed import path or 'default'
  candidate_set_id  TEXT NOT NULL,                 -- which 12-slice version was tested
  backtest_summary  JSONB NOT NULL,                -- per-candidate {sharpe, max_dd, trades, mean_universe_size}
  rationale         TEXT,                          -- Opus chain-of-reasoning excerpt
  approved          BOOLEAN,                       -- NULL until operator decides
  approved_at       TIMESTAMPTZ,
  approved_by       TEXT,
  adopted           BOOLEAN NOT NULL DEFAULT FALSE,
  adopted_at        TIMESTAMPTZ,
  mastermind_cost_usd NUMERIC
);
CREATE INDEX idx_universe_recs_pending ON strategy_universe_recommendations(strategy_id) WHERE approved IS NULL;
CREATE INDEX idx_universe_recs_strategy_date ON strategy_universe_recommendations(strategy_id, recommended_at DESC);

-- 113_universe_resolution_audit.sql

-- Lightweight audit: every union_universe call gets a row so the dashboard
-- can show drift. NOT every per-strategy resolve (too noisy); only the
-- daily union.

CREATE TABLE IF NOT EXISTS universe_resolution_audit (
  id                  BIGSERIAL PRIMARY KEY,
  resolved_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  resolved_for_date   DATE NOT NULL,
  lifecycle_states    TEXT[] NOT NULL,
  union_size          INT NOT NULL,
  per_strategy_sizes  JSONB NOT NULL,
  alpaca_universe_size INT NOT NULL,                -- denominator for "inflation" stat
  resolver_ms         INT NOT NULL
);
CREATE INDEX idx_uni_audit_date ON universe_resolution_audit(resolved_for_date DESC);

-- 114_data_quarantine.sql

-- Master-parquet append-only invariant means bad rows cannot be DELETEd.
-- Instead, flag them here so all consumers (collector, backtest engine,
-- universe_resolver coverage_floor, dashboard) skip them at read time.
-- This is the ONLY supported recovery path for backfill data that slipped
-- past stage-validate-promote.

CREATE TABLE IF NOT EXISTS data_quarantine (
  id                BIGSERIAL PRIMARY KEY,
  master_table      TEXT NOT NULL,                  -- 'prices.parquet', 'options_eod.parquet', 'ticker_metadata_snapshots', ...
  symbol            TEXT NOT NULL,
  affected_date     DATE NOT NULL,
  source_tag        TEXT NOT NULL,                  -- which backfill version produced the bad row
  reason            TEXT NOT NULL,
  flagged_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  flagged_by        TEXT NOT NULL,                  -- operator email or 'auto:<check-name>'
  superseded_by_source_tag TEXT,                    -- set when a corrected backfill writes a new row
  superseded_at     TIMESTAMPTZ
);
CREATE INDEX idx_quarantine_lookup ON data_quarantine(master_table, symbol, affected_date) WHERE superseded_at IS NULL;
```

### 3.5 Memory + docs updates (Phase A merge PR)

```
/root/.claude/projects/-root/memory/project_sp2_universe_expansion.md  (NEW)
  Architecture summary, predicate contract, 4-phase rollout, file map.

/root/.claude/projects/-root/memory/feedback_universe_predicate_contract.md  (NEW)
  Predicates must take (meta, as_of); never call today()/now()/env.
  Lint enforces; sandbox check during lifecycle.transition.

/root/.claude/projects/-root/memory/MEMORY.md
  Index entries for both above.

/root/openclaw/CLAUDE.md "Recent Changes"
  SP-2 Phase A entry post-deploy.

/root/openclaw/ARCHITECTURE.md
  New section: "Per-Strategy Universe Resolution".
```

---

## 4. Data Flow

### 4.1 Daily live cycle (post Phase A)

```
06:30 ET  refresh_tradable_universe → updates alpaca_tradable_universe
06:35 ET  ticker_metadata_writer (daily) → writes today's row to
          ticker_metadata_snapshots (source_tag='live_daily').
          Idempotent: ON CONFLICT (snapshot_date, symbol) DO UPDATE.

10:00 ET  daily-cycle.js dispatched (LangGraph orchestrator, LIVE since
          2026-05-22)
  collect:    union_universe(today, ['live']) ∪ BENCHMARKS ∪ SECTOR_ETFS
              → collector fetches quotes + bars
              → universe_resolution_audit row written
  sentiment:  union_universe(today, ['live', 'candidate']) → alpaca_news + RSS + StockTwits
  signals:    for strategy in live_strategies:
                universe_strategy = resolver.resolve(strategy.id, today)
                signals += strategy.generate(bar_date=today, universe=universe_strategy)
  ... rest of cycle unchanged ...

16:30 ET  alpaca_options_archive timer (SP-1)
          → iterates union_universe(today, ['live']) ∩ options_eligible
          → appends to options_eod.parquet
```

### 4.2 Backfill flow (Phase B) — stage → validate → promote

This protects the master-parquet append-only invariant. Bad rows never reach master.

```
scripts/backfill_universe_5y.py --target prices|metadata|options --resume

STAGE  (writes to /root/openclaw/data/.staging/<job>/)
  For each (ticker, year) chunk:
    Redis key:  backfill:5y:{target}:{ticker}:{year}  (status, started_at, rows, sha256)
    Fetch from provider, write parquet partition under staging dir
    Increment data_provider_health counter

VALIDATE (per-chunk)
  Schema match against master parquet (column names, dtypes)
  Row count plausibility (≥ 200 trading days per year unless listed mid-year)
  No null primary-key fields
  Date range sanity (within requested chunk window)
  Cross-check: random 5-row spot vs. live single-fetch
  → If any check fails: status='quarantined', alert #data-alerts, skip promote

PROMOTE  (single atomic step)
  ds = pyarrow.parquet.write_to_dataset(...) into master with
       existing_data_behavior='delete_matching' on (date, symbol) partition
       This is the documented exception to the append-only rule and is
       only permitted for (a) backfill of dates not yet present in master
       or (b) cutover-gap dates explicitly flagged.
  source column tagged 'backfill_5y_v1' for audit
  Redis key flipped to status='promoted'
```

**For `prices.parquet` and `options_eod.parquet`**: the backfill ONLY writes dates
not already present per `(symbol, date)`. Existing rows are never touched.

**For `ticker_metadata_snapshots`**: monthly snapshots written first-trading-day of
each month back to 2021-05-22; live daily writer takes over from 2026-05-22 forward.
Monthly + daily coexist; resolver uses `latest(snapshot_date ≤ as_of)`.

**Throttling**:
- FMP Starter 300 req/min → semaphore 4, 200ms sleep between calls
- Alpaca AAT Plus 10k/min → semaphore 12, 80ms sleep
- Per-ticker chunks for parallelism (8-way concurrent tickers)
- Estimated wall: prices = ~5 days, metadata snapshots = ~2 days, options = ~3 days
- Runs as `nohup` background; daily progress digest to `#backfill-log`

**Resumability**: Redis checkpoints per (target, ticker, year) — re-run skips
status='promoted'; retries status∈{'failed','timeout','quarantined'} after manual
review.

### 4.3 Saturday re-evaluation (Phase C)

```
Saturday 20:00 ET  openclaw-universe-recs.timer
                   → universe_recommender.js for each LIVE strategy

For strategy in live_strategies:
  1. Load strategy code + last 12mo of closed-trade data
  2. For each of 12 candidate predicates (universe_default.py):
       run regime_blended_backtest with resolver-substituted predicate
       record {sharpe, max_dd, win_rate, mean_universe_size, trades_n,
               sortino, calmar, mean_holding_days}
  3. Pack into Opus 4.7 1M prompt:
       - strategy code (~50-200 LoC)
       - strategy thesis from manifest description
       - 12-candidate × 8-metric backtest grid
       - current predicate (default or explicit)
       - operator preferences (Sharpe ≥ 0.5, MaxDD ≤ 20%, prefer
         simpler predicates ceteris paribus)
  4. Opus emits structured JSON:
       {choice: <candidate_name>, rationale: <text>, confidence: 0-1,
        risks: [...]}
  5. Write strategy_universe_recommendations row (approved=NULL)
  6. Post to #universe-recs Discord with approve/reject reaction buttons

Operator approval flow:
  ✅ reaction → mark approved=true; lifecycle.adopt_universe_recommendation()
                writes universe_filter_ref into manifest + strategy file
                gets a `universe_filter = candidate_predicate` line
  ❌ reaction → approved=false; row archived for next cycle
  ⏸  reaction → approved=NULL; defer
```

**Why finite candidate set, not free-form Opus emission:** the candidate set
(`universe_default.py`) is pre-vetted, pre-backtest-able cheaply, and Opus
just picks among rows of a clean grid. Free-form Python predicates would need
their own sandbox + validation pass + lint, multiplying surface area. The
candidate set can be expanded over time as new slicing axes prove useful.

### 4.4 New-strategy creation (Phase D)

```
PaperHunter (Sat 10:00 ET, src/agent/graphs/paperhunter.js)
  Existing 4 rejection gates plus NEW gate:
    "Does the paper imply a universe slice we have data for?"
    If yes, emit `inferred_universe_filter: <candidate_name>` into
    research_candidates row.
    If no, downstream StrategyCoder defaults to in_sp500.

StrategyCoder (on-demand, when operator promotes candidate to staging)
  Template extended: implementations now include either:
    # No universe_filter — strategy uses DEFAULT_UNIVERSE_FILTER (in_sp500)
  OR (if research_candidates.inferred_universe_filter is set):
    from src.strategies.universe_default import options_eligible_only as universe_filter
  Lifecycle CLI registers the predicate ref in manifest.metadata.universe_filter_ref.
```

---

## 5. Phase Ordering & Realistic Timeline

| Phase | Scope | Wall-clock estimate | Gate to next |
|---|---|---|---|
| **A** | Predicate machinery (resolver, lint, schema, defaults, lifecycle threading, backtest as_of, dashboard, doctor). All 102 strategies on default → byte-identical behavior post-deploy. | ~5-7 days (one PR or 2-3 stacked PRs). | All tests green, smoke run shows union_universe = 500 (SP500 + benchmarks dedup), 1 week Soak on default in production. |
| **B** | 5y backfill (prices, metadata snapshots, options for eligible subset). Stage → validate → promote loop. | ~10-14 days throttled. Multi-day nohup. | Master parquets contain 5y × ~3000 tickers; ticker_metadata_snapshots has monthly rows back to 2021-05-22; doctor checks green. |
| **C** | Mastermind universe-recs mode + first re-eval run for 51 live strategies. Adoption is per-strategy operator click. | ~3-5 days code + ongoing weekly job. First operator-driven adoption cycle: 2-4 weeks. | 51 live strategies all have explicit universe_filter_ref; union_universe size stabilizes. |
| **D** | PaperHunter/StrategyCoder hooks for new strategies. | ~2-3 days code; impact accrues as new strategies are minted. | All net-new candidates emit a predicate at creation. |

**Total realistic wall-clock from Phase A start to Phase D completion: 4-7 weeks.**
This is not a sprint — it's a quarter-edge program. Each phase ships independently
and the system remains functional throughout.

---

## 6. Error Handling + Rollback

### 6.1 Failure-mode matrix

| Failure | Detection | Response | Severity |
|---|---|---|---|
| Resolver returns empty list for a live strategy | `universe_resolution_audit.per_strategy_sizes` shows 0 | Strategy skipped for the cycle; #data-alerts; doctor warn. Strategy code still receives a list (empty) and emits no signals — no crash. | MEDIUM |
| `union_universe < UNIVERSE_RESOLVER_MIN_LIVE_TICKERS` (default 200) | doctor preflight | Doctor exit 2 → cycle refuses start. Operator inspects predicates / metadata snapshot freshness. | HIGH |
| ticker_metadata_snapshots stale > 2 trading days | doctor check | Warn at 2d, exit 2 at 4d. Live daily writer probably broken; cycle continues using last good snapshot until 4d. | MEDIUM |
| Predicate raises an exception | Per-strategy `try/except` in resolver | Strategy treated as empty universe; alert; predicate marked broken until file changes. | MEDIUM |
| Predicate violates `as_of` contract at runtime (e.g., monkey-patched after lint) | Resolver sandbox check at first resolve | Strategy refused; lifecycle marks predicate broken; alert. | HIGH |
| Backfill chunk validation fails | Stage-validate-promote loop | Chunk quarantined; never reaches master parquet; #data-alerts; operator triages. Resumable. | LOW |
| Backfill writes a wrong row into master (slipping past validation) | Per-cycle anomaly check + spot audit | **NOT REVERSIBLE** per master invariant — only flaggable. Mitigation: validation is the only line of defense; treat any anomaly as P1. Add `data_quarantine` table row referencing the bad `(symbol, date)`; consumer code learns to skip quarantined rows. | HIGH (latent) |
| Mastermind universe-recs hangs / overruns budget | Opus call timeout 30 min, budget cap $8/strategy | Per-strategy circuit-breaker; partial run committed; resume next Saturday. | LOW |
| New strategy file emits broken predicate | Lifecycle.transition sandbox check | Transition rejected; strategy stays in CANDIDATE; alert to #strategy-memos. | LOW |
| Migration 111/112/113 partial | Transactional; rollback on failure | Standard. Phase A code degrades to default predicate if `ticker_metadata_snapshots` table doesn't exist. | LOW |

### 6.2 Rollback ladder

```
LEVEL 1 — Kill resolver, revert to hardcoded SP500
  OPENCLAW_UNIVERSE_RESOLVER=0
  systemctl restart johnbot.service
  Effect: collector / options-archive / news / signals all fall back to
          getUniverse('all') from universe.js. Identical to pre-SP-2.
  Wall: ≤30s. Reversible.

LEVEL 2 — Kill re-eval and roll back specific strategy predicates
  OPENCLAW_UNIVERSE_RECS=0
  For affected strategies: manifest edit → metadata.universe_filter_ref = null
  Strategies revert to default in_sp500 predicate.
  Wall: ≤5min per strategy.

LEVEL 3 — Full revert of Phase A
  git revert <SP2-A-merge-SHA> on main
  Migrations 111/112/113 stay (append-only; old code ignores)
  Backfilled rows in master parquets stay (tagged source_tag='backfill_5y_v1';
    can be excluded by consumers if needed)
  ticker_metadata_snapshots stays
  Effect: pre-SP-2 hardcoded-SP500 behavior fully restored.
  Wall: ~15min (revert + redeploy + doctor verify).

LEVEL 4 — Quarantine bad backfill data (master parquet poisoning)
  Backfill chunk passed validation but contained wrong data
  → INSERT INTO data_quarantine (symbol, date, source_tag, reason)
  → All consumers (collector, backtests, resolver coverage check) filter
    out quarantined (symbol, date) pairs at read time
  → Operator can re-fetch with corrected source and write new rows
    (new rows pass dedup since (symbol, date) already exists → use
    source_tag='backfill_5y_v2' and quarantine v1)
  Wall: hours; requires careful operator audit.
```

### 6.3 Pre-deploy operator checklist (per phase)

```
Phase A:
  [ ] All tests pass locally + CI green
  [ ] Smoke run on staging shows union_universe(today, ['live']) ≈ 500
  [ ] Lint scans 51 live strategy files cleanly (no predicates yet, all on default)
  [ ] Doctor preflight green on staging
  [ ] Dashboard tile renders without errors
  [ ] Rollback dry-run on staging (set OPENCLAW_UNIVERSE_RESOLVER=0, restart, verify)

Phase B:
  [ ] Phase A in production ≥ 7 days, no incidents
  [ ] Disk space check: backfill projected 30x current → confirm headroom
  [ ] FMP Starter rate-limit headroom confirmed (off-hours throttling tested)
  [ ] Staging dir cleared; Redis checkpoint dir cleared
  [ ] Dry-run backfill on 50 tickers × 1 year completes cleanly
  [ ] #backfill-log Discord channel created

Phase C:
  [ ] Phase B complete; ticker_metadata_snapshots populated back to 2021-05-22
  [ ] OPENCLAW_UNIVERSE_RECS=1 flipped
  [ ] First Saturday run output reviewed before any approval clicks
  [ ] #universe-recs Discord channel created

Phase D:
  [ ] At least one Phase C cycle adopted ≥ 5 strategies
  [ ] PaperHunter prompt updated; smoke test passes
```

---

## 7. Testing + Validation

### 7.1 Unit tests

```
tests/test_universe_resolver.py
  - resolve(strategy_id, as_of) returns expected tickers for fixture predicate
  - LRU cache returns cached result on repeat call
  - Coverage gate excludes tickers with < MIN_BARS_FOR_INCLUSION
  - Refuses as_of > today()
  - Empty universe returned cleanly (no exception) on empty predicate

tests/test_universe_predicates.py
  - DEFAULT_UNIVERSE_FILTER true for AAPL, false for unknown
  - Each of 12 candidate predicates returns plausible counts on fixture
  - Predicates handle None market_cap / None sector gracefully

tests/test_universe_lint.py
  - Signature check passes for (meta, as_of); fails for (meta), (meta, x, y), or wrong names
  - Import ban catches datetime.today, datetime.now, time.time, os.environ
  - Import ban catches first-order callee that imports forbidden
  - Acceptable: imports of pandas / numpy / src.strategies.universe_meta

tests/test_ticker_metadata_writer.py
  - Daily run idempotent (ON CONFLICT DO UPDATE)
  - Reads alpaca_tradable_universe + FMP profile + prices.parquet
  - Computes in_r1000 / in_r3000 from market_cap rank
  - Marks options_eligible=True iff chain probe returns ≥1 contract

tests/test_backfill_idempotency.py
  - Re-run with same Redis checkpoint skips promoted chunks
  - Failed chunk re-runs cleanly
  - Quarantined chunk does NOT promote on re-run unless flag cleared

tests/test_backtest_as_of.py
  - Resolver receives bar_date as as_of for each backtest bar
  - Universe changes per bar when predicate depends on time-varying fields
  - regime_blended_backtest + quick_backtest + unified_backtest + intraday_regime_backtest
    + regime_performance_analyzer all use resolver correctly

tests/test_lifecycle_universe_filter_ref.py
  - StrategyRecord.universe_filter_ref reads from manifest
  - to_dict round-trips the field (regression: silent-strip pitfall)
  - Transitioning with broken predicate raises sandbox-check error
  - Transitioning with valid predicate succeeds and triggers adoption hook
```

### 7.2 Integration: tests/test_sp2_smoke.py

```
1. doctor.py --required-only --json — all green including new checks
2. PIPELINE_DRY_RUN=1 python3 -m execution.pipeline_orchestrator
   - collect uses union_universe; matches getUniverse('all') ± 5 since
     all strategies on default
   - signals stage receives per-strategy resolved universe in state
   - no NaN/None signals on otherwise-passing strategies
3. python3 -m scripts.backfill_universe_5y --target prices --tickers AAPL,MSFT --years 2025 --dry-run
   - 2 tickers, 2 staging parquet files, schema valid, no master writes
4. python3 -m strategies.universe_resolver --strategy S5_max_pain --as-of 2024-06-15 --json
   - Returns historical universe (point-in-time)
5. python3 -m strategies.universe_lint src/strategies/implementations/
   - All 102 files pass (none have universe_filter yet; default rules)
6. system_checks regression suite tags: pipeline, broker, regime, strategies
```

### 7.3 Pre-deploy soak (Phase A)

7-day soak in production with all strategies on default predicate.
Daily success criteria:
- union_universe size: 500 ± 10
- universe_resolution_audit: ≤ 50ms per resolution
- No #data-alerts re: resolver
- Strategy signal counts within ±10% of pre-SP-2 7-day mean
- doctor exit 0 every cycle

If any criterion fails 2 days in a row → revert via Level 1.

### 7.4 Phase B validation (per-chunk + post-completion)

Per-chunk (in staging):
- Schema match against master parquet
- Row count vs. expected (trading days × symbols)
- No null PK fields
- Spot-check: random 5 rows fetched live and compared (tolerance 0.1% on prices)

Post-completion:
- Cross-source spot-check vs. an authoritative source for 20 random `(symbol, date)` pairs
- Distribution checks: market_cap, ADV histograms plausible vs. known SP500 reference set
- ticker_metadata_snapshots count: ~36 months × ~3000 tickers ≈ 108k rows ± 10%

### 7.5 Phase C validation

First Saturday run is operator-supervised:
- Inspect Opus rationale for first 10 strategies before any approval click
- Verify backtest grid is deterministic (re-run same seed → same grid)
- Mastermind cost per strategy ≤ $5 → flagged for tuning if exceeded

### 7.6 Out-of-scope for SP-2

- Asset-class expansion (crypto, commodities) — SP-3
- New strategy templates leveraging the broader universe — SP-4
- WebSocket streaming for the expanded universe — SP-5
- EDGAR SIC-based historical sector classification — future enhancement
- Wikipedia-based historical S&P 500 membership — future enhancement
- Predicate-level sector caps — sizer handles portfolio concentration

---

## 8. References

- Handoff: `docs/superpowers/specs/2026-05-21-data-provider-overhaul-handoff.md`
- SP-1 spec: `docs/superpowers/specs/2026-05-21-sp1-provider-cutover-design.md`
- Memory: `feedback_lifecycle_silent_strip.md` (the StrategyRecord field-strip pitfall)
- Memory: `feedback_silent_failure_pattern.md` (split-source freshness rule)
- Memory: `project_alpaca_cli_integration.md` (alpaca_tradable_universe origin)
- Memory: `project_e1_langgraph_orchestrator.md` (daily-cycle.js — where signals
  consume per-strategy universe)
- CLAUDE.md core invariant: master parquets append-only — backfill must stage,
  validate, then promote
- Source: `src/strategies/lifecycle.py:103,153,480` — three lockstep update sites
- Source: `src/pipeline/universe.js` — hardcoded SP500 fallback survives Phase A
- Source: `src/database/migrations/092_alpaca_tradable_universe.sql` — broker
  active-list feeder for ticker_metadata_writer
