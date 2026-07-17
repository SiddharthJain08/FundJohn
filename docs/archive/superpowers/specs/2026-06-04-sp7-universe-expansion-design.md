# SP-7: Whole-Market Universe Expansion — Design

**Spec date:** 2026-06-04
**Author:** BotJohn brainstorm session (operator-validated, 5 anchor decisions + 3 section approvals)
**Status:** Pending operator review
**Predecessors:** SP-2 (universe machinery A–D), SP-1 (Alpaca AAT Plus cutover), 2026-06-04 SP500 gap remediation
**Companion evidence:** `/root/universe_expansion_audit_2026-06-04.md`

---

## 1. Context

The 2026-06-04 audit found the Algo Plus universe expansion was absorbed by **metadata only**.
Same-session remediation closed the SP500 gap (161-ticker 5y backfill; prices.parquet
454→615 tickers; union_universe 404→503; `universe_config` 374→536 active equities;
commit `ce228ca`). What remains — and what SP-7 delivers — is the actual whole-market
expansion: a ~5,110-name liquid-tradable price envelope, a once-per-strategy
universe-determination backtest, live per-strategy universes, and breadth-scaled
conviction thresholds.

### Anchored facts (2026-06-04 system state — all grep/query-verified this session)

- **Live signals universe = ALL tickers in `prices.parquet`.** `load_approved_strategies`
  reads `strategy_registry` whose `universe` text[] labels are ignored; `_universe` is
  never assigned; engine falls to the parquet fallback (`src/execution/engine.py:1396-1402`,
  log-verified: `Universe from master prices: 454 tickers` in
  `logs/pipeline_orchestrator_2026-05-*.log` and `redeploy_pipeline_*.log`).
  **SP-2 predicates currently shape backtests only.**
- **Collector daily envelope = `universe_config`** via `store.getActiveUniverse()`
  (`src/pipeline/collector.js:1392`). `readUnionUniverseFromRedis` (collector.js:146)
  is exported with **zero callers** — SP-2 Phase A envelope wiring never landed.
- **Resolver coverage floor**: `UniverseResolver.resolve()` requires ≥60 bars in
  prices.parquet (`src/strategies/_db_adapters.py:44-73`, `min_bars=60`) — universe
  growth requires operator backfills (Phase-B driver is the sole sanctioned
  append-only-exception path).
- **`market_cap` is NULL in every `ticker_metadata_snapshots` row ever written**
  (daily + historical). Root cause: FMP profile never delivered
  (historical endpoint 403 on Starter — probe `docs/superpowers/specs/sp2-fmp-mktcap-probe.md`,
  decision `FALLBACK:prices_x_shares`, never built). Consequence:
  `rank_in_r1000_r3000` pool is empty → `in_r1000`/`in_r3000` false everywhere →
  9 of 12 predicates resolve ∅ → the 2026-05-25 Phase C universe-recs run could
  only recommend `sp500`/`no_adr` (0 approved, 0 adopted).
- **`alpaca_tradable_universe`**: 13,909 us_equity (13,876 in latest metadata snapshot);
  **5,110 tradable + active + easy_to_borrow** (the SP-7 envelope).
  `easy_to_borrow` is already a `TickerMetadata` field (`universe_meta.py:15`).
- **`first_seen_at` is refresh-log-derived** (≈2026-05-14 even for AAPL) →
  `build_month_snapshot` PIT filter (`_alpaca_status_batch`, universe_metadata.py:192)
  drops newly-tracked symbols from all historical months (64 month-chunks quarantined
  'empty' on 2026-06-04; Redis keys `backfill:5y:metadata:*` flipped
  promoted→quarantined — behaviorally equivalent, both skip under `--resume`).
- **Adjustment conflict**: price backfills use Alpaca `--adjustment split`
  (`src/pipeline/backfillers/universe_prices.py:74`); daily collector appends use
  `--adjustment all` (`collector.js:589`). All backfilled history is split-adjusted;
  daily appends are dividend-adjusted (and mutually inconsistent across dividend dates,
  since appends never restate history).
- **`min_cumulative_sharpe`** lives in `regime_sizer_params` per-regime rows,
  resolved by `_resolve_min_cumulative_sharpe` (`regime_blended_sizer.py:166`,
  bound [1.0, 10.0], `pipeline_config` global fallback, dashboard-editable).
  Operator history: LOW_VOL 4.0 produced the 2026-06-03 zero-order day; reset to 3.0.
- **Reusable machinery**: `universe_grid_cli.py` + `MockResolver`
  (universe_resolver.py:102), `strategy_universe_recommendations` table +
  `lifecycle_universe_adoption.adopt_universe_recommendation` (Discord ✅/❌ reactions +
  dashboard :7870 buttons), Phase-B backfill driver
  (`scripts/backfill_universe_5y.py`: stage→validate→promote, `--tickers`,
  `--resume`, zero-overlap precondition for net-new tickers, metadata
  `ON CONFLICT DO NOTHING`).
- **EDGAR**: CIK map already at `data/master/_sec_ticker_cik.json`;
  `corporate_actions.parquet` exists; `edgar_8k_filings` integration precedent.
- **Box**: 8GB / 2-core, no swap. Weekend-OOM conventions are load-bearing:
  sequential subprocess-per-strategy, 40-min watchdog, 1800MB floor, resumable chunks.
- **Aux coverage for new names** (2026-06-04): sentiment broad (10.9k tickers);
  financials/earnings/insider/options_eod absent — strategies graceful-skip missing aux.

## 2. Operator decisions (locked 2026-06-04)

| # | Decision | Choice |
|---|----------|--------|
| 1 | Price-data envelope breadth | **Liquid tradable ~5,110** (tradable+active+easy_to_borrow, incl. liquid ADRs). Whole-market corroboration via 13.9k metadata + broad sentiment. |
| 2 | Universe-determination backtest | **Nested-tier ladder** (sp500 ⊂ r1000 ⊂ r3000 ⊂ liquid_tradable, ×options variant) — once per strategy. |
| 3 | Conviction scaling | **√ln(N) selection-bias curve**: `threshold_tier = base × √(ln N_tier / ln N_sp500)` ≈ ×1.00/1.05/1.13/1.17, from actual resolved tier sizes at adoption, written as dashboard-editable proposals. |
| 4 | Aux-layer breadth | **Tiered by consumer**: prices+sentiment+shares/market_cap → full 5k; fundamentals+insider → adopted-union only; options → options_eligible ∩ archive accrual; 30m bars → B1-scoped. |
| 5 | Phasing | **Foundations-first A→D**, each phase gated + independently shippable. |

Operator constraints carried from the 2026-06-04 directive: the universe backtest runs
**once per strategy** (NOT weekly — it is the heaviest backtest and tier choice is
stable against stop/duration adjustments); recompute only on **every-12th-Saturday**
sentinel or **dashboard prompt**; overnight (EOD→open) window is the ingest budget.

**Delivery model:** each phase gets its own implementation plan and branch
(SP-2/SP-5 house style); Phase A plans first. Phases B–D re-ground against live
state at their own plan time.

## 3. Phase A — Data Foundations

### A1. market_cap via EDGAR shares × close
New `src/pipeline/backfillers/edgar_shares.py`:
- Pull shares-outstanding time-series from SEC companyfacts
  (`dei:EntityCommonStockSharesOutstanding`; per-class aggregation for multi-class
  issuers e.g. GOOG/GOOGL, BRK) keyed by the existing CIK map. Free, no quota; polite
  rate limiting per SEC fair-access (10 req/s ceiling, UA header — reuse the
  Cloudflare-UA lesson).
- Persist to a new append-only `data/master/shares_outstanding.parquet`
  (joins the NEVER-DELETE family).
- `market_cap(symbol, date) = shares_on(date) × split_adjusted_close(date)`.
  Wire into the **existing** `market_cap_lookup` parameter of `build_month_snapshot`
  (universe_metadata.py:290 — designed for exactly this) and the daily writer's
  profile dict. `rank_in_r1000_r3000` self-heals; no schema change.
- Validation: existing `_validate_metadata` top-10-cap >$100B floor becomes meaningful.

### A2. Point-in-time listed dates
- One-shot probe: earliest Alpaca daily bar per symbol (bars API, start=2000-01-01,
  limit=1) → new `listed_date DATE` column on `alpaca_tradable_universe`
  (additive migration; number assigned at build time from the migrations/ sequence —
  SP-2 precedent: 116/117 collided when pre-assigned).
- `_alpaca_status_batch` PIT filter switches `first_seen_at` → `listed_date`
  (fallback `first_seen_at` when NULL). Unblocks historical month snapshots for
  newly-tracked symbols (the 161 + the coming ~4.5k).

### A3. Adjustment convention: canonical = split-adjusted
- Daily collector flips `--adjustment all` → `--adjustment split` (collector.js:589).
  Rationale: dividend-adjustment restates all history at every dividend —
  incompatible with the append-only invariant; existing all-adjusted appends are
  already mutually inconsistent. Backfiller already split-adjusted.
- Dividends remain explicit in `corporate_actions.parquet`; backtests that want total
  return add them (follow-up wiring inside backtest engines is in-scope for Phase A
  only as a documented helper, not a behavior flip).
- New **split-watcher**: on a split event in corporate_actions for a covered ticker,
  queue the sanctioned per-ticker supersede re-backfill
  (`OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1` + `--source-tag backfill_5y_vN` +
  `--supersede-quarantine` — the documented v2 recovery path).
- Legacy mixed history: documented, NOT mass-restated (append-only); converges forward.

### A4. Engine universe clamp (ordering invariant: ships + flips BEFORE A5)
- Gated env `OPENCLAW_ENGINE_UNIVERSE_CLAMP` (value = predicate name, initially
  `sp500`): engine parquet-fallback universe filters through the named predicate
  against the latest metadata snapshot. **The clamp applies to us_equity common
  stocks ONLY** — benchmarks, sector ETFs, indices, and crypto tickers
  (anything non-`us_equity` or absent from the metadata snapshot, e.g. SPY/QQQ/
  VIX/BTC-USD) pass through unfiltered, since regime models and S_btc_momentum
  depend on them. ~20 lines + TDD (incl. a pass-through regression test).
  Keeps live equity trading at the current 503 while data widens underneath.
  **Lesson encoded from 2026-06-04:** backfilling prices IS a live-universe change
  while the engine reads all-parquet.
- Retired in Phase C when the resolver takes over (clamp code deleted, not gated-off).

### A5. Liquid-5k price backfill
- Artifact `data/.backfill_universe_v2.txt` = (tradable ∩ active ∩ easy_to_borrow)
  − current coverage ≈ 4,500 tickers; committed like v1.
- Existing Phase-B driver, net-new zero-overlap path (default v1 source_tag — same
  safety as the 2026-06-04 run: overlap → quarantine, never overwrite).
- ~27k chunks, multi-night: overnight-window wrapper (systemd timer 21:00 ET start,
  08:00 ET SIGTERM; `--resume` makes restarts idempotent; nice -19 sequential).
- `universe_config` activation for the 5k (`category='equity'`, active, notes-tagged,
  `has_options=false`, `has_fundamentals=false` — aux stays scoped per decision 4).
  Daily price maintenance follows automatically (collector envelope = universe_config
  until Phase C).
- Pre-IPO/spinoff year-chunks quarantine 'empty DataFrame' — expected, benign
  (2026-06-04 precedent: GEV/CEG/KVUE/SOLV/VLTO/SNDK/PSKY/Q).
- Scale: ≈ +5.7M rows → ~7.2M total ≈ 200MB single-file parquet. Readers are
  columns-scoped; partitioning deferred (risk table).
- After prices: metadata historical backfill for the new names (now works post-A2),
  and `adv_usd_20d` self-corrects from new bars on subsequent snapshots.

## 4. Phase B — Universe-Determination Backtest

### B1. Tier ladder
- Candidate chain (nested): `sp500 ⊂ r1000 ⊂ r3000 ⊂ liquid_tradable`.
  New predicate `liquid_tradable(meta, as_of) = tradable ∧ status=='active' ∧
  easy_to_borrow` added to `universe_default.py` (lint-compliant `(meta, as_of)`
  signature; no clock imports). Strategies flagged options-dependent run each tier
  intersected with `options_eligible` (i.e. `tier(meta, as_of) ∧
  options_eligible_only(meta, as_of)`) so their ladder compares like-for-like
  optionable universes.
- Runner: extend `universe_grid_cli.py` with `--mode tier-ladder` — per strategy,
  one regime-blended backtest per tier on the t+1 fill model, via `MockResolver`
  (all machinery exists; it finally gets real data).
- Compute: ~62 approved strategies × ~4 tiers ≈ 250 backtests, **once**. Nightly
  resumable queue under weekend-OOM conventions; per-run audit rows.

### B2. Selection rule (deterministic — no LLM in the loop)
- Winner = highest regime-blended Sharpe **with parsimony tie-break**: a broader tier
  must beat the next-narrower by **ΔSharpe ≥ 0.10** (mirrors the weekend-coupling
  auto-apply threshold) else the narrower tier wins.
- Output → `strategy_universe_recommendations` (existing table; legacy 2026-05-25
  rows marked superseded via `rationale` tag — no row deletion). Adoption via the
  existing `adopt_universe_recommendation` 2-phase manifest+DB flow with Discord
  ✅/❌ reactions and dashboard buttons. **Operator remains the adoption gate.**

### B3. Breadth-scaled conviction thresholds
- At adoption, per-regime `regime_sizer_params.min_cumulative_sharpe` **proposals**
  are computed: `proposed = current_base × √(ln N_tier / ln N_sp500)` with N = actual
  resolved tier sizes that day. Written as proposal rows (NOT direct writes) surfaced
  on the dashboard next to today's editable values; operator applies/edits.
  Bound check: stays within the resolver's [1.0, 10.0] clamp.

### B4. Recompute triggers (and only these)
1. **Every-12th-Saturday sentinel**: Redis week-counter checked inside the existing
   Saturday stack (no new systemd timer); when ≥12 weeks since last full ladder,
   queue it across the following nights.
2. **Dashboard button** per strategy → queues a single-strategy ladder job.
3. **Mint-time** (Phase D): one ladder run at promotion gate for new strategies.

## 5. Phase C — Live Wiring

### C1. Engine: per-strategy universes live
- Signals step builds per-strategy universes via `UniverseResolver` + manifest
  `universe_filter_ref`; gate `OPENCLAW_LIVE_UNIVERSE_RESOLVER` default-OFF.
- Rollout: **shadow-parity first** — ≥3 trading days logging resolved-vs-clamped
  universe diffs per strategy; flip requires zero signal-delta for un-adopted
  (sp500-tier) strategies. Then clamp (A4) is deleted.
- Memory: engine loads ONE union price panel and slices per strategy — equal or
  smaller than today's whole-parquet read.

### C2. Collector envelope
- `readUnionUniverseFromRedis` gets its caller: daily equity fetch list :=
  resolver union ∪ benchmarks/sector ETFs ∪ operator `universe_config` overlay.
  `universe_config` demotes from envelope-of-record to operator overlay
  (`active=false` remains a hard exclusion).
- Coverage-floor decoupling: the fetch envelope does NOT apply `has_floor` (that
  gate is for strategy resolve only) — newly adopted tiers get their data fetched,
  breaking the chicken-and-egg permanently.

### C3. Consumer audit (silent-split-source defense)
- Walk every universe reader — sentiment step, options archive, redeploy pipeline,
  screener, doctor/system_checks — and assert which envelope each consumes; extend
  `doctor` coverage/freshness checks to the 5k envelope. Each consumer gets an
  explicit envelope assertion test.
- Fundamentals/insider fetchers scope to adopted-union (decision 4).

## 6. Phase D — Research Uplift

- **PaperHunter**: `{{AVAILABLE_DATA}}` gains per-tier descriptors (ticker counts,
  per-layer coverage spans); §5 predicate menu updated — tier predicates are now
  real choices with real data.
- **Mint-time ladder**: new strategies run the ladder once at the promotion gate
  (candidate stage uses the PaperHunter-inferred predicate; ladder confirms/corrects
  before live). Once-per-strategy invariant holds for mints.
- **Legacy Phase C universe-recs decommissioned**: the Sat 20:00 ET Opus grid mode
  (`run_mastermind.js --mode universe-recs` + `openclaw-universe-recs.timer`) retires;
  its adopt/reaction/dashboard plumbing is exactly what Phase B reuses.
- **Dashboard Universe page**: per-strategy adopted tier, ladder scores, last-run
  date, Recompute button, envelope-size trend.

## 7. Testing & Error Handling

- TDD per phase; every gate default-OFF with byte-identical-when-OFF regression tests.
- A4/C1 parity harnesses: resolved-vs-actual universe diff + zero-signal-delta proof
  before any flip.
- Backfills/ladder: resumable, audited (backfill_audit / recommendation rows),
  watchdogged, sequential (subprocess-per-strategy frees RSS between runs).
- EDGAR shares: unit sanity (shares in [1e6, 2e11]), multi-class aggregation tests,
  top-10-cap floor.
- Quarantine paths preserved verbatim — overlap refusal stays the default; supersede
  only via the documented v2 path.

## 8. Risks

| Risk | Mitigation |
|---|---|
| Backfill silently widens live universe (2026-06-04 lesson) | A4 clamp ships + flips **before** A5 — ordering invariant restated in the plan |
| Box OOM on ~250-backtest ladder | Nightly sequential queue, subprocess-per-strategy, 40-min watchdog, 1800MB floor |
| EDGAR shares quirks (units, multi-class, late filings) | Aggregation rules + unit gates + carry-forward-last-filing; validation floor |
| Single-file parquet growth (~200MB) | Columns-scoped reads (already true); partition rework deferred with revisit criterion: doctor read-latency >5s |
| Mixed adjustment legacy | Documented; split-watcher supersede; total-return via corporate_actions helper |
| FMP quota / 403s | EDGAR primary for shares; fundamentals stay adopted-union-scoped |
| Ladder universe-overfit | Nested tiers only (no per-ticker carving); parsimony tie-break; once-per-strategy cadence |
| Zero-survivor regression from threshold scaling | √ln(N) curve is gentle (≤×1.17); proposals not direct writes; [1.0,10.0] clamp; operator applies |

## 9. Acceptance Criteria

- **Phase A**: market_cap non-NULL for ≥95% of the 5k in the daily snapshot;
  `in_r1000`/`in_r3000` non-empty daily + historical; 5k price coverage = 100% of
  (tradable ∩ active ∩ ETB); clamp parity = zero live-universe delta throughout A5;
  metadata historical months build for post-A2 symbols.
- **Phase B**: every approved strategy has a ladder verdict + adoptable rec;
  √ln(N) threshold proposals visible on dashboard; 12th-Saturday sentinel +
  dashboard recompute functional.
- **Phase C**: ≥3-day shadow parity clean → gate flipped → clamp deleted; collector
  daily envelope = resolver union (log-verified); fundamentals/insider scoped to
  adopted-union; consumer audit assertions green.
- **Phase D**: first new mint passes mint-time ladder; legacy Opus grid removed;
  Universe dashboard page live.

## 10. Out of Scope

- Signal-level corroboration overlay (revisit after expanded-universe live cycles accrue)
- True foreign listings, futures (no Alpaca route)
- Partitioned-parquet storage rework (deferred with revisit criterion)
- FMP plan upgrade decisions
- Mass restatement of legacy mixed-adjustment history
