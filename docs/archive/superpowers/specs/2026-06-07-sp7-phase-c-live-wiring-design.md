# SP-7 Phase C — Live Wiring (design)

**Date:** 2026-06-07 · **Status:** operator-approved design (sections 1–3 approved in session)
**Parent:** `docs/superpowers/specs/2026-06-04-sp7-universe-expansion-design.md` §5
**Init prompt:** `docs/superpowers/specs/2026-06-07-sp7-phase-c-init-prompt.md`

Make per-strategy universes LIVE in the trading engine (C1), re-point the
collector's daily fetch envelope to the resolver union (C2), and wire every
universe consumer to its correct envelope (C3). Phases A (data foundations)
and B (tier-ladder backtest + adoption + threshold proposals) are complete
and activated.

## 0. Entry-condition deviation (operator-approved 2026-06-07)

The init prompt's entry conditions 1–2 (ladder drained; adoptions decided)
do NOT hold at design time — `ladder-20260607` has 264/268 cells queued
(night 1 = Mon 2026-06-08 01:00 UTC, est. 3–10 nights). Condition 3
(`universe_tier_coherence`) PASSes. **Operator decision: build during the
ladder drain; the C1 live flip stays hard-gated on ladder drain + adoption
decisions + 3-day shadow parity.** Every gate in this phase is default-OFF;
nothing behavioral changes at merge time.

## 1. Grounded live-state facts (verified 2026-06-07, this session)

These correct/refine the init prompt where they differ:

- **Engine universe today** (`src/execution/engine.py:1511–1534`): NO strategy
  implementation sets `self._universe`, and `get_approved_strategies`
  (`src/strategies/registry.py:202`) never sets it from the DB row — so the
  parquet fallback ALWAYS fires. Every approved strategy receives the SAME
  universe: all `prices.parquet` tickers → `clamp_universe()` → ≈591. The
  `strategy_registry.universe` column (`['SP500']`×38, `['SP100']`×29) is dead
  in the live path. Per-strategy universes do not exist live; C1 introduces
  them.
- **Clamp semantics** (`src/execution/universe_clamp.py:97–122`): the sp500
  predicate applies ONLY to clampable equities (`us_equity` in metadata AND
  `category='equity'` in universe_config); etf/index/crypto/absent-from-
  metadata ALWAYS pass through. This is why the 2 live non-equity strategies
  (S_btc_momentum crypto, S_commodity_etp_momentum etp) work under the clamp.
  Dash→dot symbol-form bridge at `universe_clamp.py:104`.
- **Resolver perf** (`src/strategies/_db_adapters.py`): `fetch_metadata_as_of`
  opens a NEW psycopg2 conn per call (line 15); `ParquetCoverage._load_month`
  re-reads ALL of prices.parquet per month-miss (line 58). 67-strategy union
  resolve = 30–50 s wall on the loaded 2-core box; the `universe_resolution`
  system_check (15 s gate) already FAILs under load.
- **Fix donor**: `CoverageIndex` (`scripts/build_tier_membership.py:41–70`) —
  one parquet read → (ticker × month) cumsum → O(1) `has_floor`. It is
  script-local today; must be hoisted to an importable module.
- **Resolver semantics** (`src/strategies/universe_resolver.py:40–66`):
  `universe_filter_ref=None` → `DEFAULT_UNIVERSE_FILTER` (sp500); `resolve()`
  applies `has_floor` inline — the C2 no-floor envelope needs a separate path.
  `resolve()` has a per-instance `(strategy_id, as_of)` cache. No
  instrument_class awareness anywhere in the resolver.
- **Non-equity strategies**: S_btc_momentum (crypto, live), S_commodity_etp_momentum
  (etp, live), S_short_straddle_vrp (option, candidate) — all carry
  `universe_filter_ref=None` in the manifest. A naive "pure resolver output"
  C1 would resolve them to sp500 and silently break them.
- **C2 target**: `readUnionUniverseFromRedis` (`src/pipeline/collector.js:146–168`)
  has ZERO callers (exported line 1906; its Redis write-through at line 161 is
  dead). Live fetch list = `store.getActiveUniverse()` = `universe_config WHERE
  active=true` (collector.js:1494 daily, 1825 EOD). Init prompt's "~1392" is
  stale.
- **C3 consumers**: sentiment (`run_sentiment_step.py:207` →
  `current_universe` = universe_config ∪ open positions ∪ 7d signals; resolver
  helper `_select_sentiment_universe` at `resolve_sentiment_universe.py:102`
  exists UNUSED); options archive (`src/pipeline/backfillers/alpaca_options.py:232`
  `_load_universe` = universe_config SP500; resolver helper
  `_select_archive_universe` line 60 exists UNUSED); redeploy
  (`scripts/redeploy_pipeline.py:170` re-runs the engine signals step —
  inherits C1); screener (`alpaca_screener.js:66` writes universe_config
  active=false); doctor `union_universe_size` (doctor.py:1149, resolver CLI);
  system_checks: universe.py / universe_resolution.py / universe_recs_health.py /
  universe_tier_coherence.py.
- **Predicates**: `CANDIDATE_PREDICATES` = 16 entries, `LADDER_TIER_PREDICATES`
  = 4 (`src/strategies/universe_default.py:75–100`; the "12 candidates"
  docstring at line 17 is stale).
- **Verified**: test_sp2_smoke.py carries NO tier-coherence exemption (B0
  runbook §2d done); #universe-recs webhook wired; latest migration = 132 →
  this phase takes **133**.

## 2. Operator decisions (this session)

| # | Decision | Choice |
|---|---|---|
| D1 | Start despite entry conditions 1–2 failing | Build during drain; **flip gated** on drain + adoptions + parity |
| D2 | C3 scope | **Wire consumers now** (sentiment, options archive, fundamentals/insider), each behind its own default-OFF gate |
| D3 | C1 universe construction | **Mirror clamp semantics**: predicate scopes clampable equities only; non-equity always passes through |
| D4 | C1 architecture | **In-cycle resolution + perf fix prerequisite** (rejected: nightly precompute = new split-source risk; union-only swap = no per-strategy universes) |

## 3. C1 — engine per-strategy universes

### 3.1 New: `src/strategies/coverage_index.py`

`CoverageIndex` hoisted verbatim from `scripts/build_tier_membership.py`
(constructor takes a pre-loaded prices DF; `from_parquet()` classmethod; O(1)
`has_floor(symbol, as_of)` on a cumsum matrix). `build_tier_membership.py`
imports it back — no behavior change, regression-tested.
`_db_adapters.ParquetCoverage` gains an injectable fast path (accepts a
`CoverageIndex`) so the resolver uses one parquet read per process instead of
one per month-miss.

### 3.2 Shared-connection adapters

`_db_adapters.fetch_metadata_as_of` accepts an optional shared psycopg2
conn/connection-factory; default = today's per-call behavior (backward-
compatible, all existing callers unchanged). Per signals cycle: ONE metadata
snapshot fetch (memoized by `as_of`) + ONE CoverageIndex, shared across all
~67 per-strategy resolves.

**Perf acceptance: warm 67-strategy union resolve <10 s on the loaded box**
(slow-marked perf smoke test); `universe_resolution` system_check (15 s gate)
re-greens.

### 3.3 New: `src/execution/live_universe.py`

`build_strategy_universes(strategies, as_of, ...) → {strategy_id: [tickers]}`

- **Mirror-clamp semantics** per strategy: `resolve(universe_filter_ref |
  default sp500)` applied to *clampable equities only* (same
  `us_equity`+`category='equity'` classification + dash→dot bridge as
  `universe_clamp.py`) ∪ non-equity passthrough (etf/index/crypto/absent).
  Un-adopted strategies reproduce today's ≈591 BY CONSTRUCTION; non-equity
  strategies keep BTC-USD/GLD/SLV/USO regardless of predicate; adoption widens
  only the equity slice.
- `universe_filter_ref` read from the **manifest**, keyed by the engine's
  **registry-approved** strategy list (the execution gate —
  feedback_manifest_vs_registry_execution_gate). Manifest-missing strategy →
  default predicate + WARN.
- **Fail-open per strategy**: any resolve error → that strategy keeps the
  legacy shared universe + ERROR log. Never empty a live strategy's universe.

### 3.4 Engine integration — gate `OPENCLAW_LIVE_UNIVERSE_RESOLVER` (default-OFF)

- OFF → byte-identical to today (regression-tested).
- ON → per-strategy dict built once per cycle; panel universe = union of all
  per-strategy sets; **ONE** `load_prices(union)` + `load_aux_data(union)` as
  today (memory invariant); `run_strategies` slices the wide panel's columns
  AND `aux_data` per strategy before each `generate_signals` call. Aux slicing
  closes the loophole where a strategy iterates aux keys instead of the
  `universe` param — identical universe ⇒ identical inputs ⇒ identical signals
  (determinism, no double signal-run needed on the 2-core box).

### 3.5 Shadow parity — gate `OPENCLAW_LIVE_UNIVERSE_SHADOW` (default-OFF; ON while resolver gate OFF)

- Each signals cycle: compute resolved per-strategy sets, diff vs the actual
  clamped universe; write **migration 133 `universe_shadow_parity`** rows:
  `(run_date, strategy_id, predicate, n_resolved, n_actual, added_tickers
  JSONB, removed_tickers JSONB, created_at)`. Per-strategy, per-ticker
  granular — a parity failure names which strategy diverges on which tickers
  (remedies: widen default predicate / adopt the strategy / fix category
  metadata).
- Shadow is a non-fatal sidecar (wrapped like pyportfolioopt_shadow): any
  shadow error logs + never blocks signals.
- **Parity criterion: zero universe-diff for all UN-ADOPTED strategies on ≥3
  consecutive trading days.** Un-adopted = manifest `universe_filter_ref` is
  None or resolves to the sp500 default at evaluation time (i.e., no
  `sp7b-%` adoption applied). Adopted strategies' diffs are expected, logged,
  non-gating. Adoptions landing mid-window don't reset the un-adopted
  criterion.
- New system_check `universe_shadow_parity` (strategies tag): PASS = last 3
  trading days zero-diff for un-adopted; FAIL lists top offenders.

### 3.6 Flip + clamp deletion (operator runbook)

Prereqs: ladder drained + adoptions decided + shadow ≥3 trading days green +
C2 envelope flipped ≥1 trading day prior (data present for adopted tiers).
Then: operator flips `OPENCLAW_LIVE_UNIVERSE_RESOLVER=1` → restart johnbot →
observe 1 cycle (signal counts sane, no empty universes) → **DELETE**
`src/execution/universe_clamp.py` + the engine call site (engine.py:1533–1534,
sole caller, grep-verified) + `OPENCLAW_ENGINE_UNIVERSE_CLAMP` env line +
clamp tests. Delete, not gate-off (parent spec A4 contract). The shadow gate
retires with the clamp; `universe_shadow_parity` rows remain as audit history.

## 4. C2 — collector fetch envelope

Gate `OPENCLAW_COLLECTOR_RESOLVER_ENVELOPE` (default-OFF → byte-identical).

- **Resolver**: new `envelope_universe(as_of, states)` — union of per-strategy
  predicate membership over equities **WITHOUT `has_floor`** (the floor stays
  strategy-resolve-only; newly adopted tiers get their data fetched → the
  coverage-floor chicken-and-egg is dead permanently). New CLI flag
  `--envelope`. Redis key `universe:envelope:<date>:<states>` (EX 14400),
  distinct from the floored-union key.
- **Collector**: `readUnionUniverseFromRedis` (generalized to take the key
  kind) gets its caller at BOTH cycle sites (collector.js:1494 daily, 1825
  EOD). Equity fetch list := `envelope ∪ universe_config(active=true,
  equity)`, then **minus `universe_config(active=false)`** — universe_config
  demotes from envelope-of-record to operator overlay; `active=false` is a
  hard exclusion applied AFTER the union. Non-equity categories
  (market/options/fundamental tickers) keep current universe_config sourcing.
- **Fail-open, never-shrink**: resolver CLI failure, empty result, or envelope
  smaller than the current universe_config equity count → fall back to today's
  list + WARN. A broken resolver must never shrink the data envelope.
- **Observability**: per-run log `envelope: resolver=N config=M excluded=K
  final=F`; doctor check `collector_envelope_freshness`.
- **Growth note**: a tier_liquid adoption pushes the envelope toward ~5k.
  Alpaca bars batch-fetch + Phase A's 5k price backfill make this viable; the
  first post-adoption collector run is the soak point — runbook instructs the
  operator to watch collect-step wall time that day.

## 5. C3 — consumer dispositions

Each wiring gets its own default-OFF gate + ONE envelope-assertion test.

| Consumer | Today | Phase C disposition | Gate |
|---|---|---|---|
| Sentiment step | universe_config ∪ positions ∪ 7d signals | wire `_select_sentiment_universe` (adopted-union) ∪ positions ∪ 7d signals | `OPENCLAW_SENTIMENT_RESOLVER_UNIVERSE` |
| Options archive | universe_config SP500 | wire `_select_archive_universe` (options-eligible ∩ live union) | `OPENCLAW_OPTIONS_ARCHIVE_RESOLVER_UNIVERSE` |
| Fundamentals/insider fetchers | universe_config category filter | scope to **adopted-union (floored)** — expensive FMP calls track what strategies use, NOT the wide envelope (parent decision 4) | rides `OPENCLAW_COLLECTOR_RESOLVER_ENVELOPE` |
| Redeploy pipeline | re-runs engine signals step | no change — inherits C1; assertion test pins it | — |
| Screener | writes universe_config active=false | no change; assertion test pins active=false as hard exclusion | — |
| Doctor | union_universe_size via resolver CLI | extend with envelope freshness | — |
| system_checks | universe_resolution 15 s gate | re-greens post-perf-fix | — |

**Envelope hierarchy:** prices fetch = wide no-floor envelope; strategy
resolve = floored union; expensive per-ticker fetchers = adopted-union.

## 6. Error handling

- Fail-open everywhere, loudly: per-strategy resolve → legacy universe +
  ERROR; envelope → universe_config + WARN; shadow → non-fatal sidecar.
- Never empty a live universe; never shrink the fetch envelope; `active=false`
  exclusion survives every fail-open path.
- Split-source defense: shadow rows record the predicate used per strategy per
  day — manifest/registry drift is visible in data, not silent.

## 7. Testing

TDD per task; chunked pytest (box OOMs monolithic runs); sequential subagents;
heavy sweeps OUTSIDE 01:00–13:00 UTC Mon–Fri (the ladder owns that window).

- CoverageIndex: equivalence vs old `ParquetCoverage` on fixture parquet +
  `build_tier_membership` regression.
- Shared-conn adapters: same-results (shared vs per-call), existing callers
  unchanged.
- live_universe: **mirror-clamp parity test** — for default-predicate
  strategies on fixtures, `build_strategy_universes` == `clamp_universe`
  output (the load-bearing test of the phase); non-equity passthrough
  (BTC-USD/GLD survive any predicate); per-strategy fail-open.
- Engine: gate-OFF byte-identical regression; gate-ON per-strategy
  panel/aux slicing.
- Shadow: rows written; zero behavior delta; non-fatal on DB error.
- C2: envelope merge/exclusion/never-shrink units (JS, existing test/
  pattern); no-floor-vs-floored differentiation test.
- C3: one envelope-assertion test per consumer (table §5).
- Perf smoke (slow-marked): warm union resolve <10 s.

## 8. Build sequencing

1. Perf fix (coverage_index hoist + shared conn) — prerequisite for everything
2. live_universe + engine gate + shadow (migration 133)
3. C2 envelope (resolver `envelope_universe` + collector caller)
4. C3 wirings (sentiment, options archive, fundamentals/insider scoping)
5. Merge gate-OFF → restart → turn `OPENCLAW_LIVE_UNIVERSE_SHADOW=1` ON
6. *(ladder drains; operator decides adoptions)*
7. ≥3-trading-day green shadow window
8. Operator: C2 flip → (≥1 day) → C1 flip → clamp DELETED → C3 flips
   individually

## 9. Risks

| Risk | Mitigation |
|---|---|
| Parity fails on passthrough names | Per-ticker shadow rows name the strategy + tickers; remedies enumerated (widen default / adopt / fix category) |
| Envelope jump to ~5k after tier_liquid adoption | Batch bars + Phase A backfill done; runbook soak-watch on first post-adoption collect |
| Resolve still slow post-fix | Perf smoke gates merge; shadow window measures real cycle cost pre-flip |
| Clamp deletion breaks hidden caller | engine.py:1533 is the sole call site (grep-verified); tests deleted with it |
| Adoptions land mid-shadow-window | Expected: adopted diffs don't gate; un-adopted criterion unaffected |
| Shadow DB writes fail | Non-fatal sidecar; doctor surfaces staleness |

## 10. Acceptance (parent spec §9, Phase C row)

≥3-day shadow parity clean → gate flipped → clamp deleted; collector daily
envelope = resolver union (log-verified); fundamentals/insider scoped to
adopted-union; consumer audit assertions green.

## 11. Out of scope (Phase D)

Legacy universe-recs mode + gate removal; mint-time ladder; PaperHunter tier
menu; options_eligible chain-probe backlog; Universe dashboard page; sp500
membership-history backfill (B0 re-bound backlog).
