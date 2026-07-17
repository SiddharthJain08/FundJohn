# Universe Expansion Audit — 2026-06-04

**Question:** Has the Alpaca Algo Plus / SP-2 universe expansion (SP100 → SP500+ → whole-market) actually propagated into ingestion and research?

**Verdict: NO — metadata ingestion absorbed it; price ingestion, universe resolution, and research usage did not.** Live strategies still trade an effective ~404-name universe. DELL (S&P 500 member) has **zero** price bars locally — exactly the class of miss the operator observed.

---

## Evidence chain

### 1. Metadata ingestion: ✅ ABSORBED
- `alpaca_tradable_universe`: **13,909** us_equity symbols (13,009 tradable). DELL present.
- `ticker_metadata_snapshots`: **13,876** symbols, latest snapshot 2026-06-03, `in_sp500` count = **503 (correct)**. DELL `in_sp500 = t`.

### 2. Derived metadata fields: ❌ EMPTY / POISONED — root cause TRACED
At latest snapshot (2026-06-03), across all 13,876 symbols:
- `market_cap` = NULL for **all** rows, and **has been NULL in every daily snapshot ever taken** (checked 05-25 → 06-03: with_mcap = 0/13,7xx every day). The daily writer fills it from `fmp_profile.get(sym).get('mktCap')` (`ticker_metadata_writer.py:~114`) — the FMP profile source has never delivered (FMP Starter 403 on bulk profile; same gap noted in Phase B for historical snapshots was assumed daily-only-OK, but daily is dead too).
- `in_r1000` = 0, `in_r3000` = 0 → **downstream consequence, not a separate bug**: `rank_in_r1000_r3000` pools only rows with non-None market_cap (`universe_metadata.py:233-265`) → pool is empty → both sets empty → `r1000`/`r3000`/`large_cap`/`mid_cap`/`small_cap_liquid` predicates return false for everything. **One fix (market_cap source) revives five predicates.**
- `adv_usd_20d` = 0.0 for DELL/PLTR/SMCI → computed from local prices.parquet, which lacks them (circular).
- `options_eligible` = f for DELL/PLTR/SMCI (wrong in reality; derived from local options data presence — circular).

**Consequence:** of the 12 vetted predicates, only `sp500`, `no_adr`, and (partially) `options_eligible_only` can return non-empty sets. The rest resolve to ∅.

### 3. Price ingestion: ❌ NOT EXPANDED
- `data/master/prices.parquet`: **454 distinct tickers** (426 with rows since 05-28), max date 2026-06-03. **DELL: 0 rows.**
- SP-2 Phase B 5y backfill (`backfill_5y_v1`) targeted only **404 tickers** — the list in `data/.backfill_universe_v1.txt` (404 lines, **no DELL**), which derives from the stale static list below. 1,062 chunks promoted (266,563 rows); 1,362 "quarantined" chunks were idempotent re-run skips (overlap-refusals), not data loss.
- `src/pipeline/universe.js`: the `SP500` constant has only **406 names (405 dedup)** — labeled "S&P 500 constituents April 2026 / SPY holdings" but ~100 names short. **DELL absent.** `SP100 = SP500` alias.

### 4. Universe resolution: ❌ STRUCTURALLY LOCKED (chicken-and-egg)
- `universe_resolver.resolve()` gates every ticker on `ParquetCoverage.has_floor()` = **≥60 bars in prices.parquet** (`src/strategies/_db_adapters.py:71`).
- DELL has 0 bars → **can never enter any strategy's universe regardless of predicate** until prices are backfilled.
- Daily collector fetches `union_universe` (predicates ∩ coverage floor) → ingestion only maintains tickers that already have data. **The universe can never grow through the daily path.** Growth requires an operator-invoked backfill (Phase B driver).
- Net: live `sp500`-predicate strategies resolve to in_sp500 (503) ∩ coverage (454) ≈ **~404 names**.

### 5. Phase C universe-recs (2026-05-25 run): ❌ COULD ONLY TIGHTEN — and is inert anyway
- All recommendations in `strategy_universe_recommendations`: candidate = `sp500` (a few `no_adr`). Broader predicates were dead at grid-build time (see §2), so Mastermind never saw a viable expansion candidate. This is precisely the operator's "it only tightened" observation.
- **Zero rows approved, zero adopted** — even the tighten recommendations never took effect. Strategies still run `DEFAULT_UNIVERSE_FILTER = sp500`.

### 6. Research process: ⚠️ WIRED BUT INERT
- PaperHunter §5 (Phase D, gate LIVE) correctly infers 1 of the 12 predicates at mint, and prompt includes r1000/large_cap/mid_cap etc.
- But any minted strategy with a broader predicate resolves to ∅ (or ~404 after floor) at runtime — research "absorbed" the taxonomy, not the data.
- PaperHunter's `{{AVAILABLE_DATA}}` context reflects the 454-ticker local reality, biasing idea selection toward incumbent coverage.

### 7. Non-equity scope (SP-3/4/5): scaffolding in, data thin
- `crypto_bars_1h.parquet`: 10,336 rows. `prices_30m.parquet`: only 16 tickers (B1 case studies). Options EOD/IV present for incumbent universe.
- Instrument-class spine (SP-3), non-equity origination (SP-4), options exec lane (SP-5) all live/merged — these are NOT blocked by this audit; the equity universe is.

### 8. Cosmetic SP100 references (the dashboard sightings)
- `src/channels/api/server.js:4411` — `CAT_LABELS = {equity:'S&P 100', ...}` ← the dashboard label the operator sees.
- `src/pipeline/collector.js:1351,1397,1479` — "S&P 100" notify strings + `getUniverse('SP100')` (alias, harmless).
- `src/pipeline/store.js:102,128` — `index_membership` defaults to `'SP100'`.

---

## Session outcome (2026-06-04, operator-approved)

Operator chose: **backfill now + cosmetics**; market_cap source fix → spec session.

1. **161-ticker SP500 gap price backfill** launched 09:13 UTC via Phase-B driver
   (`backfill_5y_v1` tag — zero-overlap precondition makes it overwrite-safe for
   net-new tickers; preflight all-green; DELL dry-run validated 2021→2026).
   Quarantined chunks = pre-IPO/spinoff empty years (GEV, CEG) — expected.
2. **Cosmetics shipped** (commit `ce228ca` on live branch, johnbot restarted
   09:22 UTC healthy): `universe.js` SP500 regenerated 406→503(+4 benchmarks)
   from metadata (DELL in), dashboard `S&P 100`→`US Equities`, collector/store/
   flash labels + defaults → SP500.
3. Once names cross the 60-bar floor: sp500-predicate strategies pick them up at
   next `union_universe` resolve; daily collector then maintains them (fetch
   envelope = resolved union, now ~500 equities).
4. **Prices backfill COMPLETE 10:4x UTC**: 943 chunks / 212,099 rows promoted;
   23 quarantines = pre-listing empties (GEV/CEG/KVUE/SOLV/VLTO/SNDK/PSKY/Q).
   prices.parquet 454→615 tickers; **SP500 price coverage 100%**; DELL 1,360
   bars (2021-01-04→2026-06-03). **union_universe verified: 404 → 503 — the
   full S&P 500, all 161 gap tickers resolve in.**
5. **universe_config activated** for all 503 members (374→536 active equities,
   notes-tagged `sp500-gap-activation 2026-06-04`). Necessary because the daily
   collector's fetch envelope is `store.getActiveUniverse()` (universe_config),
   NOT union_universe — `readUnionUniverseFromRedis` is exported with **zero
   callers** (SP-2 Phase A envelope wiring never landed; spec item 4 below).
6. **Historical monthly metadata for the 161: BLOCKED by a pre-existing gap** —
   `build_month_snapshot` drops symbols with `first_seen_at > snapshot_date`,
   and `alpaca_tradable_universe.first_seen_at` is refresh-log-derived
   (≈2026-05-14 even for AAPL; documented v1 known gap). All 64 historical
   month-chunks returned empty → quarantined (audit rows present; the 161's
   2026-05-31 month + daily snapshots DID land). Live resolution unaffected
   (daily writer covers all 13.9k symbols). True point-in-time first_seen
   semantics → spec session (required for the universe-determination backtest
   anyway). NOTE: the re-run flipped 64 Redis checkpoint keys
   (`backfill:5y:metadata:*`) from `promoted`→`quarantined`; behaviorally
   equivalent (both skip under --resume), restoration was declined by policy.

## Post-backfill findings + operator decision (added 11:0x UTC)

- **LIVE UNIVERSE WIRING (major)**: the live signals engine does NOT use the
  UniverseResolver. `load_approved_strategies` reads `strategy_registry`
  (whose `universe` text[] column — `{SP500}`/`{SP100}` labels — is ignored;
  `_universe` never set) → engine falls back to **all tickers in
  prices.parquet** (`engine.py:1396-1402`). SP-2 predicates currently shape
  BACKTESTS only. Consequence: the backfill became live-effective immediately —
  today's 3:55 PM ET cycle computes over 615 names (was 454).
- **Operator decision: RUN HOT today (06-04)** — no holdout; per-ticker cap +
  3.0 conviction gate bound risk; tonight's 20:40 UTC `sp6-fill-verify` verdict
  to be read as "first fills from expanded universe," not vs the ~35-survivor
  projection.
- **Aux-data state for the 161**: sentiment ✓ (broad 10.9k-ticker layer);
  financials/earnings/insider/options_eod ✗ (zero rows — aux-dependent
  strategies skip these names; price-only strategies rank them on full 5y
  history). Daily collector (universe_config now active) begins filling
  fundamentals/options layers from today's cycles.
- **Adjustment-basis conflict (pre-existing, system-wide)**: backfiller uses
  Alpaca `--adjustment split` (universe_prices.py:74, per Phase B spec) while
  daily collector appends use `--adjustment all` (collector.js:589, mirroring
  pre-cutover yfinance auto_adjust). All backfilled history (the May v1 404-name
  chunks AND today's 161) is split-adjusted-only; daily appends are
  dividend-adjusted. Cross-sectional ranking noise ≈ dividend yield (0–3%/yr)
  across the boundary. Needs a single-convention decision in the spec session.

## What the fix spec must still cover (new session, per operator)

1. ~~Refresh the canonical SP500 list~~ → **DONE this session** (`ce228ca`).
2. ~~SP500-gap price backfill~~ → **DONE this session** (161 tickers). **Beyond-SP500 breadth (r1000/r3000-style) still owed** — gated on market_cap fix (item 3).
3. **Close the derived-field gaps**: market_cap source is the single root cause (FMP profile never delivered — daily snapshots have had with_mcap=0 since inception; fix source → r1000/r3000 ranking self-heals via `rank_in_r1000_r3000`); options_eligible recompute follows options-archive accrual for the new names.
4. **Break the chicken-and-egg / fix the fetch envelope**: collector daily envelope = universe_config, not union_universe (`readUnionUniverseFromRedis` has zero callers — Phase A wiring never landed). Either wire it (without the coverage floor for fetch) or formalize universe_config as the envelope with an expansion queue feeding the backfill driver.
4b. **Point-in-time `first_seen_at`**: alpaca_tradable_universe first_seen is refresh-log-derived (≈2026-05-14 for everything) → historical month snapshots can't be built for newly-tracked symbols, and existing v1 history's listed-date semantics are wrong. Need true listing dates (Alpaca asset details / EDGAR) before the universe-determination backtest can trust point-in-time membership.
5. **Once-per-strategy universe-determination backtest** (operator design): heaviest backtest; runs once per strategy, NOT weekly; optional recompute every 12th Saturday or dashboard-triggered. Phase C grid machinery (`MockResolver`) is reusable once predicates resolve non-empty.
6. **min_cumulative_sharpe raise** tied to whole-market corroboration (operator: higher conviction outside high-caps).
7. **Re-run Phase C universe-recs grid** once broader predicates resolve non-empty (the 05-25 run was structurally unable to recommend expansion).
8. **Timing budget**: EOD→open pipeline (SP-6 Phase A) leaves the overnight window for the wider fetch — breadth increase is schedulable after close.
9. **Wire the resolver into the LIVE signals path** — today live universe = prices.parquet contents (engine fallback); per-strategy predicates are backtest-only. Without this, predicate adoption and the universe-determination backtest change nothing live.
10. **Unify the price adjustment convention** (split-only backfills vs all-adjusted daily appends — see findings above).

## Key file/line references
- `src/strategies/universe_resolver.py:58` (floor gate), `:80-99` (union)
- `src/strategies/_db_adapters.py:44-73` (ParquetCoverage, min_bars=60)
- `src/strategies/universe_default.py` (12 predicates; sp500 default)
- `src/pipeline/universe.js:6` (stale 406-name list), `collector.js:144-168` (fetch envelope)
- `scripts/backfill_universe_5y.py` (`--tickers`, `data/.backfill_universe_v1.txt`)
- `src/pipeline/ticker_metadata_writer.py` + `src/pipeline/backfillers/universe_metadata.py` (in_sp500 CSV point-in-time; r1000/r3000 TODO)
- `src/agent/prompts/subagents/paperhunter.md:131-167` (predicate-at-mint)
- `src/channels/api/server.js:4411` (dashboard label)
