# Crypto Bars Self-Heal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The daily collect re-requests a trailing window of crypto daily bars so holes inside the coverage range heal themselves.

**Architecture:** Two pure helpers (`cryptoHealDays`, `_mergeGaps`) plus a 10-line branch in `runMarketPricesNonEquity`; the idempotent parquet merge (`keep='first'` on (ticker,date)) makes the re-fetch safe.

**Tech Stack:** Node (CommonJS), `node:test`.

**Spec:** `docs/superpowers/specs/2026-11-23-crypto-bars-self-heal-design.md`

## Global Constraints
- Env `OPENCLAW_CRYPTO_BARS_HEAL_DAYS`: default 10; `0` disables; non-numeric or negative → 10.
- Only `category === 'crypto'` gets the window; etf/index/forex behaviour byte-identical.
- Heal window is `[max(fromDate, today − healDays), toDate]` as ISO `YYYY-MM-DD` strings; merged with `store.getGaps` output into disjoint sorted ranges.
- One `notify` line per phase run when any crypto ticker is present: `🌐 Market prices: crypto self-heal window ${healDays} d (coverage-range gaps cannot see holes)`.
- Tests: `node --test tests/test_collector_crypto_heal.test.js` only; stub `store` (`getGaps`, `logRun`, `flushPrices`) and `_fillMarketTicker`/`fillPricesAlpacaCrypto` so no network/DB is touched. Never run the whole node suite (2-core production box).

## Review Focus
1. Coverage already at today, crypto ticker → still one fetch for the heal window (not "skipped"). → Task 1 test.
2. Coverage 15 days behind + heal 10 → merged into ONE range from the coverage gap start to today (no duplicate overlapping fetch). → Task 1 test.
3. `OPENCLAW_CRYPTO_BARS_HEAL_DAYS=0` → crypto behaves exactly like today (range gaps only). → Task 1 test.
4. ETF ticker with coverage at today → skipped, no fetch, unchanged. → Task 1 test.
5. `_mergeGaps` with adjacent ranges (to = D, from = D+1) → merged. → Task 1 test.

---

### Task 1: self-heal window in `runMarketPricesNonEquity`

**Files:**
- Modify: `src/pipeline/collector.js` (`runMarketPricesNonEquity` ~L2013–2075; add helpers near `_classifyMarketTicker`; export `cryptoHealDays`, `_mergeGaps` in `module.exports`)
- Test: `tests/test_collector_crypto_heal.test.js` (new; follow `tests/test_collector_options_flush.test.js` for the require/stub style)

**Interfaces:**
- Produces: `cryptoHealDays(env = process.env) -> number`; `_mergeGaps(a, b) -> Array<{from, to}>` (ISO date strings, disjoint, sorted by `from`).

- [ ] Step 1: write the failing tests (Review Focus 1–5 as five `test(...)` cases; drive `runMarketPricesNonEquity(['BTC-USD'])` / `(['SPY'])` with `store.getGaps` stubbed to `[]` or to `[{from, to}]`, `fillPricesAlpacaCrypto`/`fillPricesAlpaca` stubbed to record calls and return 1, `store.flushPrices` → `{flushed:0}`, `store.logRun` → no-op; use `process.env.OPENCLAW_CRYPTO_BARS_HEAL_DAYS` set/unset per test; freeze `Date.now` if needed for deterministic dates).
- [ ] Step 2: RED (`node --test tests/test_collector_crypto_heal.test.js`).
- [ ] Step 3: implement helpers + branch + notify.
- [ ] Step 4: GREEN; also run `node --test tests/test_collector_options_flush.test.js tests/test_collector_heartbeat.test.js`.
- [ ] Step 5: commit `feat(collector): crypto daily bars self-heal window (decision 4)`.

### Task 2: docs
- `docs/archive/changelog.md` entry; `docs/specs/2026-11-06-crypto-engine-whale-flow-design.md` §9 decision 4 note (closed: self-heal + backfill). Commit `docs: crypto bars self-heal as-built`.
