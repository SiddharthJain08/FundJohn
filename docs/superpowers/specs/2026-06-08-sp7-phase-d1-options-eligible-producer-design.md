# SP-7 Phase D1 — `options_eligible` chain-probe producer (design)

**Date:** 2026-06-08
**Phase:** SP-7 Phase D, sub-task D1 (the HARD prerequisite of Phase D).
**Branch:** `feat/sp7-phase-d1-options-eligible` (off live tip `eac2cf9`, which has
Phase C merged — gates default-OFF).
**Parent docs:** `docs/superpowers/specs/2026-06-04-sp7-universe-expansion-design.md` (§6),
`docs/superpowers/specs/2026-06-08-sp7-phase-d-init-prompt.md` (D1 section),
`docs/sp7-phase-c-runbook.md` (the archive gate this un-blocks).

---

## 1. Problem

`OPTIONS_ELIGIBILITY_CACHE = data/.cache/options_eligibility.json`
(`src/pipeline/run_ticker_metadata_step.py:13`) is **read** by the daily metadata
writer but has **zero writers repo-wide** (verified 2026-06-08). Consequently
`options_eligible` is `False` for every `ticker_metadata_snapshots` row, and three
things are dead-on-arrival:

1. **The options-archive scoping** — `backfillers/alpaca_options.py:69`
   (`_select_archive_universe`) filters the union to `options_eligible` names;
   with all-False it returns **0 names**, so Phase C's
   `OPENCLAW_OPTIONS_ARCHIVE_RESOLVER_UNIVERSE` gate logs *"gate ON but 0
   options-eligible"* and falls back to `universe_config` (the gate is a no-op).
2. **The `options_eligible_only` predicate** (`universe_default.py:25`) +
   `large_cap_options` / `mid_cap_options` (`:42`, `:45`) — resolve to empty.
3. **Any option-strategy tier mint** that wants the options universe.

D1 builds the missing producer: a weekly job that enumerates which underlyings
have listed options and writes the `{symbol: bool}` cache the writer already reads.
**No consumer code changes** — D1 just makes the cache truthful.

## 2. Decisions (locked with operator, 2026-06-08)

| # | Decision | Choice |
|---|---|---|
| D1.a | What `options_eligible=true` means | **Listed-existence**: Alpaca lists ≥1 *active* option contract for the underlying. Liquidity is handled downstream (cap-tier predicates; the `options_eligible_only` predicate already ANDs `tradable` + `status='active'`). |
| D1.b | Refresh cadence | **Weekly**, standalone script + its own systemd timer, decoupled from the daily cycle. |
| D1.c | Probe mechanism | **Bulk enumeration**: page `alpaca option contracts --status active` (no underlying filter), collect the distinct set of `underlying_symbol`. ~130 sequential paged calls vs ~13.8k per-name probes. |

**Why bulk over per-name** (rescoped after grounding): the probe universe is
**13,845** active `us_equity` names (not the ~5k first assumed). Per-name probing
is ~13.8k trading-API calls/run (~70–90 min, 429-burst risk, multi-week initial
fill). Bulk enumeration is ~130 sequential pages (~10–15 min, complete in one run,
rate-limit-gentle). The bulk path's only real risk — a broken sweep wiping the
cache — is fully closed by a **completion-gate + sanity-floor + preserve-prior**
(§5).

## 3. The contract D1 must satisfy (grounded)

- **Output file:** `data/.cache/options_eligibility.json` = a flat JSON object
  `{symbol: bool}`. Symbol keys are **Alpaca form** (e.g. `AAPL`, `BRK.B`),
  matching `alpaca_tradable_universe.symbol`.
- **Consumer:** `run_ticker_metadata_step.py:49` `load_json(...)` →
  `ticker_metadata_writer.build_metadata_rows(...)` →
  `"options_eligible": options_cache.get(sym, False)` (`ticker_metadata_writer.py:124`;
  also the enrichment path `:190`). **Absent key ⇒ `False`.** So writing only the
  optionable subset is sufficient; an absent or `false` value both mean "not eligible".
- **Downstream readers** (no change): archive `backfillers/alpaca_options.py:69`;
  predicates `options_eligible_only`/`large_cap_options`/`mid_cap_options`;
  `TickerMetadata.options_eligible` (`universe_meta.py:20`); `_db_adapters.py:42`.

## 4. Architecture & components

```
docs/options-eligibility.{service,timer}   (systemd, weekly Sun 06:00 UTC, SHIPPED DISABLED)
        │  ExecStart
        ▼
python3 -m src.pipeline.options_eligibility        ← NEW module
        │
        ├── enumerate_optionable_underlyings()  → page `option contracts --status active`
        │        (limit 10000, sequential, follow next_page_token) → set[underlying_symbol], completed:bool
        ├── _load_universe()                    → alpaca_tradable_universe active us_equity (13,845)
        ├── _load_prior_cache()                 → data/.cache/options_eligibility.json (or {})
        ├── build_eligibility(optionable, univ) → {sym: sym in optionable  for sym in univ}
        ├── completion-gate + sanity-floor      → decide write vs keep-prior (§5)
        ├── _atomic_write_cache()               → tmp + os.replace
        └── summary log + Discord #data-alerts post
```

- **New module:** `src/pipeline/options_eligibility.py` (sibling of, and patterned on,
  `src/pipeline/backfillers/alpaca_options.py` — same `ALPACA_BIN`, subprocess +
  JSON parse, `_record_call` provider-health hook, soft time budget). Bulk paging is
  **sequential** (no `ThreadPoolExecutor`) — gentler on rate limits and the sweep is
  inherently ordered by page token.
- **CLI:** `python3 -m src.pipeline.options_eligibility [--dry-run] [--limit N] [--budget-s S]`.
  `--dry-run` writes to `/tmp/options_eligibility_dryrun.json` and skips the Discord post.
- **Systemd units:** `docs/openclaw-options-eligibility.{service,timer}` → installed
  at `/etc/systemd/system/` (a **system** oneshot unit, `User=claudebot`,
  `EnvironmentFile=/root/openclaw/.env` — mirroring `openclaw-options-archive.*`).
  Weekly `OnCalendar=Sat *-*-* 06:00:00 UTC` — early Saturday, before the weekend
  backtest refresh (12:00 UTC) and the mastermind corpus (14:00 UTC); the tier ladder
  runs Mon–Fri only, so the weekend is clear; I/O-bound → low contention. **Shipped
  disabled** — `sudo systemctl enable --now openclaw-options-eligibility.timer` is the
  activation step (see §8). Tunable slot.

## 5. The bulk sweep + safety (the load-bearing logic)

**Enumerate** (`enumerate_optionable_underlyings`):
- Loop: `option contracts --status active --limit 10000 [--page-token T]`; parse
  `{option_contracts, next_page_token}`; add each contract's `underlying_symbol` to a
  `set`; advance `T = next_page_token`; stop when `next_page_token` is null/absent.
- Returns `(optionable: set[str], completed: bool)`. `completed=True` **only** when a
  page returns no `next_page_token` (a genuinely terminal sweep). `completed=False` if
  the soft budget is exceeded mid-sweep or any page errors.

**Write decision** (`main`):
1. If `not completed` → **keep prior** (do not write); log `WARN incomplete sweep,
   prior cache retained`; exit 1.
2. Build `new = {sym: True for sym in sorted(optionable & universe)}` — a **full
   replace** with only the eligible names (absent ⇒ False at read time, per §3), so a
   name that lost its listing simply drops out of the fresh snapshot. `n_eligible = len(new)`.
3. **Sanity floor:** require `n_eligible >= max(ABS_FLOOR, 0.5 * prior_eligible_count)`
   where `ABS_FLOOR` defaults to `1000` (env `OPTIONS_ELIGIBILITY_MIN_FLOOR`). This
   catches both a degenerate empty/tiny result and a sweep that completed but returned
   implausibly few names. On failure → **keep prior**, log `WARN sanity floor`, exit 1.
4. **Atomic write:** serialize `new` to a temp file in the same dir, then `os.replace`
   onto `options_eligibility.json` (atomic on one filesystem; a crash never leaves a
   partial/corrupt cache).
5. Summary: `swept_pages / optionable_total / universe / eligible / newly_added /
   removed` → log + Discord `#data-alerts`. Exit 0.

**Net safety property:** the cache can only ever be replaced by a *complete,
plausibly-sized* fresh snapshot; any failure (API outage, partial sweep, degenerate
result) leaves last week's cache untouched. Eligibility can never be silently wiped —
mirrors the Phase-C "don't wipe on empty" discipline.

## 6. Observability

- **New `system_check`:** `src/system_checks/checks/options_eligibility_freshness.py`
  (tag `strategies`): PASS iff the cache exists, mtime ≤ 10 days, and eligible-count
  ≥ `ABS_FLOOR`; WARN on stale/empty/missing; never FAIL (it's an advisory). Run via
  `python3 -m system_checks --check options_eligibility_freshness`.
- **Weekly Discord summary** to `#data-alerts` (counts + run duration + page count).
- **provider_health:** each page records `record('alpaca', 'options_contracts', ...)`
  so a bad sweep surfaces on the Data Health tile.

## 7. Error handling

| Failure | Behavior |
|---|---|
| A page subprocess errors / non-zero rc / bad JSON | sweep aborts → `completed=False` → keep prior, exit 1 |
| Soft budget exceeded mid-sweep | `completed=False` → keep prior, exit 1 |
| Sweep completes but `n_eligible < floor` | keep prior, WARN, exit 1 |
| Crash during write | temp+`os.replace` ⇒ prior cache intact |
| Alpaca auth missing | first page errors → keep prior, exit 1 (never wipes) |

The producer never raises uncaught; `main()` returns 0 (wrote) / 1 (kept prior). The
weekly timer tolerates exit 1 (next week retries).

## 8. Safety — why D1 is inert to land, and activation

**Inert to land** (verified 2026-06-08):
- **No live strategy** uses an options predicate as `universe_filter_ref`
  (`options_eligible_only`/`large_cap_options`/`mid_cap_options` → 0 hits in the
  live manifest).
- The engine resolver gate `OPENCLAW_LIVE_UNIVERSE_RESOLVER` is **OFF** → the engine
  clamps every strategy to ≈sp500 regardless of metadata.
- The archive gate `OPENCLAW_OPTIONS_ARCHIVE_RESOLVER_UNIVERSE` is **OFF**.

So populating `options_eligible` changes **no live trading**. Its only effects are
to make the column truthful (so the archive gate becomes real the moment the operator
flips it, post-Phase-C) and to make the `options_eligible_only` predicate resolve to a
real set in backtests/shadow. (The shadow sidecar would show resolver-vs-clamp drift
for an options-predicate strategy — but none exist, so N/A.)

**Activation (operator-gated, fully reversible):**
1. Merge `feat/sp7-phase-d1-options-eligible` → live branch (gates unchanged).
2. Run once manually to seed the cache (`sudo -u claudebot python3 -m
   src.pipeline.options_eligibility`) and eyeball the summary.
3. Install the units to `/etc/systemd/system/`, `sudo systemctl daemon-reload`,
   `sudo systemctl enable --now openclaw-options-eligibility.timer`.
4. (Post-Phase-C, separate decision) flip `OPENCLAW_OPTIONS_ARCHIVE_RESOLVER_UNIVERSE=1`
   — now non-inert.
Rollback: `sudo systemctl disable --now openclaw-options-eligibility.timer`. The cache
is additive and under `data/.cache/` (not master data); never deleted.

## 9. Testing (TDD)

**Unit (mocked subprocess — no live API):**
- parse: `{option_contracts:[...]}` non-empty → underlyings collected; empty page →
  none; malformed JSON / non-zero rc → sweep marked incomplete.
- pagination: follows `next_page_token` across pages; terminates on null token;
  `completed=True` only on terminal page.
- `build_eligibility`: keys = our universe, value = membership in optionable set;
  symbol form preserved (incl. `BRK.B`).
- completion-gate: incomplete sweep → prior retained, no write.
- sanity-floor: `n_eligible < floor` (absolute and relative) → prior retained.
- atomic write: writes via temp+replace; simulated mid-write failure leaves prior intact.
- exit codes: 0 on write, 1 on any keep-prior path.

**Live smoke (manual, not CI):**
- Run against a tiny synthetic universe `[AAPL, MSFT, <known-non-optionable e.g. ZVZZT>]`
  → AAPL/MSFT `true`, ZVZZT `false`.
- Full `python3 -m src.pipeline.options_eligibility --dry-run` → confirm ~thousands
  eligible, completion-gate passes, summary sane.
- Then run `run_ticker_metadata_step` and confirm `ticker_metadata_snapshots.options_eligible`
  flips to `true` for AAPL on today's snapshot.

## 10. Non-goals / out of scope

- Liquidity tiering of eligibility (penny-program/OI floors) — explicitly deferred;
  existence-only per D1.a.
- Touching any consumer (predicates, archive, metadata writer) — D1 only writes the cache.
- Flipping `OPENCLAW_OPTIONS_ARCHIVE_RESOLVER_UNIVERSE` — separate post-Phase-C step.
- Per-name probing / `data option chain` snapshot path — rejected (market-data-dependent;
  wrong for a weekend timer).
- D2–D5 (mint menu, mint-time ladder, legacy decommission, dashboard) — separate sub-tasks.

## 11. Grounding appendix (verified 2026-06-08)

- Endpoint: `alpaca option contracts --status active [--underlying-symbols X]
  --limit N --page-token T`. Live response top keys `['option_contracts',
  'next_page_token']`; contract fields include `underlying_symbol`, `root_symbol`,
  `symbol`. AAPL → ≥1 contract (eligible); `ZVZZT` → 0 (not eligible). Bulk
  `--status active` (no underlying filter) returns contracts + `next_page_token`
  (sorted by underlying). Reference data — returned AAPL's contracts at 05:40 UTC with
  market closed (market-closed-safe). `--limit` max 10000.
- Universe: `alpaca_tradable_universe WHERE status='active'` → 13,845 rows, all
  `asset_class='us_equity'`.
- Auth: CLI env `ALPACA_API_KEY` + `ALPACA_SECRET_KEY` (present in `.env`; the
  systemd unit injects `.env` via `EnvironmentFile`).
- Cache contract: `ticker_metadata_writer.py:124` `options_cache.get(sym, False)`.
- Inert: 0 live strategies on an options predicate; resolver + archive gates OFF.
- Box: 2-core / 8 GB / no-swap — the sweep is sequential + I/O-bound (no CPU/memory
  pressure). Timer = Sat 06:00 UTC; the tier ladder runs Mon–Fri 01:00–13:00 UTC, so
  the Saturday slot is ladder-free.
