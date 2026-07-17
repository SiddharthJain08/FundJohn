# SP-3 — Asset-Class Expansion — Design Spec

**Created:** 2026-05-26
**Author:** BotJohn (Claude Code, live VPS)
**Status:** Design approved (operator, 2026-05-26) → ready for implementation plan
**Parent program:** Data-Provider Overhaul (`docs/superpowers/specs/2026-05-21-data-provider-overhaul-handoff.md` §3)
**Predecessor:** SP-2 (universe expansion, A–D) — MERGED + LIVE.
**Handoff:** `docs/superpowers/specs/2026-05-25-sp3-asset-class-expansion-handoff.md`

---

## 1. Goal & MVP scope

Add a strategy-level notion of **what asset class a strategy trades**, and thread it
through the lifecycle, sizer, backtest engine, and promotion guards — so the system can
host non-equity strategies without per-strategy special-casing.

**MVP instrument classes:** `equity`, `option`, `etp`.
**Deferred to SP-3.1:** `crypto` (24/7 session work) and direct `futures`.

The MVP additionally **mints one reference ETP strategy** (commodity-momentum on a small
liquid ETP basket) through backtest → `candidate`, as a real end-to-end proof of the rails.

### Operator decisions (brainstorming, 2026-05-26)
1. **Field model:** new enum `instrument_class` (NOT reuse `asset_class`). Avoids collision
   with the existing ticker-level `TickerMetadata.asset_class` (Alpaca vocabulary
   `us_equity`/`crypto`/`us_option`).
2. **MVP scope:** equity/option/etp + plumbing; crypto deferred. ETPs ride existing equity
   execution+session rails.
3. **Sizing:** dispatcher keyed by `instrument_class` (not inline branches) — keeps the live
   equity path byte-identical.
4. **Backtest:** one engine, made `instrument_class`-aware via loader + cost-model selection
   (not a fork).
5. **Thresholds:** per-class promotion-threshold table in code (not external YAML).
6. **Reference strategy:** YES — mint one concrete ETP strategy as proof (lands `candidate`).

---

## 2. Grounding — verified names (against live source @ `main` b83e930)

Every named convention below was grep/read-verified before this spec was committed
(per `feedback_spec_plan_codebase_grounding.md`). Implementers may rely on these.

| Name | Kind | Location (verified) |
|------|------|---------------------|
| `StrategyRecord` | dataclass | `src/strategies/lifecycle.py:110` — fields: `strategy_id, state, state_since, history, metadata, eligible_regimes, universe_filter_ref`. **No `instrument_class` today.** |
| `LifecycleStateMachine.from_manifest` | method | `src/strategies/lifecycle.py:267` |
| `LifecycleStateMachine.to_dict` | method | `src/strategies/lifecycle.py:677` |
| `LifecycleStateMachine.save_manifest` | method | `src/strategies/lifecycle.py:714` |
| `LifecycleStateMachine.can_transition` | method | `src/strategies/lifecycle.py:396`; candidate→live guard at `:424` reads `metadata['sharpe'|'max_drawdown']` + has `rec` in scope |
| `CANDIDATE_TO_LIVE_MIN_SHARPE = 0.5` | module const | `src/strategies/lifecycle.py:88` |
| `CANDIDATE_TO_LIVE_MAX_DRAWDOWN = 0.20` | module const | `src/strategies/lifecycle.py:89` (aliased to `PAPER_TO_LIVE_*` at `:91-92`) |
| `TickerMetadata` | dataclass | `src/strategies/universe_meta.py:7` — has `asset_class` (ticker-level) but **no ETF/leverage flag** |
| `size_positions` | fn (core sizer) | `src/execution/regime_blended_sizer.py:24` — produces orders with `notional_usd`, `qty`, `contributions[]` |
| `_build_sized_payload` | fn | `src/execution/regime_blended_sizer_live.py:39` — maps `size_positions` output → executor payload |
| `regime_blended_sizer_live.main` | fn | `src/execution/regime_blended_sizer_live.py:171` (imports `size_positions` at `:33`, `finalize_sized_payload` at `:36`) |
| `_alpaca_session_kind` | fn | `src/execution/alpaca_executor.py:144` → `rth|premarket|afterhours|closed` |
| executor asset-class read | inline | `src/execution/alpaca_executor.py:653` — reads Alpaca `class` ∈ {`us_equity`,`us_option`,`crypto`}; skips non-`us_equity` for ext-hours; `execute_single` at `:799`, `main` at `:1176` |
| `load_prices_panels` | fn | `src/backtest/unified_backtest.py:164` (reads `prices.parquet` only; `filter_quarantined` at `:175`) |
| `simulate_trade` | fn | `src/backtest/unified_backtest.py:208` (per-trade exit sim) |
| `run_backtest` | fn | `src/backtest/unified_backtest.py:572`; `run_backtest_with_resolver` at `:816` (SP-2 resolver param) |
| `aggregate_metrics` | fn | `src/backtest/unified_backtest.py:302` (has `sortino`/`calmar`/`mean_universe_size` from SP-2 C) |
| `scripts/backfill_universe_5y.py` | driver | `_promote_chunk` at `:342` (sole append-only write exception), `main` at `:886` |
| `registry._IMPL_MAP` | dict | `src/strategies/registry.py:17`; `load_strategy_class` at `:148` |
| `strategy_registry` | DB table | `src/database/migrations/012_execution_engine.sql:2` (mirrors some manifest fields) |
| `union_universe(today, ['live'])` | fn | SP-2 Phase A — drives collector data-fetch envelope from **live** strategies only |
| last migration | number | `117_lifecycle_audit_log.sql` → next free = **118** (only if needed; see §4.1) |

**Open grounding items resolved during implementation (probe required, not assumed):**
- **G1 — ETP identification data source.** `TickerMetadata` has no `isEtf`/leverage flag.
  FMP `/stable/profile` exposes `isEtf` (candidate); leverage factor has no confirmed source.
  Probe before relying. MVP does NOT need ticker-level ETP detection for the strategy field
  (strategies *declare* `instrument_class`); it is only needed for leveraged-ETP decay sizing,
  which is **deferred** (§3.2).
- **G2 — Alpaca ETP tradability + basket symbols.** Probe
  `alpaca asset list --status active` (filter for the chosen basket) to confirm each ETP is
  tradable/fractionable before minting the reference strategy. (Export only
  `ALPACA_API_KEY`/`ALPACA_API_SECRET`; do NOT `source .env`.)

---

## 3. Design

### 3.1 The `instrument_class` field (deliverable bucket A)

- Add `instrument_class: str` as a **top-level** field on `StrategyRecord`
  (alongside `state`/`universe_filter_ref`, NOT nested in `metadata`).
- **Enum (validated):** `equity`, `option`, `etp` are *routed* in MVP; `crypto`, `futures`
  are **reserved as valid enum values** (accepted by validation) but have **no handler**
  (sizer/backtest raise a clear "no handler registered for instrument_class=crypto" error).
  SP-3.1 adds handlers, not enum edits.
- **Silent-strip discipline** (`feedback_lifecycle_silent_strip.md`): thread through all three:
  1. `StrategyRecord` dataclass attribute (default `'equity'`).
  2. `from_manifest` — read with `rec.get('instrument_class', 'equity')` (**read-side backfill**).
  3. `to_dict` — **always emit** `instrument_class` (write-side; unlike the omit-when-None
     pattern, this field is always present so existing records backfill on first lifecycle write).
- **Existing-strategies backfill:** no migration script. The read-default (`'equity'`) +
  always-emit round-trip backfills all 129 strategies organically on their next lifecycle
  write — byte-identical behavior in the meantime.
- **Validation:** enum-membership check in `from_manifest`/`register`/`transition`; reject
  unknown values with a clear error (defends against subagent-authored typos).
- **Regression test (required):** load a manifest with `instrument_class='etp'` on strategy A,
  mutate strategy B via the state machine, `save_manifest`, assert A's `instrument_class`
  survives (the exact shape of the 2026-05-12 `eligible_regimes` incident).

### 3.2 Sizing dispatcher (deliverable bucket B)

- A dispatcher resolves each order's `instrument_class` (via `instrument_class_for(strategy_id)`
  — a helper that reads the manifest through `LifecycleStateMachine.from_manifest`, default
  `'equity'`) and routes:
  - `equity` / `etp` → **existing notional path, byte-identical** (no transform).
  - `option` → delta-equivalent exposure wrapper (notional scaled by |delta| so a 0.5-delta
    option consumes ~half the dollar exposure of the equivalent share notional). Greeks read
    from the same source the engine uses (`options_eod` / engine greeks filter).
  - `crypto` / `futures` → raise "no handler registered".
- **Hook point:** annotate/transform in the per-order path that already computes `notional_usd`
  (`size_positions` in `regime_blended_sizer.py`, or at the `_build_sized_payload` boundary in
  `regime_blended_sizer_live.py:39`). Implementer picks the cleaner of the two during the plan;
  the equity/etp branch MUST be a literal pass-through (no numeric change).
- **Leveraged/inverse-ETP decay sizing is DEFERRED** (data-gated by G1): ETPs size on plain
  notional in MVP. Leverage-divisor is a documented fast-follow once a leverage data source
  exists.
- **Kill-switch interaction:** when `OPENCLAW_INSTRUMENT_CLASS_ROUTING` is off (default), the
  dispatcher forces every order down the equity/notional path regardless of declared class
  (§3.6).

### 3.3 Backtest engine class-awareness (deliverable bucket B)

- Keep the single `unified_backtest.py` engine. Add a thin **data-loader + execution-cost-model
  selection** keyed by `instrument_class` (mirrors the sizer dispatcher):
  - `equity` / `etp` → `load_prices_panels()` (`prices.parquet`) + equity cost model.
  - `option` → greeks-aware backtest is a **scoped fast-follow** (no options strategy in the
    funnel yet). MVP behavior: record the backtested `instrument_class` on the result and use
    the equity path for any non-option class.
- The engine **records which `instrument_class` it backtested** (carried in the result dict /
  metadata) so promotion + dashboards can read it.
- Equity backtests stay numerically identical (the loader/cost-model selection defaults to the
  equity path).

### 3.4 Per-class promotion thresholds (deliverable bucket B)

- Replace the two global constants with a **typed dict keyed by `instrument_class`**, e.g.:
  ```python
  PROMOTION_THRESHOLDS = {
      'equity': {'min_sharpe': 0.5, 'max_drawdown': 0.20},
      'etp':    {'min_sharpe': 0.5, 'max_drawdown': 0.20},
      # option uses equity values as an EXPLICIT placeholder until research calibrates:
      'option': {'min_sharpe': 0.5, 'max_drawdown': 0.20},  # TODO(SP-4): calibrate
      # 'crypto' reserved — added in SP-3.1
  }
  ```
- Preserve `CANDIDATE_TO_LIVE_MIN_SHARPE` / `CANDIDATE_TO_LIVE_MAX_DRAWDOWN` as the
  `equity` entry (back-compat for any external import).
- `can_transition` (lifecycle.py:424) selects the threshold row from `rec.instrument_class`
  (already in scope) and applies it. Behavior for all current (equity) strategies is unchanged.

### 3.5 Executor (deliverable bucket B) — minimal

- ETPs are `us_equity` to Alpaca → already pass the `:653` asset-class check and ride equity
  execution + session rails. **No routing/session change required for MVP.**
- Make the executor `instrument_class`-aware only as a forward-looking pass-through (so SP-3.1
  crypto has an obvious seam). No `_alpaca_session_kind` change, no doctor "is market open"
  change, no 24/7 logic.

### 3.6 Gate / rollback (deliverable bucket D)

- **Kill-switch env `OPENCLAW_INSTRUMENT_CLASS_ROUTING` (default-OFF).** When off, every
  strategy is forced down the equity path (sizer + backtest loader + thresholds), regardless of
  declared `instrument_class` — ultimate safety on the live VPS.
- The field threading + backfill (§3.1) ship **ungated** (inert by equity-default).
- Flip ON after soak. Document in `.env.example` and a rollback note.

### 3.7 Reference ETP strategy (deliverable bucket C)

- Hand-build one **commodity-momentum** strategy on a small liquid ETP basket
  (candidate basket: `GLD`, `SLV`, `USO`, `DBC` — confirm tradability via G2; substitute liquid
  alternatives if any are not Alpaca-tradable).
- Archetype: cross-sectional / time-series momentum over the basket (rank by trailing return,
  go long top performer(s)); standard bracket. Implementer keeps the signal logic simple and
  well-documented — the point is to exercise the rails, not to discover alpha.
- Registered in `manifest.json` with `instrument_class='etp'` and an appropriate
  `universe_filter_ref` (or a small dedicated basket predicate); impl in
  `src/strategies/implementations/` + `registry._IMPL_MAP` entry.
- **Lands as `candidate`** after a passing backtest through the class-aware engine. Promotion
  to `live` is the operator's separate, deliberate call after soak (surfaced, not automated).
- The **§3.8 synthetic fixture stays** for unit-level edge cases (no-handler raises, threshold
  selection, silent-strip) without a manifest artifact.

### 3.8 Synthetic test fixture (required deliverable, bucket B)

- A synthetic ETP fixture strategy lives in **`tests/` only** (never registered in
  `manifest.json` / `_IMPL_MAP`). It exercises the full path — manifest field → resolver →
  sizer dispatcher → threshold guard → backtest cost model — and the reserved-but-unhandled
  `crypto`/`futures` raise paths. This is the runtime smoke test for the rails, independent of
  the live reference strategy.

---

## 4. Data & coverage

### 4.1 No schema migration (by design)
`instrument_class` lives on the manifest `StrategyRecord`; the lifecycle threshold guard and
the sizer resolver read it from there. `strategy_registry` (DB) does **not** require the column
for MVP logic. If a DB column is later wanted for dashboard queries, it is a purely additive
`ALTER TABLE strategy_registry ADD COLUMN IF NOT EXISTS instrument_class TEXT` (migration 118) —
out of MVP scope.

### 4.2 ETP price coverage (append-only)
- The reference strategy's basket needs sufficient `prices.parquet` history to backtest. Backfill
  via the existing `scripts/backfill_universe_5y.py` driver (`_promote_chunk`, the documented
  append-only exception). **NEVER delete/overwrite** existing rows (CLAUDE.md core invariant).
- **Coverage-freshness wrinkle (must handle):** `union_universe(today, ['live'])` builds the
  daily collector fetch envelope from **live** strategies only. A *candidate* ETP strategy's
  basket will therefore NOT be auto-collected day-to-day. Options (decide in plan):
  (a) add the basket tickers to `universe_config` as active so the collector keeps them fresh;
  (b) include `candidate` ETP-class strategies in the collector envelope for their declared
  basket. Recommendation: (a) for the 4-ticker basket (smallest, explicit, append-only).

---

## 5. Out of scope (explicitly deferred)

- **Crypto + 24/7 session logic** → SP-3.1 (session-awareness across doctor / executor /
  redeploy gates / intraday HMM `*/5 9-19` cron).
- **Greeks-aware options backtest** → fast-follow.
- **Leveraged/inverse-ETP decay sizing** → fast-follow (data-gated, G1).
- **Direct futures** → Alpaca-limited; reserved enum only.
- **Research-funnel asset-class awareness** (PaperHunter/StrategyCoder/Mastermind learning new
  archetypes) → that is **SP-4**, not SP-3.
- **Streaming/WebSocket** → SP-5.

---

## 6. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| Lifecycle silent-strip of new field | §3.1 threads dataclass+from_manifest+to_dict; required regression test |
| Live equity path regression (sizer is the LIVE submitter) | equity/etp branch is literal pass-through; kill-switch forces equity path; synthetic fixture + equity-parity assertion |
| Rails never exercised before SP-4 | §3.7 reference strategy (real, candidate) + §3.8 synthetic fixture |
| ETP basket not Alpaca-tradable | G2 probe before minting; substitute liquid alternatives |
| Candidate basket coverage goes stale | §4.2 — add basket to `universe_config` active |
| `migrate()` re-run transaction poisoning | N/A — no migration in MVP (§4.1) |
| Reference strategy promoted to live prematurely | lands `candidate`; live promotion is operator-only + surfaced |

---

## 7. Deliverable buckets (→ plan phasing)

- **A.** `instrument_class` field + threading (dataclass/from_manifest/to_dict) + validation +
  read-default backfill + silent-strip regression test. *Lands inert first.*
- **B.** Sizing dispatcher + per-class threshold table + class-aware backtest loader/cost-model +
  executor pass-through + **synthetic `tests/` fixture**.
- **C.** ETP coverage backfill (+ `universe_config` freshness) + reference ETP strategy + backtest
  → `candidate`.
- **D.** Kill-switch gate (`OPENCLAW_INSTRUMENT_CLASS_ROUTING`, default-OFF) + `.env.example` +
  soak + rollback note.

Order: A → B → C → D. Each task: one implementer subagent + two-stage review
(spec-compliance, then code-quality), per `superpowers:subagent-driven-development`, in a git
worktree.

---

## 8. Verification (before claiming done)

- Field threading: silent-strip regression test green; all 129 strategies still load; manifest
  round-trip diff shows only additive `instrument_class` keys.
- Equity parity: a known equity strategy's backtest + sizer output is numerically identical
  pre/post (kill-switch off AND on).
- Reference strategy: real backtest run produces metrics; lands `candidate`; `_IMPL_MAP` +
  manifest entry present; backtest reads its ETP basket from `prices.parquet`.
- Probes are real commands (G2 Alpaca asset list), not assumed.
- **Surface before any merge/deploy** (live VPS). Confirm LLM-usage headroom before any heavy
  cycle (monthly cap was hit twice 2026-05-25).
