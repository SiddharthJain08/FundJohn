# Crypto daily bars: self-healing gap fetch + one-off backfill (Phase 3 operator decision 4)

## 1. Intent and why
Crypto trades 7 days a week but the price master is filled by the weekday collect. `store.getGaps` compares the
requested window against `data_coverage.date_from..date_to`; a hole INSIDE that range is invisible ("silent
permanent hole", store.js ~L49). Holes form whenever a day's market-prices phase does not write (aborted cycle,
provider error) while another writer (the intraday snapshot) stamps coverage forward. Measured 2026-11-23:
BTC-USD misses 11-14/15; ETH/ADA/DOGE/SOL/XRP miss 28 days each since 2026-06-06; AVAX 97 (from 2020);
29 of the 36 tradable crypto symbols have NO bars at all (out of scope here — Phase 4 strategy space).
Operator ruling 2026-11-23 ("go ahead" on decision 4): fix the collect defect and backfill.

## 2. Goals / non-goals
- Goals: (a) the daily collect heals recent crypto holes by itself; (b) one-off backfill of the 7 present
  crypto tickers; (c) no change to equity/ETF/index/forex paths.
- Non-goals: adding the 29 absent crypto tickers; recalibrating BTC regime tiers / S_m (BTC's only holes are
  11-14/15 — immaterial; the 11-12 calibration ran on a complete series through 11-12).

## 3. Approaches
A. **Trailing self-heal window for crypto (chosen).** In `runMarketPricesNonEquity`, a crypto ticker always
   requests `[today − N, today]` (N = `OPENCLAW_CRYPTO_BARS_HEAL_DAYS`, default 10, 0 disables) in addition to
   the coverage-range gaps; the parquet merge keeps existing rows (`keep='first'` on (ticker, date)), so the
   re-fetch is idempotent. Cost: ≤ 7 extra `alpaca data crypto bars` calls/day today (36 if all symbols get bars).
B. Hole detection from the parquet date set per ticker. Rejected: a parquet read per ticker per day for the
   same outcome within the window; heals arbitrarily old holes but those are the backfill's job.
C. Make the snapshot writer not stamp coverage. Rejected: coverage is written by the shared flush from the
   rows actually written; special-casing sources is fragile and would not heal provider-error holes.

## 4. What is decided
- `src/pipeline/collector.js` `runMarketPricesNonEquity`: `const healDays = cryptoHealDays()` (env parse,
  default 10, non-numeric/negative → 10, 0 → off). For `category === 'crypto'` and `healDays > 0`:
  `gaps = _mergeGaps(gaps, [{from: max(fromDate, isoDate(today − healDays)), to: toDate}])` where
  `_mergeGaps` unions/overlaps ranges into disjoint sorted ranges. One `notify` per phase when any crypto
  ticker is present: `🌐 Market prices: crypto self-heal window ${healDays} d (coverage-range gaps cannot see holes)`.
  Skip-count semantics: a crypto ticker with only the heal window still fetches (not counted as skipped).
- Pure helpers exported for tests: `cryptoHealDays(env = process.env) -> number`, `_mergeGaps(a, b) -> gaps`.
- Backfill (operator-run, outside the 22:05Z hive / 22:30Z fleet / 22:50Z flip windows, i.e. after 23:30Z):
  for each of the 7 present crypto tickers `collector.fillPricesAlpacaCrypto(t, '2021-01-01', today)` then one
  `store.flushPrices()`; verify missing-day counts; the nightly hive rebuild picks the rows up.
- Docs: changelog entry; crypto spec §7c/§9 note that decision 4 is closed.

## 5. Left to the builder
- Exact `_mergeGaps` implementation (sort by from, merge when `next.from <= cur.to + 1 day`).
- Test file name/location following `tests/test_collector_*.test.js` (node:test, stub `store`).
