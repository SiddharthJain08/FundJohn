# Six-repo Stream D — backtest engine and gates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the monthly-macro look-ahead behind a flag that joins the pit/gap epoch (D1), make backtest diagnostics additive trackers with a wall-clock budget (D3), and add an opt-in, parity-checked indicator cache with a measurement path (D2).

**Architecture:** D1 adds a shared as-of helper (`strategies/asof.py`), a per-series macro release-lag table, and a flag-gated availability cut in `aux_data_loader._macro_slice`. It also records `macro_pit` in `config_json` and extends the existing pit/gap flip gate and flip script to three flags. D3 moves the loose counters in `unified_backtest._per_bar_simulate` into `src/backtest/trackers.py` objects driven once per bar. The persisted `config_json` is then built from `run.info`, and is proven byte-identical by a golden synthetic fixture recorded before the refactor plus a two-checkout replay script. D2 adds `BaseStrategy.cached`, fed by an `IndicatorCacheTracker` that sets `_cache_source` before the OOS loop when `OPENCLAW_BT_INDICATOR_CACHE=1`. It also adds a parity assert in `validate_strategy`, a synthetic-panel measurement script, and three NEW `_cached` strategy files.

**Tech Stack:** Python 3.13, pandas 3 (Copy-on-Write), pytest, bash. No new dependencies.

**Spec:** `docs/specs/2026-10-19-six-repo-adoptions-spec.md` §4 (D1–D3), bound by §0, §6, §7 and ruling R2. Fit-review notes: `/root/.learnings/reviews/2026-10-19-six-repo-fit-review/NOTES-{questdb,timescaledb,tdengine,pypde}.md`.

## Global Constraints

Spec §0, verbatim:
- Master parquets and canonical Postgres tables are append-only (repo CLAUDE.md). New tables/columns only; never DELETE, never rewrite history. A one-off re-layout that preserves every row byte-for-byte (A1) is a re-write of the container, not of the data, and is allowed under R1.
- Backtest side is AUTHORITATIVE (2026-08-07 ruling). Anything that changes a backtest input (D1) lands flag-gated, default off, and flips only with a fleet epoch.
- Every new behaviour that can change sizing, signals or gate outcomes ships behind an `OPENCLAW_*` env flag, default = pre-change behaviour, byte-identical when unset. Pure bug fixes with no gated consumer (B2 OU estimator, B3 MC seed/bridge, A2 windowed reads, A4 doc/dormant) need no flag; the plan says which.
- Never read the whole `prices.parquet` or `options_eod.parquet` in a test or an implementer session. Tests use synthetic frames; parquet tests write a tiny file to `tmp_path`.
- Tests reach the REAL Postgres when `.env` is importable — stub every DB gate in fixtures (`monkeypatch` the connect function); fixture tickers must not be real symbols (`ZZTEST1`, not `AAA`).
- The box is 2-core / 8 GB / no swap and shared with the live pipeline. Implementers run single-file pytest only, `nice -n 19`, never the full suite, never while a fleet unit is active.
- `strategy_lint.ALLOWED_ROOTS` stays as is; new shared modules live under `src/strategies/` (already an allowed root) or `src/lib/`.
- Observability: every new gate/shadow emits one log line per cycle with a fixed prefix; a missing line means the process died, never "nothing to report". Never silent.
- Commit per task; edits, `git commit` and `git push` as separate tool calls (ERR-20261018-001).

Stream D flags and defaults (all unset = pre-change behaviour, byte-identical):

| Flag | Default | Effect when set |
|---|---|---|
| `OPENCLAW_MACRO_PIT=1` | unset (`0`) | `_macro_slice` cuts each series at `available_at <= as_of` instead of observation date `<= as_of` |
| `OPENCLAW_BT_MAX_RUNTIME_S=<s>` | `0` (off) | `MaxRuntimeTracker(s)` raises `FinishedBacktest`; the run is NOT persisted; CLI rc=1 |
| `OPENCLAW_BT_CONSISTENCY_ABORT=1` | unset | `ConsistencyTracker` aborts a run with a non-finite `pnl_pct` or mark (spec defect: the spec names no flag; see the report) |
| `OPENCLAW_BT_INDICATOR_CACHE=1` | unset | backtest loop sets `instance._cache_source = close_wide`, so `BaseStrategy.cached` serves prefix slices |
| `OPENCLAW_BT_CACHE_PARITY=1` | unset | `validate_strategy.validate` asserts cached slice == fresh compute on 3 random dates |

Rules for every task in this plan:
- Run steps are `nice -n 19 python3 -m pytest <one file> -q -W error` from the worktree root `/root/openclaw/.claude/worktrees/six-d`. Never run the full suite.
- Any test that drives `_per_bar_simulate` or `run_backtest` patches `strategies.aux_data_loader.load_aux_data`, so the real masters are never read. It also sets `OPENCLAW_BT_ASSET_GATE=off`, `OPENCLAW_BT_SPREAD_COSTS=0` and `OPENCLAW_BACKTEST_COUPLED_RECS=0` (the pattern in `tests/backtest/test_intraday_exit.py:172-206`).
- Executing `scripts/pit_gap_flip_after_fleet.sh`, `scripts/measure_tracker_parity.py capture` or `scripts/measure_indicator_cache.py` at full size are controller operations. Implementers only run their unit tests.
- Do not edit `src/strategies/manifest.json` or `src/strategies/registry.py` (Stream C ruling: the manifest is live and rewritten on main).

## Review Focus

1. **An unmapped macro series under `OPENCLAW_MACRO_PIT=1`.** This is a future ingest of a monthly series (e.g. CPI) with no lag entry. Expected behaviour: it never silently gets a daily lag. It falls back to lag 0, the same as legacy, and is named in a one-time `[macro_pit] WARN unmapped=` line. A drift test fails when a writer adds a series without a table entry. Test: Task 2 `test_unmapped_series_lag0_and_warned_once` and `test_every_writer_series_has_an_explicit_lag`.
2. **Partial flag state in `.env`.** `FINANCIALS_PIT=1` and `BT_GAP_FILL=open` are set but `MACRO_PIT` is not. Expected behaviour: the flip script must not report "already applied" and exit, or the macro flag never flips. Test: Task 3 `test_already_applied_requires_all_three_flags`.
3. **A strategy hands `cached` a transformed frame.** Examples are `prices.ffill()` or log prices: same index and columns, different values. Expected behaviour: a bypass (plain `fn(frame)`, counted), never a slice of `fn(source)`. Test: Task 7 `test_transformed_frame_same_shape_bypasses`.
4. **A budget abort mid-run.** Expected behaviour: no `INSERT INTO strategy_backtest_runs` reaches the cursor (a truncated run would become primary and demote the good run), and `main()` returns 1. Test: Task 5 `test_max_runtime_abort_persists_nothing_and_cli_returns_1`.
5. **A run whose every bar hits a `continue` path before signals.** Examples are regime NaN or a short prefix. Expected behaviour: the runtime budget still fires, because `handle` runs at the top of the loop body outside every `try`. Test: Task 5 `test_budget_fires_even_when_every_bar_continues`.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `src/strategies/asof.py` (create) | `asof_align` — the one backward as-of join with an explicit same-day rule | 1 |
| `src/strategies/aux_data_loader.py` (modify) | `MACRO_RELEASE_LAG_DAYS`, `available_at_frame`, flag-gated `_macro_slice` | 2 |
| `src/backtest/unified_backtest.py` (modify) | `config_json['macro_pit']` (3); tracker wiring + persist from `run.info` (5); cache tracker registration (7) | 3, 5, 7 |
| `scripts/pit_gap_flip_gate.py`, `scripts/pit_gap_flip_after_fleet.sh` (modify) | G1 requires three keys; flip sets three flags | 3 |
| `src/backtest/trackers.py` (create) | `Tracker` protocol, `TrackerCollection`, `BacktestContext`, `FinishedBacktest`, five trackers (+ `IndicatorCacheTracker` in 7) | 4, 7 |
| `scripts/measure_tracker_parity.py` (create) | two-checkout replay capture + compare, `--rows-json` seam | 6 |
| `src/strategies/base.py` (modify, ONE hunk) | `BaseStrategy.cached` | 7 |
| `src/strategies/validate_strategy.py` (modify) | `OPENCLAW_BT_CACHE_PARITY` assert | 8 |
| `scripts/measure_indicator_cache.py` (create) | synthetic-panel wall time + signal byte-identity, cache off vs on | 9 |
| `src/strategies/implementations/{S_ast_trend_following_effect_in_stocks,S_tr_02_hurst_regime_flip,S_two_day_pullback_atr_uptrend}_cached.py` (create) | measurement twins using `cached` | 10 |
| `docs/archive/changelog.md` (modify) | one entry per D item | 3, 5, 10 |

Ordering: D1 (Tasks 1–3), then D3 (Tasks 4–6), then D2 (Tasks 7–10). D3 precedes D2 because D2's cache source hook is a tracker in the D3 loop. The Task 5 golden fixture is recorded after Task 3, so it already carries `macro_pit`. Stream D merges after Stream C (spec §6.3); both touch `base.py`, and D rebases.

---

### Task 1: D1a — `asof_align` helper

**Files:**
- Create: `src/strategies/asof.py`
- Test: `tests/strategies/test_asof.py`

**Interfaces:**
- Produces: `asof_align(right: pd.DataFrame, left_index: pd.Index, *, on: str = 'available_at', by: str | None = None, tolerance: pd.Timedelta | None = None, same_day: str = 'after_close') -> pd.DataFrame`
  - `by is None`: `left_index` is a `DatetimeIndex`; the result is indexed by `left_index` (same order, duplicates kept, name kept).
  - `by` given: `left_index` is a 2-level `MultiIndex` `(by, date)`.
  - Columns: all of `right`'s columns, including `on`. A row with no match is all-NaN.
  - `same_day='after_close'` ⇒ `allow_exact_matches=False`; `'same_bar'` ⇒ `True`; any other value ⇒ `ValueError`.
- The module path and name are fixed: Stream C's `unguarded_parquet_read` lint rule (C plan Task 1) matches `from strategies.asof import asof_align` / `import strategies.asof` by AST.

- [ ] **Step 1: Write the failing tests** (synthetic frames, tickers `ZZTEST1`/`ZZTEST2`):
  - `test_after_close_excludes_same_day`: `right` has one row with `available_at=2026-03-02` and `v=1.0`. The left index is `[2026-03-02, 2026-03-03]`. Result `v` is `[NaN, 1.0]`.
  - `test_same_bar_includes_same_day`: same data with `same_day='same_bar'` gives `[1.0, 1.0]`.
  - `test_backward_latest_wins`: rows at 03-02 (v=1) and 03-04 (v=2); left `[03-03, 03-05]` gives `[1, 2]`.
  - `test_tolerance_expires_match`: `tolerance=pd.Timedelta(days=5)`; a left date 10 days after the last row gives NaN.
  - `test_by_key_isolated`: ZZTEST1 rows never fill ZZTEST2 left keys; MultiIndex result order equals the input order.
  - `test_unsorted_and_duplicate_left_index_preserved`: left `[03-05, 03-03, 03-05]` returns 3 rows in that order.
  - `test_nat_right_keys_dropped_nat_left_rows_nan`: a NaT `available_at` in `right` does not raise (merge_asof raises on NaT keys; see `aux_data_loader.py:537-545`). A NaT in `left_index` yields an all-NaN row.
  - `test_tie_on_available_at_last_input_row_wins`: two right rows with the same `available_at`; the later row in input order wins (stable mergesort).
  - `test_bad_same_day_raises`: `same_day='x'` gives `ValueError`; missing `on` column gives `KeyError`.
  - `test_tz_mismatch_raises_typeerror`: tz-aware `right[on]` with a naive `left_index` raises `TypeError` whose message names both dtypes.

- [ ] **Step 2: Run to verify failure**

Run: `nice -n 19 python3 -m pytest tests/strategies/test_asof.py -q -W error`
Expected: FAIL (`ModuleNotFoundError: strategies.asof`)

- [ ] **Step 3: Implement `asof_align` in `src/strategies/asof.py`.** Build a left frame with a positional `_asof_pos` column. Drop NaT keys on both sides. Sort both by the date key with `kind='mergesort'`. Call `pd.merge_asof(direction='backward', by=by, tolerance=tolerance, allow_exact_matches=…)`, then restore the input order by `_asof_pos` and reinsert the NaT-left rows as NaN. The module docstring states the same-day contract: an after-close publication (earnings, a macro print) is NOT usable on its own bar. It imports only pandas.

- [ ] **Step 4: Run to verify pass**

Run: `nice -n 19 python3 -m pytest tests/strategies/test_asof.py -q -W error`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add src/strategies/asof.py tests/strategies/test_asof.py
git commit -m "feat(asof): shared asof_align helper with explicit same-day rule (six-repo D1a)"
```

---

### Task 2: D1b — macro release lags, `available_at` builders, flag-gated `_macro_slice`

**Files:**
- Modify: `src/strategies/aux_data_loader.py`. Add constants after `_load_macro_series` (`:416-448`), change `_macro_slice` (`:451-464`), and add `import sys` to the import block (`:27-33`).
- Test: `tests/strategies/test_aux_macro_pit.py`, `tests/strategies/test_macro_pit_parity.py`

**Interfaces:**
- Consumes: nothing from Task 1. `_macro_slice` stays a filter, not a join; `asof_align` is for strategy authors.
- Produces:
  - `MACRO_RELEASE_LAG_DAYS: dict[str, int]` (calendar days)
  - `MACRO_DEFAULT_LAG_DAYS = 0`
  - `_macro_pit_enabled() -> bool`: `os.environ.get('OPENCLAW_MACRO_PIT', '0') == '1'`
  - `macro_release_lag(series: str) -> int`
  - `available_at_frame(kind: str) -> pd.DataFrame`, module-cached per kind. `kind` is one of:
    - `'macro'`: long `date, series, value, available_at`, where `available_at = date + lag`
    - `'financials'`: the existing `_financials_with_availability()`
    - `'insider'`: `ticker, date, available_at`, where `available_at = date` (the filing date; `collector.js:2185`)
    - `'options'`: the aggregates panel plus `available_at = date` (build date)
    - anything else ⇒ `ValueError`
  - Served shapes are unchanged: `available_at` never appears inside the per-ticker dicts, txn dicts, or macro Series that strategies receive.

The lag table: every series a writer puts in `macro.parquet` (from `ingest_macro_rates.FRED_SERIES` `:75-85`, `NYFED_TYPES` `:88`, `cboe_vol_indices.VOL_INDICES` `:41-46`), plus the spec's monthly names kept for future ingestion:

```python
MACRO_RELEASE_LAG_DAYS: dict[str, int] = {
    # CBOE vol-index closes: same-bar close, like the price bar (spec silent; see report)
    'VIX': 0, 'VIX3M': 0, 'VIX9D': 0, 'VVIX': 0,
    # FRED daily (spec: "daily FRED series: 1")
    'DGS1MO': 1, 'DGS3MO': 1, 'DGS6MO': 1, 'DGS1': 1, 'DGS2': 1, 'DGS5': 1,
    'DGS10': 1, 'DGS30': 1, 'DTB3': 1, 'T10Y2Y': 1, 'T10Y3M': 1, 'DFF': 1,
    'SOFR': 1, 'BAMLH0A0HYM2': 1, 'BAMLC0A0CM': 1, 'DTWEXBGS': 1,
    'DCOILWTICO': 1, 'DEXUSEU': 1, 'DEXJPUS': 1,
    # NY Fed reference rates: published ~08:00 ET next business day
    'NYFED_SOFR': 1, 'NYFED_EFFR': 1, 'NYFED_OBFR': 1, 'NYFED_TGCR': 1, 'NYFED_BGCR': 1,
    # Monthly, observation dated the 1st (spec values)
    'INDPRO': 45,
    'CPIAUCSL': 45, 'UNRATE': 40, 'PAYEMS': 40, 'GDP': 90,   # not ingested today
}
```

Cut semantics (pinned): visible iff `available_at <= as_of`, i.e. `s.loc[:ts - pd.Timedelta(days=lag)]`. Each served Series keeps its observation-date index; only the cut moves. With the flag on, the first `_macro_slice` call in the process prints exactly one stderr line:

```
[macro_pit] on series=<n> mapped=<m> unmapped=<comma list or ->
```

If any series is unmapped it prints instead:

```
[macro_pit] WARN unmapped=<list> lag=0
```

With the flag unset, the body is the existing loop, byte-identical, and nothing is printed.

- [ ] **Step 1: Write the failing tests in `tests/strategies/test_aux_macro_pit.py`.** Each test writes a tiny `macro.parquet` to `tmp_path`, monkeypatches `adl.MACRO_PATH`, resets `adl._MACRO_SERIES = None` and the PIT-announce module flag, and uses `monkeypatch.setenv`/`delenv('OPENCLAW_MACRO_PIT')`. Tests:
  - `test_flag_unset_is_legacy_observation_cut`: INDPRO obs `2026-09-01` is visible on `2026-09-05` with the flag unset.
  - `test_monthly_boundary_indpro`: flag=1, INDPRO obs `2026-09-01`. It is hidden on `2026-10-15` (09-01 + 45 = 10-16) and visible on `2026-10-16`. The served Series index max stays the observation date `2026-09-01`.
  - `test_daily_boundary_dgs10`: flag=1, DGS10 obs Monday `2026-10-12`. It is not visible on the `2026-10-12` bar and is visible on `2026-10-13`.
  - `test_vix_identical_both_states`: the VIX Series is equal under flag 0 and 1 (`pd.testing.assert_series_equal`).
  - `test_unmapped_series_lag0_and_warned_once` (Review Focus 1): series `ZZMACRO` with flag=1 is served with the legacy cut. capsys stderr has exactly one `[macro_pit] WARN unmapped=ZZMACRO` after two `_macro_slice` calls.
  - `test_every_writer_series_has_an_explicit_lag` (Review Focus 1): `ast`-parse `src/ingestion/ingest_macro_rates.py` for the `FRED_SERIES` and `NYFED_TYPES` tuples (no import: the module adds to `sys.path`). Also parse `src/ingestion/cboe_vol_indices.py` for `VOL_INDICES` second elements. Every FRED id, every `'NYFED_' + type`, and every vol-index name is a key of `MACRO_RELEASE_LAG_DAYS`.
  - `test_available_at_frame_macro_matches_cut`: for 3 dates, `{s: df[df.available_at <= ts]}` from `available_at_frame('macro')` equals the flag-on `_macro_slice(ts)` series by series.
  - `test_available_at_frame_insider_is_filing_date` and `test_available_at_frame_options_is_build_date`: tiny fixture parquets via monkeypatched `INSIDER_PATH`/`AGG_PATH`. `available_at == pd.to_datetime(date)`, and `_insider_slice` / `_day_slice` dict keys are unchanged (no `available_at` key).
  - `test_available_at_frame_unknown_kind_raises`.
  - Also re-run the existing `tests/strategies/test_aux_macro_slice.py` unchanged in Step 4.

- [ ] **Step 2: Write the parity test `tests/strategies/test_macro_pit_parity.py`** (spec D1 parity, both flag states). Use the fixture pattern of `tests/strategies/test_financials_pit_parity.py:41-55`, with `engine.ROOT` monkeypatched to `tmp_path`. Call `engine.load_aux_data(['ZZTEST1'], as_of=T, needed_kinds={'macro'})`; `needed_kinds` is mandatory because without it the sentiment block queries Postgres. Set `T = 2026-10-15`. Build two frames:
  - `PUB`: only rows already published by T. VIX through 10-15, DGS10 through 10-14, INDPRO through 08-01.
  - `FULL`: `PUB` plus the unpublished rows. DGS10 10-15 and INDPRO 09-01.

  Tests:
  - `test_flag_on_backtest_on_full_equals_live_on_published`: flag=1, `adl._macro_slice(T)` with `adl.MACRO_PATH=FULL` equals `engine…['macro']` with the engine master holding `PUB`, for every series (`assert_series_equal`, names checked).
  - `test_flag_off_backtest_equals_live_when_everything_published`: flag=0, adl on `PUB` equals engine on `PUB`.
  - `test_flag_off_on_full_leaks_unpublished_rows`: flag=0, adl on `FULL` contains INDPRO 09-01. This pins the look-ahead the flag removes.
  - `test_live_engine_ignores_the_flag`: the engine dict on `FULL` is identical under `OPENCLAW_MACRO_PIT` 0 and 1 (live is unchanged by D1).

- [ ] **Step 3: Run to verify failure**

Run: `nice -n 19 python3 -m pytest tests/strategies/test_aux_macro_pit.py -q -W error` and the same for `tests/strategies/test_macro_pit_parity.py`
Expected: FAIL (`AttributeError: … MACRO_RELEASE_LAG_DAYS`; the flag-on parity tests fail on the INDPRO row)

- [ ] **Step 4: Implement** the constants, `_macro_pit_enabled`, `macro_release_lag` (unmapped ⇒ `MACRO_DEFAULT_LAG_DAYS`), `available_at_frame` (cached in a module dict `_AVAIL_FRAMES`; `_reset_financials_caches` also clears its `'financials'` and `'options'` entries), the flag branch in `_macro_slice`, and the one-time announce line (module flag `_MACRO_PIT_ANNOUNCED`). Update the `_macro_slice` docstring and the `load_aux_data` docstring `'macro'` bullet: "point-in-time on observation date; on availability date under OPENCLAW_MACRO_PIT=1".

- [ ] **Step 5: Run to verify pass**

Run: the two new files plus `nice -n 19 python3 -m pytest tests/strategies/test_aux_macro_slice.py -q -W error`, and `tests/strategies/test_financials_pit_parity.py`
Expected: PASS, with the existing files unchanged

- [ ] **Step 6: Commit**

```bash
git add src/strategies/aux_data_loader.py tests/strategies/test_aux_macro_pit.py tests/strategies/test_macro_pit_parity.py
git commit -m "feat(aux): macro release lags + available_at builders; OPENCLAW_MACRO_PIT availability cut (six-repo D1b)"
```

---

### Task 3: D1c — `config_json['macro_pit']`, three-key G1, three-flag flip script

**Files:**
- Modify: `src/backtest/unified_backtest.py`. In the config_json literal, insert after `'financials_pit'` (`:1880`).
- Modify: `scripts/pit_gap_flip_gate.py` (`:1-25` docstring, `verdict` `:35-57`, `_rows_from_db` `:60-71`)
- Modify: `scripts/pit_gap_flip_after_fleet.sh` (header `:1-22`, already-applied `:57-62`, apply `:83-91`, post text `:104-106`)
- Modify: `docs/archive/changelog.md` (newest-first entry)
- Test: `tests/backtest/test_config_json_macro_pit.py` (create), `tests/scripts/test_pit_gap_flip_gate.py` (modify), `tests/scripts/test_pit_gap_flip_after_fleet_sh.py` (create)

**Interfaces:**
- Consumes: the env flag name `OPENCLAW_MACRO_PIT` (Task 2).
- Produces:
  - `config_json['macro_pit']: bool`, written unconditionally for every run. Key order: immediately after `financials_pit`.
  - Gate row shape: `[strategy_id, gap_fill, financials_pit, run_at_iso, macro_pit]`. The 5th element is appended so legacy 4-element rows still parse; a missing 5th element means `False` (lagging).
  - SQL adds `COALESCE((config_json->>'macro_pit')::boolean, false)` as the 5th selected column.

- [ ] **Step 1: Write the failing tests.**
  - `tests/backtest/test_config_json_macro_pit.py`: copy the `TestConfigJsonExitHook._config_json_of` capture (`tests/backtest/test_open_book.py:400-434`), additionally patching `strategies.aux_data_loader.load_aux_data` to return `{'options': {}}`. Tests:
    - `test_macro_pit_false_when_unset` (`cfg['macro_pit'] is False`)
    - `test_macro_pit_true_when_set` (env `OPENCLAW_MACRO_PIT=1`)
    - `test_macro_pit_key_follows_financials_pit`: `list(cfg).index('macro_pit') == list(cfg).index('financials_pit') + 1`
  - `tests/scripts/test_pit_gap_flip_gate.py`: the six existing tests get `True` appended as a 5th element on every row, so they keep their meaning. Add:
    - `test_a_row_missing_macro_pit_lags` (5th element False ⇒ NOT_YET, sid listed)
    - `test_legacy_four_column_row_lags_not_crashes` (Review Focus 2's sibling: a 4-element row counts as lagging)
    - `test_detail_names_all_three_keys` (the G1 detail line contains `gap_fill`, `financials_pit`, `macro_pit`)
  - `tests/scripts/test_pit_gap_flip_after_fleet_sh.py`: static text assertions only. Executing the script would `cd /root/openclaw`, read the production `.env`, and call `systemctl`. Tests:
    - `test_already_applied_requires_all_three_flags` (Review Focus 2): the already-applied `if` line contains `OPENCLAW_BT_GAP_FILL=open`, `OPENCLAW_FINANCIALS_PIT=1` and `OPENCLAW_MACRO_PIT=1`.
    - `test_apply_sets_macro_pit`: a `sed … s|^OPENCLAW_MACRO_PIT=.*|OPENCLAW_MACRO_PIT=1|` line and an append fallback both exist.
    - `test_kill_switch_text_names_macro_pit`: the APPLIED post contains `OPENCLAW_MACRO_PIT=0`.
    - `test_bash_syntax_ok`: `subprocess.run(['bash', '-n', path]).returncode == 0`. Syntax check only; nothing executes.

- [ ] **Step 2: Run to verify failure**

Run each of the three test files with `nice -n 19 python3 -m pytest <file> -q -W error`
Expected: FAIL (`KeyError: 'macro_pit'`; the gate passes rows lacking macro_pit; the grep assertions fail)

- [ ] **Step 3: Implement.**
  - config_json line, with a comment naming D1 and the gate: `'macro_pit': os.environ.get('OPENCLAW_MACRO_PIT', '0') == '1',`
  - Gate: in `verdict`, `macro = bool(row[4]) if len(row) > 4 else False`; lagging unless `gap_fill == 'open' and fin_pit is True and macro is True`. The G1 detail line reads `G1 fleet (gap_fill=open, financials_pit, macro_pit): live=… on_new=… lagging=… (max N) -> OK|NOT_YET`. Update the docstring row-shape text and the SQL.
  - Shell: the already-applied check greps all three; apply sets `OPENCLAW_MACRO_PIT=1` (sed-or-append, same pattern as `:84-87`); the `flags set:` regex includes `MACRO_PIT`. Rewrite the post text to name three flags, with kill switch `OPENCLAW_FINANCIALS_PIT=0 + OPENCLAW_BT_GAP_FILL=level + OPENCLAW_MACRO_PIT=0`. Update the header comment to name spec 2026-10-19 R2.
  - Changelog entry: D1 behind `OPENCLAW_MACRO_PIT`; G1 now three keys. **Controller note**: the fleet drop-in `/etc/systemd/system/openclaw-fleet-overnight-resume.service.d/pit-gap.conf` must carry `OPENCLAW_MACRO_PIT=1` alongside the two existing flags when the R2 epoch is armed.

- [ ] **Step 4: Run to verify pass** (same three files, plus `tests/backtest/test_open_book.py::TestConfigJsonExitHook`)
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/backtest/unified_backtest.py scripts/pit_gap_flip_gate.py scripts/pit_gap_flip_after_fleet.sh docs/archive/changelog.md tests/backtest/test_config_json_macro_pit.py tests/scripts/test_pit_gap_flip_gate.py tests/scripts/test_pit_gap_flip_after_fleet_sh.py
git commit -m "feat(gate): config_json macro_pit; pit/gap G1 + flip script require three flags (six-repo D1c, R2)"
```

---

### Task 4: D3a — `src/backtest/trackers.py` (pure module)

**Files:**
- Create: `src/backtest/trackers.py`
- Test: `tests/backtest/test_trackers.py`

**Interfaces:**
- Produces:
  - `class FinishedBacktest(Exception)` — `reason: str` attribute; `str()` is the message.
  - `class InconsistentBacktest(FinishedBacktest)`.
  - `@dataclass class BacktestContext` with these fields:
    - `instance`
    - `close_wide: pd.DataFrame`
    - `strategy_id: str | None`
    - `oos_dates: pd.DatetimeIndex`
    - `trades: list` (the loop's own list object)
    - `counters: dict` (the event sink; the SAME dict passed to `advance_open_book(counters=…)`)
    - `current_date: pd.Timestamp | None = None`
    - `started: float` (monotonic, set by `TrackerCollection.initialize`)
  - `class Tracker(Protocol)`: `name: str`; `initialize(ctx: BacktestContext) -> None`; `handle(ctx: BacktestContext, bar_idx: int) -> None`; `finalize(info: dict) -> None`.
  - `class TrackerCollection`: `__init__(trackers: list)`; `initialize(ctx)`; `handle(ctx, bar_idx)` calls every tracker in order, and a `FinishedBacktest` propagates; `finalize() -> dict` returns a fresh `info` after every tracker's `finalize(info)`; `names() -> list[str]`.
  - Trackers:
    - `ExitCensusTracker(census_fn)`: finalize sets `info['exit_reasons'] = census_fn(ctx.trades)`. The census stays in `unified_backtest.exit_reason_census`; it is injected to avoid a circular import.
    - `HookTracker`: finalize sets `info['hook_exits']`, `info['hook_raised']`, `info['first_hook_raise']` from `ctx.counters` (defaults 0, 0, None).
    - `IntradayBarTracker`: finalize sets `info['intraday_trades']`, `info['intraday_no_bars']`, `info['intraday_missing_entry_bar']` from `ctx.counters` (default 0).
    - `MaxRuntimeTracker(seconds: float, clock=time.monotonic)`: handle raises `FinishedBacktest(f'max_runtime: elapsed {e:.1f}s > budget {seconds:g}s at bar {bar_idx}/{len(ctx.oos_dates)} ({ctx.current_date.date() if ctx.current_date is not None else "drain"})')` when `clock() - ctx.started > seconds`. finalize sets `info['elapsed_s']`.
    - `ConsistencyTracker`: handle scans `ctx.trades[self._seen:]` and raises `InconsistentBacktest('nonfinite: <ticker> <entry_date> <field>')` on a non-finite `pnl_pct` or any non-finite `daily_marks[i][1]`, then advances `_seen`. finalize sets `info['consistency_checked'] = _seen`.

- [ ] **Step 1: Write the failing tests** (pure; no unified_backtest import):
  - `test_collection_calls_in_order_and_finalize_merges`: two recording trackers.
  - `test_finished_backtest_propagates_from_handle`.
  - `test_exit_census_uses_injected_fn_on_ctx_trades`.
  - `test_hook_tracker_defaults_when_no_hook_counts` gives `(0, 0, None)`.
  - `test_intraday_tracker_reads_shared_counters`: the loop increments `ctx.counters['intraday_no_bars']` twice, which yields 2.
  - `test_max_runtime_with_fake_clock`: the clock returns `[0, 5, 11]`; `MaxRuntimeTracker(10)` raises on the third handle with `'max_runtime'` and `'budget 10s'` in the message. No sleep.
  - `test_consistency_raises_on_nan_mark_and_on_nan_pnl`, `test_consistency_scans_each_trade_once` (`_seen` advances).

- [ ] **Step 2: Run** `nice -n 19 python3 -m pytest tests/backtest/test_trackers.py -q -W error`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implement `src/backtest/trackers.py`** (stdlib + pandas/math only).

- [ ] **Step 4: Run** the same command. Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/backtest/trackers.py tests/backtest/test_trackers.py
git commit -m "feat(backtest): tracker protocol, collection, census/hook/intraday/runtime/consistency trackers (six-repo D3a)"
```

---

### Task 5: D3b — wire trackers into `_per_bar_simulate`; persist from `run.info`

**Files:**
- Modify: `src/backtest/unified_backtest.py`. Touch these points:
  - `_per_bar_simulate` counters `:1039-1048`, `:1080-1081`
  - the loop head `:1094`
  - intraday increments `:1245-1251`, `:1296-1300`
  - the drain loop `:1325-1350`
  - the hook log `:1372-1375`
  - the return dict `:1390-1406`
  - persist `:1866-1898`
- Create: `tests/backtest/fixtures/tracker_parity_golden.json`
- Modify: `docs/archive/changelog.md`
- Test: `tests/backtest/test_tracker_parity_golden.py`, `tests/backtest/test_tracker_wiring.py`

**Interfaces:**
- Consumes: Task 4 names.
- Produces:
  - `_per_bar_simulate(...)` returns every existing key, with values now sourced from `info`, plus `'info': dict`.
  - Persist builds the counter, hook and census keys from `_info = sim.get('info') or {}`: `int(_info.get('intraday_trades', 0))` etc.
  - `'exit_reasons'` is `_info['exit_reasons'] if 'exit_reasons' in _info else exit_reason_census(trades)`; the options path has no `info`.
  - Key order and every other key are unchanged. D3 adds NO config_json keys.
  - Collection membership: `ExitCensusTracker(exit_reason_census)`, `HookTracker()` and `IntradayBarTracker()` always. `MaxRuntimeTracker(s)` when `float(os.environ.get('OPENCLAW_BT_MAX_RUNTIME_S', '0') or 0) > 0`. `ConsistencyTracker()` when `OPENCLAW_BT_CONSISTENCY_ABORT == '1'`.
  - Loop contract:
    - `collection.handle(ctx, i)` is the FIRST statement of the `for i, current_date in enumerate(oos_dates)` body, after `ctx.current_date = current_date`, outside every `try`.
    - It is also the first statement of each drain iteration (`bar_idx = len(oos_dates) + k`, `ctx.current_date = _dt`).
    - It runs once more after the drain, before `finalize()`.
    - The `hook_counters` local becomes `ctx.counters`; `advance_open_book`'s signature is unchanged (`test_exit_hook_live_parity.py` uses it).
    - The three intraday locals become `ctx.counters['intraday_…'] = ctx.counters.get(…, 0) + 1`.
  - One stdout line per run after finalize: `[unified_backtest] [bt_trackers] trackers=<names> bars=<len(oos_dates)> elapsed_s=<x.x> max_runtime_s=<s|0> consistency=<on|off>`.
  - `FinishedBacktest` propagates out of `run_backtest` before the persist block, so no INSERT happens. `main()`'s existing `except Exception` logs `FAIL: FinishedBacktest: max_runtime: …` and returns 1; the fleet runner then does not mark the strategy done.

- [ ] **Step 1: Write the characterization test BEFORE touching the loop.** `tests/backtest/test_tracker_parity_golden.py` defines `_capture(scenario) -> dict`, which uses `run_backtest(..., conn=spy_conn, commit=False, fill_model='same_close', max_hold_days=21)` with the mocks of `test_intraday_exit.py:172-206`. It records:
  - `config_json`: the exact INSERT param string; key order is part of byte-identity.
  - `trade_rows`: each `execute_values` row for `strategy_backtest_trades` with element 0 (`run_id`, a uuid4) dropped, `json.dumps(..., default=str)`.
  - `run_row`: the INSERT params minus index 0 (`run_id`) and index 2 (`code_sha`), `json.dumps(default=str)`.

  Scenarios, all with ticker `ZZTEST1` on synthetic bars:
  - `plain`: signals every 5th bar over 40 bars.
  - `hook_exits`: exit-hook stub, exits on day 2.
  - `hook_raised`: `should_exit` raises.
  - `intraday_ok`: `exit_on='session_close'`, `entry_bar_et='10:00'`, with a 30m aux frame.
  - `intraday_no_bars`: opted in, aux without 30m. Covers the entry-open site `:1248-1251`.
  - `intraday_sim_none`: `simulate_intraday_trade` patched to return None. Covers site `:1296-1300`.
  - `intraday_missing_entry_bar`.
  - `event_gated`: `OPENCLAW_BT_EVENT_GATE=1` with `lib.macro_events.gated_sessions` patched to gate one session.
  - `asset_gated`: `asset_gate={'ZZTEST1': (False, True)}` via the patched `load_bt_asset_gate`.
  - `options_dispatch`: `instrument_class='option'` with `options_backtest.simulate` patched to return the minimal dict at `options_backtest.py:309-311`.

  `test_golden_matches(scenario)` is parametrized over all ten and compares `_capture` against `tests/backtest/fixtures/tracker_parity_golden.json[scenario]` for string equality on all three fields. `RECORD_GOLDEN=1` writes the fixture instead of asserting.

- [ ] **Step 2: Record the golden on the PRE-refactor code and verify it.**

Run: `RECORD_GOLDEN=1 nice -n 19 python3 -m pytest tests/backtest/test_tracker_parity_golden.py -q -W error`, then the same command without `RECORD_GOLDEN`
Expected: fixture written; second run PASS (10)

- [ ] **Step 3: Commit the fixture alone** (so the reviewer sees it predates the refactor)

```bash
git add tests/backtest/test_tracker_parity_golden.py tests/backtest/fixtures/tracker_parity_golden.json
git commit -m "test(backtest): golden config_json+trades characterization before tracker refactor (six-repo D3b)"
```

- [ ] **Step 4: Write the failing wiring tests** `tests/backtest/test_tracker_wiring.py`:
  - `test_sim_returns_info_with_census_and_counters`: `sim['info']` has `exit_reasons`, `hook_exits`, `intraday_no_bars`.
  - `test_bt_trackers_line_emitted_once` (capsys: exactly one `[bt_trackers]`).
  - `test_max_runtime_abort_persists_nothing_and_cli_returns_1` (Review Focus 4): env `OPENCLAW_BT_MAX_RUNTIME_S=1`, `trackers.time.monotonic` patched to jump by 10 s per call. `run_backtest` raises `FinishedBacktest`, and the spy cursor saw no `INSERT INTO strategy_backtest_runs`. Then `main()` with `sys.argv=['x', '--strategy-id', 'stub_fm']` and `run_backtest` patched to raise `FinishedBacktest('max_runtime: …')` returns 1, and stdout contains `FAIL: FinishedBacktest`.
  - `test_budget_fires_even_when_every_bar_continues` (Review Focus 5): all regimes NaN (every bar `continue`s at `:1119-1120`), same clock patch. `_per_bar_simulate` raises `FinishedBacktest`.
  - `test_consistency_off_by_default_nan_mark_run_completes` and `test_consistency_on_aborts_nan_mark_run`: a bars frame with a NaN close mid-trade.
  - `test_unset_flags_build_only_three_trackers`.

- [ ] **Step 5: Run** `nice -n 19 python3 -m pytest tests/backtest/test_tracker_wiring.py -q -W error`
Expected: FAIL

- [ ] **Step 6: Implement the wiring** per Interfaces.

- [ ] **Step 7: Run all of these:** `tests/backtest/test_tracker_wiring.py`, `tests/backtest/test_tracker_parity_golden.py` (must pass with no fixture change), `tests/backtest/test_intraday_exit.py`, `tests/backtest/test_open_book.py`, `tests/backtest/test_event_gate_backtest_mirror.py`, `tests/backtest/test_exit_census.py`, `tests/execution/test_exit_hook_live_parity.py`, each as its own `nice -n 19 python3 -m pytest <file> -q -W error`
Expected: PASS; `git diff tests/backtest/fixtures/` is empty

- [ ] **Step 8: Commit** (with a changelog entry for D3 trackers + both flags)

```bash
git add src/backtest/unified_backtest.py tests/backtest/test_tracker_wiring.py docs/archive/changelog.md
git commit -m "feat(backtest): per-bar tracker collection; config_json from run.info; OPENCLAW_BT_MAX_RUNTIME_S / OPENCLAW_BT_CONSISTENCY_ABORT (six-repo D3b)"
```

---

### Task 6: D3c — `scripts/measure_tracker_parity.py`

**Files:**
- Create: `scripts/measure_tracker_parity.py`
- Test: `tests/scripts/test_measure_tracker_parity.py`

**Interfaces:**
- CLI:
  - `capture --out PATH [--repo PATH] [--run-ids a,b,c | --rows-json PATH] [--env-file /root/openclaw/.env]`
  - `compare A.json B.json`
- `--repo` (default: this script's repo) is the code root inserted first on `sys.path` (`<repo>`, `<repo>/src`) before importing `backtest.unified_backtest`. The controller captures the pre-D3 checkout and the D3 checkout on the same data snapshot, then compares.
- `--rows-json`: a JSON list of `[run_id, strategy_id, config_json]` (`config_json` is a dict or str), used INSTEAD of Postgres.
- DB default: with `--run-ids`, `SELECT run_id, strategy_id, config_json FROM strategy_backtest_runs WHERE run_id = ANY(%s)`. Otherwise it auto-picks the newest primary run of each of three kinds:
  - `config_json->>'exit_hook' = 'true'`
  - `(config_json->>'intraday_trades')::int > 0`
  - one with neither
- Pure functions:
  - `replay_kwargs(config: dict) -> tuple[dict, dict]`. Kwargs: `start_date`, `end_date`, `max_hold_days`, `fill_model`. Env mapping:
    - `gap_fill` → `OPENCLAW_BT_GAP_FILL`
    - `financials_pit` → `OPENCLAW_FINANCIALS_PIT` (`'1'`/`'0'`)
    - `macro_pit` (absent ⇒ `'0'`) → `OPENCLAW_MACRO_PIT`
    - `target_mode` → `OPENCLAW_TARGET_MODE`
    - `double_touch` → `OPENCLAW_BT_DOUBLE_TOUCH`
    - `event_gate` (`'on'` ⇒ `'1'`, else `'0'`) → `OPENCLAW_BT_EVENT_GATE`
    - `intraday_exit` (`'off'` ⇒ `'0'`, else `'1'`) → `OPENCLAW_BT_INTRADAY_EXIT`
    - `asset_gate` → `OPENCLAW_BT_ASSET_GATE`
    - `rf.source` → `OPENCLAW_RF_SOURCE`
    - Keys absent from config are left to the ambient env, which is the same for both captures.
  - `capture_one(ub, strategy_id: str, kwargs: dict, env: dict) -> dict` returns `{'trade_rows': [...], 'config_json': str, 'run_row': str}`. Same stripping as Task 5 Step 1; `ub` is the imported module, injected for tests. It calls `ub.run_backtest(strategy_id, conn=<MagicMock conn>, commit=False, instrument_class=ub._resolve_instrument_class(strategy_id), **kwargs)` under `patch.object(ub.psycopg2.extras, 'execute_values', spy)` and `patch.dict(os.environ, env)`.
  - `compare(a: dict, b: dict) -> tuple[bool, list[str]]` prints one line per run: `[tracker_parity] run=<id> sid=<sid> trades=IDENTICAL|DIFF(n=<na>/<nb> first=<idx>) config_json=IDENTICAL|DIFF(keys=<k,…>) run_row=IDENTICAL|DIFF`. A run present on one side only is a DIFF. `ok` is true iff every run is IDENTICAL on all three fields. The rc is `0 if ok else 1`.
- `capture` refuses to run unless `PYTHONHASHSEED == '0'` (re-exec itself with it set): determinism `1acb7eb`. Docstring: a controller operation reading real masters and Postgres; run as a transient unit outside 18:45–20:45Z and never alongside a fleet unit.

- [ ] **Step 1: Write the failing tests:**
  - `test_replay_kwargs_maps_provenance_keys` (every mapping above, and `macro_pit` absent ⇒ `'0'`)
  - `test_compare_identical_ok`
  - `test_compare_reports_first_diff_index_and_config_keys`
  - `test_compare_missing_run_is_diff`
  - `test_capture_one_strips_run_id_and_code_sha`: a fake `ub` `SimpleNamespace(psycopg2=SimpleNamespace(extras=SimpleNamespace(execute_values=None)), run_backtest=fake, _resolve_instrument_class=lambda s: 'equity')`. Its `fake` calls `conn.cursor().execute('INSERT INTO strategy_backtest_runs …', params)` and `ub.psycopg2.extras.execute_values(cur, 'INSERT INTO strategy_backtest_trades …', rows)`.
  - `test_rows_json_seam_never_touches_db`: `psycopg2.connect` monkeypatched to raise. The row-loader path with `--rows-json` returns the rows.

  Load the script via `importlib.util.spec_from_file_location` (pattern `tests/scripts/test_pit_gap_flip_gate.py:12-17`).

- [ ] **Step 2: Run** `nice -n 19 python3 -m pytest tests/scripts/test_measure_tracker_parity.py -q -W error`. Expected: FAIL
- [ ] **Step 3: Implement** the script per Interfaces.
- [ ] **Step 4: Run** the same command. Expected: PASS
- [ ] **Step 5: Commit**

```bash
git add scripts/measure_tracker_parity.py tests/scripts/test_measure_tracker_parity.py
git commit -m "feat(scripts): measure_tracker_parity two-checkout replay capture/compare with --rows-json seam (six-repo D3c)"
```

---

### Task 7: D2a — `BaseStrategy.cached` + `IndicatorCacheTracker`

**Files:**
- Modify: `src/strategies/base.py`. Add ONE contiguous method block between `cadence_reset` (ends `:251`) and `position_scale` (`:253`). Make no other edit: no class attribute, no import line, no `__init__` change. Stream C edits the class-attribute block after `benchmark_sleeve` (`:179`), `compute_stops_and_targets` (`:300-303`), and new methods after `should_exit` (`:257-273`); this placement touches none of their hunks.
- Modify: `src/backtest/trackers.py` (add `IndicatorCacheTracker`)
- Modify: `src/backtest/unified_backtest.py` (register it in Task 5's collection when `OPENCLAW_BT_INDICATOR_CACHE == '1'`)
- Test: `tests/strategies/test_base_cached.py`, `tests/backtest/test_indicator_cache_hook.py`

**Interfaces:**
- Produces: `BaseStrategy.cached(self, name: str, fn: 'Callable[[pd.DataFrame], pd.DataFrame | pd.Series]', frame: pd.DataFrame) -> 'pd.DataFrame | pd.Series'`. Annotations are strings, so no typing import is needed.
  - `src = getattr(self, '_cache_source', None)`. When `src is None` (live engine, validate, every unflagged backtest): `stats['bypass'] += 1` and return `fn(frame)`.
  - Compatible iff:
    - `frame.index` equals `src.index[:len(frame)]`
    - every `frame` column is in `src.columns`
    - `frame.iloc[-1]` equals `src.loc[frame.index[-1], frame.columns]` NaN-aware (`Series.equals`)

    Otherwise `stats['incompatible'] += 1` and return `fn(frame)`.
  - Key: `(name, tuple(frame.columns))`. On a miss, `full = fn(src[list(frame.columns)])`. If `full` is not a DataFrame or Series, `stats['uncacheable'] += 1` and return `fn(frame)` without storing. Otherwise store `(fn, full)`, count `miss`, then return `full.loc[:frame.index[-1]]` (spec). A hit counts `hit`.
  - A key whose column tuple differs from the previous key for the same `name` counts `recompute`. Cross-sectional fns (`mean(axis=1)`, `rank(axis=1)`) over a per-bar universe recompute every bar; correct but no gain (docstring says so).
  - Storage lives in the instance `__dict__`: `self.__dict__.setdefault('_bt_cache', {})` and `self.__dict__.setdefault('_bt_cache_stats', {'hit': 0, 'miss': 0, 'recompute': 0, 'bypass': 0, 'incompatible': 0, 'uncacheable': 0})`. Names avoid Stream C's `ind`.
  - Docstring contract: `frame` is the `prices` argument as received, or a column subset of it. Every transform (`dropna`, `ffill`, `pct_change`) goes INSIDE `fn`. `fn` must be causal (rolling, ewm, shift, cumulative), never centred windows, full-sample normalisation or future-inclusive ranks. Mutating the returned object is safe under pandas 3 Copy-on-Write.
- Produces: `IndicatorCacheTracker` in `trackers.py`.
  - `initialize(ctx)` sets `ctx.instance._cache_source = ctx.close_wide`.
  - `finalize(info)` sets `info['indicator_cache'] = dict(stats)` (persist never reads it, so config_json is unchanged), prints `[unified_backtest] [bt_cache] on hits=<h> misses=<m> recomputes=<r> bypass=<b> incompatible=<i> uncacheable=<u> names=<sorted names>`, and clears `_cache_source` and `_bt_cache` to release memory.

- [ ] **Step 1: Write the failing tests** `tests/strategies/test_base_cached.py`. A concrete `BaseStrategy` stub; a synthetic panel of 300 business days × `ZZTEST1..ZZTEST5` with NaN gaps and a late listing; `fn = lambda p: p.rolling(20).mean()` wrapped in a call counter. Tests:
  - `test_no_source_is_plain_call`: equals `fn(frame)` and counts bypass.
  - `test_prefix_slices_equal_fresh_compute_and_fn_called_once`: 5 prefixes, each `assert_frame_equal(check_exact=True)` against `fn(prefix)`, with the counter at 1.
  - `test_column_subset_key_and_equality`.
  - `test_transformed_frame_same_shape_bypasses` (Review Focus 3): `frame = panel.loc[:d].ffill()` and `np.log(panel.loc[:d])` each produce `incompatible`, and the result equals `fn(frame)`.
  - `test_non_prefix_index_bypasses` (a frame starting mid-panel).
  - `test_mutating_returned_slice_does_not_poison_cache`.
  - `test_ndarray_result_is_uncacheable_and_correct`.
  - `test_universe_change_counts_recompute`.

  Then write `tests/backtest/test_indicator_cache_hook.py`, driving `_per_bar_simulate` directly with `load_aux_data` patched. Its stub strategy calls `self.cached('sma5', lambda p: p.rolling(5).mean(), prices)`. Tests:
  - `test_flag_off_no_source_and_no_line` (capsys: no `[bt_cache]`; stats all bypass)
  - `test_flag_on_sets_source_trades_identical_and_line_once` (trades equal to the flag-off run; exactly one `[bt_cache] on hits=`)
  - `test_source_cleared_after_run`

- [ ] **Step 2: Run** each file with `nice -n 19 python3 -m pytest <file> -q -W error`. Expected: FAIL (`AttributeError: cached`)
- [ ] **Step 3: Implement** per Interfaces.
- [ ] **Step 4: Run** both files plus `tests/backtest/test_tracker_parity_golden.py` (unchanged golden). Expected: PASS
- [ ] **Step 5: Commit**

```bash
git add src/strategies/base.py src/backtest/trackers.py src/backtest/unified_backtest.py tests/strategies/test_base_cached.py tests/backtest/test_indicator_cache_hook.py
git commit -m "feat(strategies): BaseStrategy.cached prefix-slice cache + IndicatorCacheTracker behind OPENCLAW_BT_INDICATOR_CACHE (six-repo D2a)"
```

---

### Task 8: D2b — parity assert in `validate_strategy` (`OPENCLAW_BT_CACHE_PARITY=1`)

**Files:**
- Modify: `src/strategies/validate_strategy.py`. Add a new step 6c after the zero-signal WARN (`:267-276`), plus a helper. The spec says `scripts/validate_strategy.py`; the file is at `src/strategies/validate_strategy.py`.
- Test: `tests/strategies/test_validate_cache_parity.py`

**Interfaces:**
- Consumes: `cached`, `_cache_source`, `_bt_cache` (Task 7).
- Produces: `_cache_parity_check(cls, *, seed: int = 0, n_dates: int = 3) -> tuple[list[str], list[str]]` (errors, warnings).
  - Panel: `_make_synthetic_prices(tickers=[f'ZZTEST{i}' for i in range(25)] + ['SPY', 'QQQ'], n_days=max(SYNTHETIC_DAYS, int(getattr(cls, 'min_lookback', 20)) + 60))`. SPY/QQQ are needed because `S_two_day_pullback_atr_uptrend` hard-codes them. The panel is synthetic and touches no DB.
  - Regime: `_make_synthetic_regime()` with `state` set to `cls.active_in_regimes[0]`.
  - Dates: `random.Random(seed).sample(list(panel.index[min_lookback + 5:]), k=min(n_dates, …))`.
  - Per date `d`:
    - `A = cls()` with `A._cache_source = panel` runs `A.generate_signals(panel.loc[:d], …)`.
    - For each `(name, cols) -> (fn, full)` in `A._bt_cache`: `full.loc[:d]` must equal `fn(panel.loc[:d, list(cols)])` exactly (`pd.testing.assert_*_equal(check_exact=True)`). On mismatch, error `cache_parity: <name> differs at <d:%Y-%m-%d>`.
    - A fresh `B = cls()` with no source must produce the same signal tuples `(ticker, direction, entry_price, stop_loss, target_1, signal_params)`. On mismatch, error `cache_parity: signals differ at <d>`.
  - A class that defines `cached` calls but populated zero keys across all dates gets warning `cache_parity_vacuous`. Detection: source text contains `self.cached(`.
  - Called from `validate()` only when `os.environ.get('OPENCLAW_BT_CACHE_PARITY') == '1'`. Errors extend `errors` (so `ok` is false); warnings extend `warnings_out`. One stderr line `[cache_parity] checked=<n_names> dates=<k> errors=<e>` per call. Unset ⇒ no call, byte-identical output.

- [ ] **Step 1: Write the failing tests.** Throwaway strategy modules in `tmp_path`, as in `tests/strategies/test_validate_zero_signal_warning.py`:
  - `test_flag_unset_no_parity_keys` (result identical to today's keys)
  - `test_causal_cached_strategy_passes` (`rolling(10).mean()`)
  - `test_noncausal_cached_strategy_fails`: `fn = lambda p: p - p.mean()` (full-sample mean) gives `ok is False` and an error starting `cache_parity:`.
  - `test_long_lookback_panel_is_long_enough`: `min_lookback=252`, and the check populates ≥1 key (not vacuous).
  - `test_vacuous_warning_when_cached_never_reached`: `generate_signals` returns before calling `cached`.
  - `test_stderr_line_emitted`.

- [ ] **Step 2: Run** `nice -n 19 python3 -m pytest tests/strategies/test_validate_cache_parity.py -q -W error`. Expected: FAIL
- [ ] **Step 3: Implement.** Use a local `random.Random`, never the global RNG.
- [ ] **Step 4: Run** the new file plus `tests/strategies/test_validate_zero_signal_warning.py`. Expected: PASS
- [ ] **Step 5: Commit**

```bash
git add src/strategies/validate_strategy.py tests/strategies/test_validate_cache_parity.py
git commit -m "feat(validate): OPENCLAW_BT_CACHE_PARITY cached-vs-fresh assert on a lookback-sized panel (six-repo D2b)"
```

---

### Task 9: D2c — `scripts/measure_indicator_cache.py`

**Files:**
- Create: `scripts/measure_indicator_cache.py`
- Test: `tests/scripts/test_measure_indicator_cache.py`

**Interfaces:**
- CLI: `--strategy SID` (resolved with `unified_backtest.find_strategy_file`, loaded with `load_strategy_class`) or `--strategy-file PATH`, plus `--compare-to SID` (optional), `--days 2746`, `--tickers 1000` and `--seed 0`.
- `make_panel(days: int, tickers: int, seed: int) -> tuple[pd.DataFrame, dict, pd.Series]` returns `(close_wide, bars_by_ticker, regimes)`:
  - GBM closes for `ZZTEST0000…` plus `SPY` and `QQQ` columns, on a business-day index ending 2026-10-16.
  - About 5 % of tickers list late (leading NaN), and interior NaN gaps (1 % of cells) appear, so byte-identity is a real test.
  - OHLC bars are derived from the closes.
  - The regime Series is constant at the strategy's first active regime (set by the caller).
- `run_once(cls, close_wide, bars, regimes, *, cache: bool) -> tuple[list, float, int]` returns `(signal_records, seconds, peak_rss_mb)`. It drives `unified_backtest._per_bar_simulate(instance, close_wide, bars, regimes, start, end, strategy_id=..., fill_model='same_close')`. The start is `close_wide.index[min_lookback + 5]` and the end is the last date. The settings:
  - env `OPENCLAW_BT_INDICATOR_CACHE` set to `'1'` or removed
  - `OPENCLAW_BACKTEST_COUPLED_RECS=0`; `OPENCLAW_BT_EVENT_GATE` removed
  - `strategies.aux_data_loader.load_aux_data` patched to `{'options': {}}`
  - `instance.generate_signals` wrapped to record `(date_iso, ticker, direction, entry_price, stop_loss, target_1, target_2, target_3, position_size_pct, confidence, json.dumps(signal_params, sort_keys=True, default=str))`
  - strategy stderr redirected to `os.devnull`
  - peak RSS from `resource.getrusage(RUSAGE_SELF).ru_maxrss // 1024`
- Output, one line:

  ```
  [bt_cache_measure] strategy=<sid> days=<d> tickers=<n> uncached_s=<x> cached_s=<y> speedup=<x/y>x signals_identical=<true|false> trades_identical=<…> n_signals=<k> sha=<sha256[:12]> peak_rss_mb=<m>[ orig=<sid2> orig_identical=<…>]
  ```

  `--compare-to` runs the original with the cache off and compares its records to the cached run. The rc is 0 iff every identity is true.
- Docstring: full size (1,000 × 2,746) is a controller run, `nice -n 19`, as a transient unit outside the compute window.

- [ ] **Step 1: Write the failing tests** (`--days 320 --tickers 12`; a stub strategy file in `tmp_path` using `self.cached('sma', lambda p: p.rolling(20).mean(), prices)` and `min_lookback=30`):
  - `test_make_panel_has_gaps_and_late_listings`
  - `test_run_once_identical_both_ways`
  - `test_main_prints_one_line_rc0`
  - `test_compare_to_detects_divergent_port`: a second stub that shifts its threshold gives `orig_identical=false` and rc 1.
  - `test_never_reads_real_aux` (`load_aux_data` spy is the patched one; `psycopg2.connect` patched to raise)
- [ ] **Step 2: Run** `nice -n 19 python3 -m pytest tests/scripts/test_measure_indicator_cache.py -q -W error`. Expected: FAIL
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same. Expected: PASS
- [ ] **Step 5: Commit**

```bash
git add scripts/measure_indicator_cache.py tests/scripts/test_measure_indicator_cache.py
git commit -m "feat(scripts): measure_indicator_cache synthetic-panel wall time + signal byte-identity (six-repo D2c)"
```

---

### Task 10: D2d — three `_cached` measurement twins

**Files:**
- Create: `src/strategies/implementations/S_ast_trend_following_effect_in_stocks_cached.py`, `S_tr_02_hurst_regime_flip_cached.py`, `S_two_day_pullback_atr_uptrend_cached.py`, and a `.requirements.json` for each (copy the parent's, `strategy_id` = new id).
- Modify: `docs/archive/changelog.md`
- Test: `tests/strategies/test_cached_twins_parity.py`

**Selection evidence** (live in `manifest.json`; no `read_parquet`/`read_csv`/`duckdb`/`load_wide`/`open(` in the file; full-history ops inside `generate_signals`, recomputed on every bar's growing prefix):
- `S_ast_trend_following_effect_in_stocks` (live, `tier_r1000`). Per bar it loops the whole universe doing `prices[t].dropna()` + `series.max()`, which is O(N·T) per bar, plus `diff().abs().rolling(10)` (`:64-80`).
- `S_tr_02_hurst_regime_flip` (live, `tier_r1000`). Per bar it does `prices[tickers].ffill().pct_change()` over the full prefix (`:85-91`).
- `S_two_day_pullback_atr_uptrend` (live, SPY/QQQ). Per bar it does `series.diff().abs().rolling(20)` and `series.rolling(200)` over full history (`:114-122`). These are fixed tickers, so the gain is small; it is the control case.
- Rejected:
  - `S_macro_risk_momentum_ip_beta`: D1 moves its macro input.
  - `S_intl_momentum_attention_regime`: universe-dependent `mean(axis=1)` and row drop.
  - `S_volume_shock_overnight_drift`, `S_downside_beta_premium`: self-loaded `_extra_panels` / bounded `iloc` tails.
  - The gold strategies and `S_aligned_economic_index_regime_timing`: bounded `tail`.

**Interfaces:**
- Each file imports its parent by MODULE ALIAS, `import strategies.implementations.<orig> as _orig`, and subclasses `_orig.<Class>`. A bare `from … import <Class>` would make the parent a module member that `validate_strategy` (`:184-191`, alphabetical, no module filter) picks first. It sets `id = '<orig>_cached'` and overrides only `generate_signals`.
- The body is copied verbatim from the parent except these substitutions; everything else, including print lines, order and rounding, is unchanged:
  - **trend following**: before the loop, take `last = prices.index[-1]` and three cached Series from the panel:
    - `n_valid = self.cached('nvalid', lambda p: p.notna().cumsum(), prices).loc[last]`
    - `cur = self.cached('ffill', lambda p: p.ffill(), prices).loc[last]`
    - `hmax = self.cached('cummax_ffill', lambda p: p.cummax().ffill(), prices).loc[last]`

    Inside the per-ticker loop, the gates use `n_valid[t] < ATR_PERIOD + 5`, `float(cur[t]) < MIN_PRICE` and `float(cur[t]) < float(hmax[t])`, in the same order. Only for tickers that pass do `series = prices[t].dropna()` and the ATR line run as in the parent.
  - **hurst**: `price_data` stays `prices[tickers].ffill()` only for its length check. `latest = self.cached('ffill', lambda p: p.ffill(), prices)[tickers].iloc[-1]`. `rets_all = self.cached('ffill_pct', lambda p: p.ffill().pct_change(), prices)[tickers]`. `vol = rets_all.iloc[-VOL_WINDOW:].dropna(how='all').std() * np.sqrt(252)` (all-NaN rows of ffilled returns form a prefix, so this equals the parent's `returns.iloc[-VOL_WINDOW:]`). Per ticker, `series = rets_all[ticker].dropna().values`.
  - **two-day pullback**: per ticker:
    - `atr_raw = self.cached('absdiff_r20', lambda p: p.apply(lambda c: c.dropna().diff().abs().rolling(atr_p).mean()), prices)[ticker].loc[series.index[-1]]`
    - `sma_200 = float(self.cached('sma200', lambda p: p.apply(lambda c: c.dropna().rolling(trend_p).mean()), prices)[ticker].loc[series.index[-1]])`

    The per-column `dropna` inside `fn` reproduces the parent's dropna-then-roll semantics across NaN gaps.
- No manifest, `_IMPL_MAP` or register-script edit (see the report: promotion risk). Measurement needs none of them: `find_strategy_file` resolves `<id>.py` by filename (`unified_backtest.py:228-231`), and `python3 -m backtest.unified_backtest --strategy-file <path>` / `scripts/measure_indicator_cache.py --strategy <id>_cached --compare-to <id>` both work unregistered.

- [ ] **Step 1: Write the failing tests** `tests/strategies/test_cached_twins_parity.py`. Use `measure_indicator_cache.make_panel(days=330, tickers=30, seed=s)` for seeds 0 and 1, regime = the parent's first active regime, and universe = all non-SPY/QQQ columns (for the pullback, its own tickers). Tests:
  - `test_twin_signals_identical_to_parent[<sid>]` (parametrized ×3): over every bar from `min_lookback + 5`, the parent's `generate_signals(prefix)` records equal the twin's with `_cache_source = panel`, and equal the twin's without a source.
  - `test_twin_validates_with_parity_flag[<sid>]`: `validate_strategy.validate(path)` under `OPENCLAW_BT_CACHE_PARITY=1` gives `ok is True` with no `cache_parity_vacuous`.
  - `test_twin_class_is_the_one_validate_picks[<sid>]` (the selected class `id` ends with `_cached`).
  - `test_twin_cache_hits_dominate`: after the loop, `stats['hit'] > stats['miss']`.
- [ ] **Step 2: Run** `nice -n 19 python3 -m pytest tests/strategies/test_cached_twins_parity.py -q -W error`. Expected: FAIL (files absent)
- [ ] **Step 3: Implement the three files and sidecars.** Add a changelog entry naming the twins and the controller measurement commands.
- [ ] **Step 4: Run** the same. Expected: PASS. Also run `python3 -m strategies.strategy_lint src/strategies/implementations/<id>_cached.py` for each and expect no violations.
- [ ] **Step 5: Commit**

```bash
git add src/strategies/implementations/S_ast_trend_following_effect_in_stocks_cached.py src/strategies/implementations/S_ast_trend_following_effect_in_stocks_cached.requirements.json src/strategies/implementations/S_tr_02_hurst_regime_flip_cached.py src/strategies/implementations/S_tr_02_hurst_regime_flip_cached.requirements.json src/strategies/implementations/S_two_day_pullback_atr_uptrend_cached.py src/strategies/implementations/S_two_day_pullback_atr_uptrend_cached.requirements.json docs/archive/changelog.md tests/strategies/test_cached_twins_parity.py
git commit -m "feat(strategies): three _cached measurement twins using BaseStrategy.cached (six-repo D2d)"
```

---

## Controller operations (not implementer tasks)

1. After D merges: `scripts/measure_tracker_parity.py capture --repo <pre-D3 checkout at the Task 4 parent commit> --out before.json`, then the same with `--repo <D3 checkout> --out after.json`, then `compare before.json after.json`. Use `PYTHONHASHSEED=0` and a transient unit, outside 18:45–20:45Z, with no fleet unit active.
2. `scripts/measure_indicator_cache.py --strategy <id>_cached --compare-to <id>` for each twin (full size).
3. R2 epoch: the `pit-gap.conf` fleet drop-in carries `OPENCLAW_FINANCIALS_PIT=1`, `OPENCLAW_BT_GAP_FILL=open` and `OPENCLAW_MACRO_PIT=1`. The flip script then flips all three.
4. §6.5 registration of the `_cached` ids: an operator decision (see the report).

## Self-review notes

- Spec coverage:
  - D1 helper is Task 1; `available_at` + lags + flagged slice + parity are Task 2; config_json + G1 + flip are Task 3.
  - D3 is Tasks 4–6.
  - D2 `cached` + hook is Task 7, the parity assert Task 8, the measurement Task 9, the three ids Task 10.
- Names used across tasks: `BacktestContext.counters`, `FinishedBacktest`, `IndicatorCacheTracker`, `_cache_source`, `_bt_cache`, `_bt_cache_stats`, `available_at_frame`, `MACRO_RELEASE_LAG_DAYS`, `macro_release_lag`, `_cache_parity_check`, `make_panel`, `run_once`, `replay_kwargs`, `capture_one`, `compare`.
