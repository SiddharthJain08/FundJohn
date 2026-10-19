# Stream A — Storage Layout and Reads: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the parquet masters lay out and read the way their consumers assume: date-tight `prices.parquet` row groups, windowed reads in place of full-master reads, an optional trailing-window live price load, per-month `options_eod` files, and a dual-written hive-partitioned prices tree. Also two cleanup items: dead backtest indexes and dormant code.

**Architecture:** Every layout decision goes through `src/data/parquet_store.py`. That module gains a footer-only layout evaluator, options and prices *source resolvers* (single file vs directory, chosen by env flag), and dataset-read helpers. Readers call these helpers instead of hard-coding `data/master/<x>.parquet`. One-off host operations are standalone `scripts/*.py` with `--dry-run|--apply`. The controller runs them (R1), never an implementer. Tier 1 (Tasks 1–5) has no flags and is byte-identical. Tier 2 (Tasks 6–11) sits behind `OPENCLAW_PRICES_TRAILING_WINDOW`, `OPENCLAW_OPTIONS_MONTHLY` and `OPENCLAW_PRICES_HIVE`, all default off.

**Tech Stack:** Python 3.13, pandas 3.0.2, pyarrow 23.0.1 (`pyarrow.dataset`, `pq.filters_to_expression`), DuckDB 1.1.3, pytest (`pytest.ini`: `pythonpath = src`). SQL migration for Postgres 16.

**Spec:** `docs/specs/2026-10-19-six-repo-adoptions-spec.md` §0, §1 (A1–A7), §6, §7. Notes: `/root/.learnings/reviews/2026-10-19-six-repo-fit-review/NOTES-timescaledb.md` (F1, F3, F4, F7, D1–D3) and `NOTES-questdb.md` (F2, F4, D2, D3, D6).

## Global Constraints

Spec §0 (verbatim):
- Master parquets and canonical Postgres tables are append-only (repo CLAUDE.md). New tables/columns only; never DELETE, never rewrite history. A one-off re-layout that preserves every row byte-for-byte (A1) is a re-write of the container, not of the data, and is allowed under R1.
- Backtest side is AUTHORITATIVE (2026-08-07 ruling). Anything that changes a backtest input (D1) lands flag-gated, default off, and flips only with a fleet epoch.
- Every new behaviour that can change sizing, signals or gate outcomes ships behind an `OPENCLAW_*` env flag, default = pre-change behaviour, byte-identical when unset. Pure bug fixes with no gated consumer (B2 OU estimator, B3 MC seed/bridge, A2 windowed reads, A4 doc/dormant) need no flag; the plan says which.
- Never read the whole `prices.parquet` or `options_eod.parquet` in a test or an implementer session. Tests use synthetic frames; parquet tests write a tiny file to `tmp_path`.
- Tests reach the REAL Postgres when `.env` is importable — stub every DB gate in fixtures (`monkeypatch` the connect function); fixture tickers must not be real symbols (`ZZTEST1`, not `AAA`).
- The box is 2-core / 8 GB / no swap and shared with the live pipeline. Implementers run single-file pytest only, `nice -n 19`, never the full suite, never while a fleet unit is active.
- `strategy_lint.ALLOWED_ROOTS` stays as is; new shared modules live under `src/strategies/` (already an allowed root) or `src/lib/`.
- Observability: every new gate/shadow emits one log line per cycle with a fixed prefix; a missing line means the process died, never "nothing to report". Never silent.
- Commit per task; edits, `git commit` and `git push` as separate tool calls (ERR-20261018-001).

Stream A specifics:
- R1: `scripts/resort_prices_master.py --apply`, the A5 index drops, `scripts/split_options_master.py --apply` and `scripts/build_prices_hive.py --apply` are run by the controller as transient systemd units outside 18:45Z–20:45Z on trading days, and never while a fleet unit is active. Implementers run these scripts only against `tmp_path` fixtures.
- Flags (unset = pre-change behaviour, byte-identical):
  - `OPENCLAW_PRICES_TRAILING_WINDOW=1` (A3)
  - `OPENCLAW_OPTIONS_MONTHLY=1` (A6)
  - `OPENCLAW_PRICES_HIVE=1` (A7)
- No flag needed: A1 writer and check (container-only), A2 (pure fix, byte-identical via the two-pass read in Task 3), A4 (doc/dormant), A5 (DDL).
- Log prefixes:
  - `[prices_window]` (A3, once per `load_prices`)
  - `[options_store]` (A6, once per options write)
  - `[prices_hive]` (A7, once per `write_prices` while the flag is on)
  - `[prices_source]` (A7, once per reader call while the flag is on)
- One-off scripts print their own run line with a fixed prefix (`[resort_prices]`, `[split_options]`, `[build_prices_hive]`, `[prices_hive_parity]`). Each exits non-zero on a verification failure.
- Spill directory for one-off scripts defaults to `/root/openclaw/data/.duckdb_spill`. **`/tmp` is tmpfs on this box (RAM), so a DuckDB spill to `/tmp` consumes the memory it is meant to save.**
- Test command shape: `nice -n 19 python3 -m pytest <one file> -W error -q` from the worktree root.
- Fixture tickers: `ZZTEST1`, `ZZTEST2`, …

## Review Focus

1. **A multi-month options batch** (a backfill or month-boundary flush) must land each row in its own `<YYYY-MM>.parquet`, deduped only against that month. The test is in Task 8 (`test_append_options_splits_batch_by_month`).
2. **After the A6 flip the single `options_eod.parquet` is frozen by design.** `doctor.options_archive_freshness` (runs in johnbot `ExecStartPre`) and `master_freshness` must judge the directory, not the frozen file, or they will FAIL and block boot. The test is in Task 9 (`test_freshness_consumers_follow_monthly_dir`).
3. **DuckDB 1.1.3 auto-detects hive keys** on any `read_parquet('…/year=2026/part-0.parquet')`, so `append_dedup`'s `SELECT e.*` would write a `year` column into the part file. That column then conflicts with the partition field. The test is in Task 10 (`test_hive_part_file_has_no_year_column`).
4. **The trailing window must not be re-expanded by the date-axis reindex.** `_parquet_date_axis` reads every date and `wide.reindex(_date_axis)` would restore ten years of all-NaN rows. The test is in Task 6 (`test_trailing_window_axis_is_windowed`).
5. **Migration 164 runs on the LIVE DB at every johnbot start** (`bot.js:1663` → `postgres.js:42 migrate()`).
   - It must not block behind a long seq scan.
   - Migration 093 must stop re-creating the two indexes. Otherwise every restart rebuilds 547 MB of index and then drops it.
   - The test is in Task 5 (`test_093_no_longer_creates_dropped_indexes`, `test_164_bounded_lock_wait`).

---

## Tier 1

### Task 1: A1 — writer discipline + footer-only layout evaluator + `prices_rowgroup_span` check

No flag: container-only, data byte-identical.

**Files:**
- Modify: `src/data/parquet_store.py:62-69` (`_atomic_write`), `:122-129` (the stale "pruning is unchanged" comment in `append_dedup`)
- Create: `src/data/parquet_layout.py`
- Create: `src/system_checks/checks/parquet_layout.py`
- Modify: `src/system_checks/checks/__init__.py` (add `from . import parquet_layout  # noqa: F401`)
- Test: `tests/pipeline/test_parquet_layout.py`, `tests/system_checks/test_parquet_layout_check.py`

**Interfaces:**
- Produces:
  - `parquet_store._atomic_write(path: Path, df: pd.DataFrame, *, write_page_index: bool = True, row_group_size: int | None = 131072) -> None`
  - `parquet_layout.rowgroup_span_report(path: Path | str, date_col: str = 'date', max_span_days: int = 60) -> dict` with keys:
    - `n_row_groups: int`, `n_tight: int`, `frac_tight: float`
    - `median_span_days: float`, `max_span_days_seen: int`
    - `page_index: bool` (True iff every column chunk of row group 0 `has_column_index`). Informational only: it is printed in the detail and never affects the verdict (controller ruling 1: no page-index WARN, because the daily DuckDB `COPY` append can never write one).
    - `monotone: bool` (every `rg[i].max(date) <= rg[i+1].min(date)`)
    - `missing_stats: int`
  - `parquet_layout.layout_verdict(report: dict, min_frac: float = 0.95) -> tuple[str, str]`
    - returns `('FAIL'|'PASS', detail)`
    - FAIL if `frac_tight < min_frac` or `missing_stats > 0`, else PASS. There is no WARN branch.
    - detail ≤ 200 chars
  - Check `prices_rowgroup_span` (tags `['storage']`, requires `['fs']`) reads `$OPENCLAW_MASTER_DIR/prices.parquet` (default `/root/openclaw/data/master`, same env as `master_freshness.py:31`). It maps the verdict onto `Status`. A missing file is `SKIP`.

- [ ] **Step 1: Write the failing tests**

`tests/pipeline/test_parquet_layout.py`:
- `test_atomic_write_page_index_and_rowgroup_size(tmp_path)`:
  - `_atomic_write(p, df_1000_rows, row_group_size=256)` produces `num_row_groups == 4`.
  - Row group 0 column 0 `has_column_index` and `has_offset_index` are True.
- `test_atomic_write_default_rowgroup_size(monkeypatch, tmp_path)`:
  - Spy on `pq.write_table`; assert it was called with `row_group_size=131072, write_page_index=True, compression='snappy'`.
- `test_append_dedup_keeps_date_monotone_after_sort(tmp_path)`:
  - Seed a file sorted by (date, ticker) over 30 dates × `ZZTEST1..3`.
  - `append_dedup(..., mode='replace')` one new date, then re-append the SAME date with changed closes.
  - The `pd.read_parquet` row order has a non-decreasing `date`.
  - The replaced day appears once, with the new closes.
- `test_span_report_ticker_contiguous_vs_date_sorted(tmp_path)`:
  - Fixture 1: 3 tickers × 400 dates written ticker-contiguous with `row_group_size=100` (each row group spans more than 60 days) gives `frac_tight < 0.95`, `monotone False`, and verdict `FAIL`.
  - Fixture 2: the same rows sorted (date, ticker) gives `frac_tight == 1.0`, `monotone True`, and verdict `PASS`.
  - Fixture 3: the same sorted rows written via DuckDB `COPY` (no page index) still give verdict `PASS`, `report['page_index'] is False`, and the detail contains `page_index=False`.
- `test_span_report_reads_footer_only(monkeypatch, tmp_path)`:
  - Monkeypatch `pq.read_table` and `pd.read_parquet` to raise; the report still succeeds.

`tests/system_checks/test_parquet_layout_check.py`:
- `test_check_registered_and_maps_status(monkeypatch, tmp_path)`:
  - Set `OPENCLAW_MASTER_DIR` to tmp; write a FAIL-shaped fixture; `run_one('prices_rowgroup_span').status is Status.FAIL`.
  - Missing file gives `Status.SKIP`.
  - Because the module reads env at import like `master_freshness`, monkeypatch the module attribute `_MASTER_DIR` instead of reloading.

- [ ] **Step 2: Run tests to verify they fail**

Run: `nice -n 19 python3 -m pytest tests/pipeline/test_parquet_layout.py -W error -q`
Expected: FAIL (`ModuleNotFoundError: src.data.parquet_layout`, `TypeError` on `row_group_size`).

- [ ] **Step 3: Implement**

- `_atomic_write`: pass `write_page_index=write_page_index, row_group_size=row_group_size` to `pq.write_table` and keep `compression='snappy'`.
- Replace the `:122-129` comment with the truth. The file is insertion-ordered. Date-pruning holds only once the master has been re-sorted (A1 script) because daily appends are date-monotone. Ticker-ordering is still avoided.
- `rowgroup_span_report`:
  - Use `pq.ParquetFile(path).metadata` only.
  - Coerce stats with a local `_coerce_date(v) -> date | None` (bytes/str/datetime/date, same rules as `master_freshness._coerce`). `src/data` must not import from `src/system_checks`.
  - A row group with `statistics is None or not has_min_max` counts in `missing_stats`.
- Check function: `return Status[verdict], detail`.

- [ ] **Step 4: Run tests to verify they pass**

Run:
- `nice -n 19 python3 -m pytest tests/pipeline/test_parquet_layout.py -W error -q`
- `nice -n 19 python3 -m pytest tests/system_checks/test_parquet_layout_check.py -W error -q`
- `nice -n 19 python3 -m pytest tests/pipeline/test_parquet_store.py -W error -q` (regression)

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/data/parquet_store.py src/data/parquet_layout.py src/system_checks/checks/parquet_layout.py src/system_checks/checks/__init__.py tests/pipeline/test_parquet_layout.py tests/system_checks/test_parquet_layout_check.py
git commit -m "feat(storage): A1 writer page index + row-group size, footer-only prices_rowgroup_span check"
```

---

### Task 2: A1 — `scripts/resort_prices_master.py` (controller-run one-off)

**Files:**
- Create: `scripts/resort_prices_master.py`
- Test: `tests/scripts/test_resort_prices_master.py`

**Interfaces:**
- Consumes:
  - `parquet_store._file_lock(path, timeout)` (`parquet_store.py:36`)
  - `parquet_layout.rowgroup_span_report`, `parquet_layout.layout_verdict` (Task 1)
- Produces:
  - `resort(path: Path, *, apply: bool, spill_dir: Path, memory_limit: str = '1GB', row_group_size: int = 122880, threads: int = 1, lock_timeout: float = 600.0, today: date | None = None) -> dict`
  - Return keys: `rows_old, rows_new, colhash_equal: bool, rowhash_equal: bool, schema_equal: bool, report_new: dict, verdict_new: str, backup: str | None, applied: bool`
  - CLI: `--dry-run` (default) | `--apply`, `--path` (default `data/master/prices.parquet`), `--spill-dir` (default `/root/openclaw/data/.duckdb_spill`), `--memory-limit`, `--row-group-size`, `--threads`
  - Exit codes: 0 when verification passes; 2 on any mismatch (tmp removed, master untouched)

- [ ] **Step 1: Write the failing tests** (import via `importlib.util.spec_from_file_location`, as `tests/scripts/test_build_options_surface.py:11-14` does)
- `test_resort_dry_run_leaves_master_untouched(tmp_path)`:
  - Ticker-contiguous 3×400 fixture.
  - After `resort(apply=False)`: master bytes are unchanged (sha256), no `*.resort.tmp` is left, `rows_old == rows_new`, all three equality flags are True, and `verdict_new` is `'PASS'`.
- `test_resort_apply_sorts_and_keeps_backup(tmp_path)`:
  - After `apply=True, today=date(2026,10,20)`, `prices.parquet.pre-resort-20261020` exists, has the old sha256, and is a hardlink (`st_ino` equals the old inode).
  - The new file's `rowgroup_span_report(...)['monotone']` is True.
  - The multiset of `(ticker,date,close)` rows is equal to the original.
- `test_resort_output_has_page_index(tmp_path)` (controller ruling 1):
  - Small fixture, `row_group_size=256`.
  - For every row group `i` and column `j` of the output, `pq.ParquetFile(out).metadata.row_group(i).column(j).offset_index_offset is not None`.
  - `num_row_groups == ceil(rows / 256)`.
- `test_resort_refuses_on_hash_mismatch(monkeypatch, tmp_path)`:
  - Monkeypatch the script's `_hashes(con, src)` to return a different value for the tmp file.
  - Expect `SystemExit(2)` from `main([...,'--apply'])`, master sha256 unchanged, and no backup created.
- `test_resort_takes_master_lock(tmp_path)`:
  - Hold `parquet_store._file_lock(master)` in the test.
  - `resort(..., lock_timeout=0.2)` raises `TimeoutError`.

- [ ] **Step 2: Run to verify failure**

Run: `nice -n 19 python3 -m pytest tests/scripts/test_resort_prices_master.py -W error -q`
Expected: FAIL (file not found).

- [ ] **Step 3: Implement**

Sequence inside `_file_lock(path, timeout=lock_timeout)`:
1. Open a DuckDB connection with `SET memory_limit`, `SET temp_directory=<spill_dir>` (mkdir), `SET threads`, `SET preserve_insertion_order=true`.
2. Write the sorted file through pyarrow, not `COPY`, so the output carries a page index (controller ruling 1):
   - `reader = con.execute("SELECT * FROM read_parquet('<p>', hive_partitioning=false) ORDER BY date, ticker").fetch_record_batch(rows_per_batch=row_group_size)`
   - `pq.ParquetWriter('<p>.resort.tmp', reader.schema, compression='snappy', write_page_index=True)`
   - For each batch, `writer.write_batch(batch, row_group_size=row_group_size)`; close the writer.
   - The DuckDB sort spills to `spill_dir`, and only one batch is held in Python at a time.
   - `append_dedup` stays on DuckDB `COPY`. The page index therefore lasts only until the next daily append; the layout check does not depend on it.
3. Verify old vs tmp:
   - `DESCRIBE` lists equal (schema)
   - `count(*)` equal
   - per-column `sum(hash(columns(*))::HUGEINT)` equal
   - whole-row `sum(hash(t)::HUGEINT) FROM read_parquet(..) t` equal

   All of these forms were probed on DuckDB 1.1.3: order-independent and struct-hash OK.
4. Compute `rowgroup_span_report` on the tmp file and print it with a `layout_verdict`. The verdict is informational, not a gate, because a sorted file is strictly better than today's. If it reads FAIL, the controller re-runs with a smaller `--row-group-size`.
5. On `--apply`: `os.link(path, backup)` then `os.replace(tmp, path)`. The master path is never absent. On a dry run: unlink the tmp.

Print one line:
```
[resort_prices] rows=… schema_eq=… colhash_eq=… rowhash_eq=… rg=… frac_tight=… monotone=… page_index=… verdict=… applied=… backup=…
```

The script never deletes the backup. It prints `remove <backup> after 2026-10-27 if prices_rowgroup_span is PASS and the 15:00 ET compute is clean`.

- [ ] **Step 4: Run to verify pass**

Run: `nice -n 19 python3 -m pytest tests/scripts/test_resort_prices_master.py -W error -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/resort_prices_master.py tests/scripts/test_resort_prices_master.py
git commit -m "feat(storage): A1 resort_prices_master one-off (locked, hash-verified, hardlink backup)"
```

---

### Task 3: A2 — windowed reads for the daily ADV and market-cap steps

No flag (spec §0 names A2 a pure fix). The output must be byte-identical to the legacy full read (controller ruling 3), using a two-pass read:

- **Pass 1:** the 60-day windowed stats.
- **Pass 2:** the legacy `tail(20)` math over the full history of the fallback tickers only.

The pass-1 result equals legacy `tail(20)` only for a ticker with at least `last_n` = 20 rows in the window. So the fallback set is every requested symbol with fewer than 20 window rows. That includes tickers with zero window rows (stale), which is the ruling's case, and tickers that are sparse or halted inside the window. The ruling names only the stale case; including the sparse case is my refinement, needed for byte-identity.

Pass 2 is one filtered read: `pq.read_table(path, columns=['ticker','date','close','volume'], filters=[('ticker','in', fallback)])`.

`market_cap_lookup` already cuts at a 10-day floor (`:41-43`), so it needs only pass 1 with the same floor.

**Files:**
- Modify: `src/data/parquet_store.py` (new helper after `read_filtered`, `:192-214`)
- Modify: `src/pipeline/run_ticker_metadata_step.py:33-42` (`adv_usd_from_parquet`), `:50` (call site)
- Modify: `src/pipeline/market_cap_lookup.py:40-44`
- Test: `tests/pipeline/test_windowed_ticker_stats.py`; regression `tests/pipeline/test_market_cap_lookup.py`

**Interfaces:**
- Produces:
  - `parquet_store.ticker_window_stats(path: Path | str, *, cutoff: str, upto: str | None = None, last_n: int = 20) -> pd.DataFrame`
    - Columns `['ticker', 'last_close', 'last_date', 'adv_usd', 'n_rows']`, one row per ticker with any row in `[cutoff, upto]`. `n_rows` is `count(*)` in the window, NaN rows included.
    - Empty frame with those columns if the file is missing.
  - `run_ticker_metadata_step.adv_usd_from_parquet(symbols, as_of: date | None = None) -> dict[str, dict]` (default `date.today()`)
  - `ADV_WINDOW_CALENDAR_DAYS = 60` (module constant in `run_ticker_metadata_step.py`)

`read_filtered` has no bind parameters and cannot aggregate, so `ticker_window_stats` issues the same predicate-pushdown `read_parquet` with the aggregation inline. `cutoff` and `upto` must match `^\d{4}-\d{2}-\d{2}$` (raise `ValueError` otherwise) and are interpolated as literals. SQL shape:

```sql
WITH w AS (SELECT ticker, date, close, volume FROM read_parquet('<p>', hive_partitioning=false)
           WHERE date >= '<cutoff>' [AND date <= '<upto>']),
     r AS (SELECT *, row_number() OVER (PARTITION BY ticker ORDER BY date DESC) AS rn FROM w)
SELECT ticker,
       arg_max(close, date) FILTER (WHERE close IS NOT NULL AND NOT isnan(close)) AS last_close,
       max(date) AS last_date,
       count(*) AS n_rows,
       avg(close*volume) FILTER (WHERE rn <= <last_n> AND close*volume IS NOT NULL
                                   AND NOT isnan(close*volume)) AS adv_usd
FROM r GROUP BY ticker
```

The `FILTER`s reproduce pandas' NaN-skipping in `groupby.last()` and `mean()`. `rn` counts NaN rows, as `tail(20)` does.

- [ ] **Step 1: Write the failing tests** (`tests/pipeline/test_windowed_ticker_stats.py`)
- Import `run_ticker_metadata_step` only after `monkeypatch.setenv('POSTGRES_URI', 'postgresql://stub')`. Its `:10` reads the env at import. Use `importlib.import_module` inside the test, and `monkeypatch.setattr` `PRICES_PARQUET` to the tmp file.
- `test_adv_matches_legacy_tail20_on_fresh_tickers`:
  - `ZZTEST1/2` with 50 consecutive sessions ending at as_of.
  - New `adv_usd_from_parquet(..., as_of)` equals a verbatim copy of the legacy `:37-42` body (kept in the test as `_legacy_adv`) to `pytest.approx(rel=1e-12)`.
- `test_adv_true_nan_skipped_like_pandas`:
  - Write the fixture with `pa.Table.from_pandas(df, from_pandas=False)` so one `close` is a real IEEE NaN, not null.
  - The result equals `_legacy_adv`.
- `test_adv_stale_ticker_matches_legacy` (controller ruling 3): `ZZTEST3`'s last bar is 90 days before as_of. The new `adv_usd_20d` equals `_legacy_adv`, which is non-zero. `build_market_cap_lookup` gives the same result (`None`, outside the 10-day floor) as the legacy pandas path.
- `test_adv_sparse_in_window_ticker_matches_legacy`: `ZZTEST4` has 5 bars in the window and 100 older bars. The new value equals `_legacy_adv`.
- `test_adv_log_line(caplog)`: exactly one `[adv_window] adv_window_hits=2 adv_stale_fallback=2` line for the fixture above (`ZZTEST1/2` are hits; `ZZTEST3/4` fall back).
- `test_market_cap_identical_with_nan_latest_close`:
  - The latest close is NaN (null) for `ZZTEST1`.
  - `build_market_cap_lookup` returns `shares × previous close`, equal to the legacy pandas path copied into the test.
- `test_ticker_window_stats_rejects_non_iso_cutoff`: `ValueError` on `cutoff="2026-01-01' OR 1=1"`.
- `test_no_full_read(monkeypatch)`: monkeypatch `pandas.read_parquet` to raise when called on the prices fixture path, and wrap `pq.read_table` to assert that every call carries a `ticker in` filter. Both functions still succeed.

- [ ] **Step 2: Run to verify failure**

Run: `nice -n 19 python3 -m pytest tests/pipeline/test_windowed_ticker_stats.py -W error -q`
Expected: FAIL (`AttributeError: ticker_window_stats`).

- [ ] **Step 3: Implement**

- `adv_usd_from_parquet`:
  - `cutoff = (as_of - timedelta(days=ADV_WINDOW_CALENDAR_DAYS)).isoformat()`, `upto = as_of.isoformat()`.
  - Pass 1: `ticker_window_stats(PRICES_PARQUET, cutoff=…, upto=…)`. Symbols with `n_rows >= 20` take `adv_usd`.
  - Pass 2: if the fallback list is non-empty, run the filtered `pq.read_table` above, then apply the verbatim legacy `:38-41` math (isin → dollar_volume → sort by date → `groupby.tail(20)` → mean) to that subset.
  - Missing symbols default to `0.0`, as legacy `last_20.get(s, 0.0)` does.
  - Log `[adv_window] adv_window_hits=<N> adv_stale_fallback=<M>` once per call.
  - Keep the missing-file branch at `:34-35`.
  - Pass `as_of=today` at `:50`.
- `market_cap_lookup`: replace `:40-44` with `ticker_window_stats(prices_path, cutoff=floor_iso, upto=iso)` and take `last_close` per ticker. The shares read at `:36-38` is unchanged (small file).

- [ ] **Step 4: Run to verify pass**

Run:
- `nice -n 19 python3 -m pytest tests/pipeline/test_windowed_ticker_stats.py -W error -q`
- `nice -n 19 python3 -m pytest tests/pipeline/test_market_cap_lookup.py -W error -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/data/parquet_store.py src/pipeline/run_ticker_metadata_step.py src/pipeline/market_cap_lookup.py tests/pipeline/test_windowed_ticker_stats.py
git commit -m "fix(storage): A2 windowed ADV/latest-close reads (arg_max + 20-row avg) instead of full prices master"
```

---

### Task 4: A4 — docstring truth + delete dormant Polygon 30m ingester

No flag (doc/dormant).

**Files:**
- Modify: `src/ingestion/close_proxy_snapshot.py:176`. The spec cites `:178`; the actual line is `:176`, which reads `default env OPENCLAW_CLOSE_PROXY_MIN_COVERAGE (0.90)`. Change it to `(0.60)` to match code `:216`.
- Delete: `src/ingestion/ingest_prices_30m.py`. No unit, import or test references it. Verified: grep over `src scripts tests docs/systemd /etc/systemd /root/.config/systemd`.
- Modify citations of the deleted file:
  - `src/ingestion/intraday_features.py:7` and `:384`: re-word them as "the retired Polygon `ingest_prices_30m.py` (deleted 2026-10-19; see docs/archive/changelog.md)" and keep the 4-step discipline text.
  - `src/ingestion/quote_sources/__init__.py:29`: drop the line.
  - Leave `src/ingestion/ingest_prices_30m_alpaca.py:2` as is (a historical mention).
- Modify: `docs/archive/changelog.md` (new top bullet under `## Recent Changes`: what was deleted, why — Polygon key purged in SP-1, no unit references it, Alpaca path `ingest_prices_30m_alpaca.py` is live)
- Test: `tests/ingestion/test_a4_docs_dormant.py`

- [ ] **Step 1: Write the failing tests**
- `test_close_proxy_doc_default_matches_code`:
  - Import `ingestion.close_proxy_snapshot`.
  - Regex `OPENCLAW_CLOSE_PROXY_MIN_COVERAGE", "([0-9.]+)"` over `inspect.getsource(module)` gives the code default.
  - Assert `f"({default})"` is in `fetch_close_proxy.__doc__`.
- `test_polygon_30m_ingester_deleted`:
  - `not (ROOT/'src/ingestion/ingest_prices_30m.py').exists()`.
  - No `*.py`/`*.js`/`*.service`/`*.timer` under `src/ scripts/ docs/systemd/` matches `import ingest_prices_30m\b|ingest_prices_30m\.py` outside comments/docstrings. Simplification: assert no line contains `ingest_prices_30m.py` except lines that also contain `retired` or `deleted`.

- [ ] **Step 2: Run to verify failure**

Run: `nice -n 19 python3 -m pytest tests/ingestion/test_a4_docs_dormant.py -W error -q`
Expected: FAIL on both.

- [ ] **Step 3: Implement** the edits listed under Files (`git rm` the ingester).

- [ ] **Step 4: Run to verify pass**

Same command. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A src/ingestion docs/archive/changelog.md tests/ingestion/test_a4_docs_dormant.py
git commit -m "chore(ingestion): A4 close-proxy docstring 0.60, delete dormant Polygon ingest_prices_30m.py"
```

---

### Task 5: A5 — migration 164 (idempotent drop of the two zero-scan indexes) + stop 093 re-creating them

**Controller note:** the controller runs `DROP INDEX CONCURRENTLY` by hand, off-hours, BEFORE the first johnbot restart after this merges (the R1 runbook below).

**Why 093 changes:** `postgres.js:42-55 migrate()` re-runs EVERY migration file on each johnbot start (`bot.js:1663`). `093_unified_backtest_tables.sql:84-88` has `CREATE INDEX IF NOT EXISTS` for both indexes. After the drop, every restart would rebuild them on the 31 M-row table (non-concurrently, blocking writes) and 164 would drop them again.

**Why the file uses no `CONCURRENTLY`:** `migrate()` sends each file as ONE `query()`, so a multi-statement string runs in an implicit transaction, where `CONCURRENTLY` is illegal. The controller runs the R1 `DROP INDEX CONCURRENTLY` by hand off-hours. The file exists so a fresh clone matches. On a live DB where the indexes still exist, it must give up fast rather than queue behind a seq scan.

**Files:**
- Create: `src/database/migrations/164_drop_dead_backtest_trades_indexes.sql`
- Modify: `src/database/migrations/093_unified_backtest_tables.sql:84-88`. Replace both `CREATE INDEX` statements with a comment: `-- idx_backtest_trades_strategy / _regime removed 2026-10-19 (idx_scan = 0 lifetime); see 164.`
- Test: `tests/database/test_migration_164.py` (static file parse only — never connects)

Exact file body for 164 (fixed copy):
```sql
-- 164: drop two strategy_backtest_trades indexes with idx_scan = 0 (lifetime, measured 2026-10-19:
-- idx_backtest_trades_strategy 321 MB, idx_backtest_trades_regime 226 MB). Spec 2026-10-19 §1 A5.
-- Live DB: the controller runs DROP INDEX CONCURRENTLY off-hours (R1) BEFORE the next johnbot restart;
-- this file is the idempotent record so a fresh clone matches. migrate() runs every file on each
-- johnbot start inside one implicit transaction, hence no CONCURRENTLY and a bounded lock wait.
BEGIN;
SET LOCAL lock_timeout = '5s';
DROP INDEX IF EXISTS idx_backtest_trades_strategy;
DROP INDEX IF EXISTS idx_backtest_trades_regime;
COMMIT;
```

- [ ] **Step 1: Write the failing tests**
- `test_164_exists_and_idempotent`: the file exists; it contains `DROP INDEX IF EXISTS idx_backtest_trades_strategy;` and `DROP INDEX IF EXISTS idx_backtest_trades_regime;`; `CONCURRENTLY` does not appear outside `--` comment lines.
- `test_164_bounded_lock_wait`: non-comment SQL contains `SET LOCAL lock_timeout`, wrapped by `BEGIN;` … `COMMIT;`.
- `test_093_no_longer_creates_dropped_indexes`: across ALL `src/database/migrations/*.sql`, no non-comment line matches `CREATE INDEX.*idx_backtest_trades_(strategy|regime)`.
- `test_164_is_next_number`: `164_` is the only file with that prefix, and no `16[5-9]_` file sorts before it in `sorted()`.

- [ ] **Step 2: Run to verify failure**

Run: `nice -n 19 python3 -m pytest tests/database/test_migration_164.py -W error -q`
Expected: FAIL.

- [ ] **Step 3: Implement** (create 164 with the body above, edit 093).

- [ ] **Step 4: Run to verify pass**

Same command. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/database/migrations/164_drop_dead_backtest_trades_indexes.sql src/database/migrations/093_unified_backtest_tables.sql tests/database/test_migration_164.py
git commit -m "chore(db): A5 migration 164 drops two zero-scan backtest_trades indexes; 093 stops re-creating them"
```

**Controller runbook (not an implementer step):**
1. Off-hours, before any johnbot restart after merge: `DROP INDEX CONCURRENTLY IF EXISTS idx_backtest_trades_strategy;` then the same for `_regime`, each as its own `psql -c`.
2. Record `pg_relation_size` before and after.

---

## Tier 2 (second Stream A wave — §6 step 4)

### Task 6: A3 — `max_lookback_days` + trailing-window `load_prices` behind `OPENCLAW_PRICES_TRAILING_WINDOW`

**Files:**
- Modify: `src/strategies/base.py:158` (add `max_lookback_days: int = 0` after `min_lookback`, with a comment: calendar days of price history `generate_signals` needs; 0 = undeclared)
- Modify: `src/execution/engine.py:786-807` (`_parquet_date_axis`), `:816` (`load_prices`), `:3020` (call site)
- Test: `tests/execution/test_load_prices_trailing_window.py`; regression `tests/execution/test_load_prices_universe_pushdown.py`

**Interfaces:**
- Produces:
  - `BaseStrategy.max_lookback_days: int = 0` — **calendar** days (the spec says "days"; the Task 7 audit converts bars to calendar days).
  - `PRICES_TRAILING_FLOOR_DAYS = 1095` (engine module constant; spec "floor 3 years")
  - `engine.trailing_window_days(strategies) -> int | None`
    - `None` unless `os.environ.get('OPENCLAW_PRICES_TRAILING_WINDOW') == '1'`
    - otherwise `max([PRICES_TRAILING_FLOOR_DAYS] + [int(getattr(s, 'max_lookback_days', 0) or 0) for s in strategies])`
  - `engine._parquet_date_axis(path, date_col: str = 'date', cutoff: str | None = None)`: with `cutoff`, reads with `filters=[(date_col, '>=', cutoff)]`.
  - `engine.load_prices(universe: list, *, lookback_days: int | None = None) -> pd.DataFrame`
    - `None` gives today's path, byte-identical.
    - Otherwise `cutoff = (pd.Timestamp.today().normalize() - pd.Timedelta(days=lookback_days)).strftime('%Y-%m-%d')`, appended to the read filters AND passed to `_parquet_date_axis`.
  - One log line per call, both modes:
    - `[prices_window] mode=full rows=<n_dates>`, or
    - `[prices_window] mode=trailing days=<d> cutoff=<YYYY-MM-DD> rows=<n_dates>`
  - The call site at `:3020` becomes `load_prices(universe, lookback_days=trailing_window_days(strategies))`.

- [ ] **Step 1: Write the failing tests** (fixture pattern from `test_load_prices_universe_pushdown.py:22-60`, tickers `ZZTEST1..3`, 6 years of business days)
- `test_flag_unset_is_byte_identical(monkeypatch)`:
  - With the env unset, `trailing_window_days([...]) is None`.
  - `load_prices(u)` and `load_prices(u, lookback_days=None)` give `pd.testing.assert_frame_equal` to the pre-change frame computed by the existing `_old_path` helper (copy it into this test).
- `test_trailing_window_cuts_rows`:
  - `lookback_days=1095`: `wide.index.min() >= today - 1095d`.
  - The values on the overlapping dates equal the full read's.
- `test_trailing_window_axis_is_windowed` (Review Focus 4):
  - Add an out-of-universe `ZZTEST9` that alone trades on one date inside the window and one date outside it.
  - The windowed frame contains the inside date as an all-NaN row and does NOT contain the outside date.
  - `load_prices.last_parquet_max_date` still equals the parquet max.
- `test_window_days_is_max_of_declared_and_floor(monkeypatch)`:
  - Flag `=1`, stub strategies with `max_lookback_days` 0, 400 and 2000: result is 2000.
  - With all 0: result is 1095.
- `test_prices_window_log_line(caplog)`: exactly one `[prices_window]` record per call in each mode.

- [ ] **Step 2: Run to verify failure**

Run: `nice -n 19 python3 -m pytest tests/execution/test_load_prices_trailing_window.py -W error -q`
Expected: FAIL (`TypeError: unexpected keyword 'lookback_days'`).

- [ ] **Step 3: Implement** per the Interfaces block. The close-proxy append at `:900-907` is untouched.

- [ ] **Step 4: Run to verify pass**

Run:
- `nice -n 19 python3 -m pytest tests/execution/test_load_prices_trailing_window.py -W error -q`
- `nice -n 19 python3 -m pytest tests/execution/test_load_prices_universe_pushdown.py -W error -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/strategies/base.py src/execution/engine.py tests/execution/test_load_prices_trailing_window.py
git commit -m "feat(engine): A3 trailing-window live price load behind OPENCLAW_PRICES_TRAILING_WINDOW (default off)"
```

---

### Task 7: A3 — `scripts/audit_strategy_lookbacks.py`

**Files:**
- Create: `scripts/audit_strategy_lookbacks.py`
- Test: `tests/scripts/test_audit_strategy_lookbacks.py`

**Interfaces:**
- Produces:
  - `infer_lookback(source: str) -> dict` with keys:
    - `bars: int` — max integer literal across `rolling(N`, `rolling(window=N`, `shift(N`, `pct_change(N`, `diff(N`, `iloc[-N`, `.tail(N`
    - `unbounded: list[str]` — the distinct tokens among `ewm(`, `expanding(`, `cumsum(`, `cumprod(`, `cummax(`, `cummin(`
    - `non_literal: int` — count of those calls whose first argument is not an integer literal
    - `declared: int | None` — from `max_lookback_days\s*(?::\s*int)?\s*=\s*(\d+)`
  - `bars_to_calendar_days(bars: int) -> int` = `math.ceil(bars * 365 / 252) + 7`
  - `audit(impl_dir: Path, window_days: int = 1095) -> list[dict]` — one row per `*.py`: `{strategy_file, declared, inferred_days, unbounded, non_literal, exceeds: bool}`, where `exceeds = max(declared or 0, inferred_days) > window_days`
  - CLI: `--impl-dir` (default `src/strategies/implementations`), `--window-days 1095`, `--json`
    - prints a table, then `[lookback_audit] files=… exceeds=… unbounded=… non_literal=… window_days=…`
    - exit 0 (report-only; the flip gate is the dry-run signals parity day)

- [ ] **Step 1: Write the failing tests** (synthetic source strings and a tmp impl dir; never the real implementations)
- `test_infer_literals`: `"x.rolling(252).mean(); y.shift(5); z.iloc[-300]"` gives `bars == 300`.
- `test_infer_unbounded_and_non_literal`: `"s.ewm(span=20).mean(); s.rolling(self.n)"` gives `unbounded == ['ewm(']`, `non_literal == 1`.
- `test_declared_parsed`: `"max_lookback_days: int = 1500"` gives `declared == 1500`.
- `test_audit_flags_exceeders(tmp_path)`: two files, one with `rolling(800)` (→ 1167 calendar days > 1095), so exactly one row has `exceeds True`.

- [ ] **Step 2: Run to verify failure**

Run: `nice -n 19 python3 -m pytest tests/scripts/test_audit_strategy_lookbacks.py -W error -q`
Expected: FAIL.

- [ ] **Step 3: Implement** (regex over source text; no imports of strategy modules).

- [ ] **Step 4: Run to verify pass**

Same command. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/audit_strategy_lookbacks.py tests/scripts/test_audit_strategy_lookbacks.py
git commit -m "feat(scripts): A3 strategy lookback audit (declared vs inferred, unbounded ewm/cum flagged)"
```

**Flip runbook (controller):**
1. Run the audit; set `max_lookback_days` on every `exceeds` strategy as a separate reviewed change.
2. Run one `--dry-run` signals day with the flag on and one with it off. Require identical signal sets.
3. Only then set `OPENCLAW_PRICES_TRAILING_WINDOW=1`.

---

### Task 8: A6 — options storage API, routed writers, `split_options_master.py`

**Files:**
- Modify: `src/data/parquet_store.py`: options section near `:238-266`; make `read_filtered` (`:192`), `max_date` (`:217`) and `row_count` (`:229`) directory-aware.
- Modify: `src/pipeline/backfillers/alpaca_options.py:234-275` (`_master_readable`, `_merge_write`)
- Modify: `scripts/populate_master_parquets.py:95`: refuse with exit 2 when `OPENCLAW_OPTIONS_MONTHLY=1`. It is a legacy bootstrap that overwrites the whole single file.
- Create: `scripts/split_options_master.py`
- Test: `tests/pipeline/test_options_monthly_store.py`, `tests/scripts/test_split_options_master.py`

**Interfaces:**
- Produces in `parquet_store`:
  - `OPTIONS_DIR = MASTER_DIR / 'options_eod'`
  - `options_monthly_enabled() -> bool` (env `OPENCLAW_OPTIONS_MONTHLY == '1'`, read at call time)
  - `options_path() -> Path`: `OPTIONS_DIR` when enabled, else `OPTIONS_PATH`
  - `options_month_files(root: Path | None = None) -> list[Path]`: sorted `<root>/[0-9][0-9][0-9][0-9]-[0-9][0-9].parquet`; `root` defaults to `OPTIONS_DIR`
  - `options_dataset() -> pyarrow.dataset.Dataset`
    - Single file when disabled.
    - When enabled, the directory's month files with `schema=pa.unify_schemas([pq.read_schema(f) for f in files], promote_options='permissive')`, so a month whose column is all-null (`null` type) unifies with `double`.
    - Raises `FileNotFoundError` when enabled but no month files exist.
  - `options_read_table(columns: Sequence[str], filters=None, read_dictionary: Sequence[str] | None = None) -> pa.Table`
    - `filters` accepts a DNF list or a `pc.Expression` (convert lists via `pq.filters_to_expression`).
    - `read_dictionary` maps to `ds.ParquetFileFormat(default_fragment_scan_options=…)` / `read_options=ds.ParquetReadOptions(dictionary_columns=…)`.
    - Single-file mode delegates to `pq.read_table(OPTIONS_PATH, columns=…, filters=…, read_dictionary=…)`, byte-identical.
  - `_sql_source(path: Path) -> str`
    - file: `read_parquet('<p>', hive_partitioning=false)`
    - directory: `read_parquet(['<f1>', …], union_by_name=true, hive_partitioning=false)`
    - Used by `read_filtered`, `max_date` and `read_latest_per_ticker`.
  - `row_count(path)`: for a directory, the sum of `pq.read_metadata(f).num_rows` over `options_month_files(path)`.
  - `append_options(df: pd.DataFrame, key_cols: Sequence[str] = OPTIONS_KEYS, mode: str = 'replace') -> int`
    - Disabled: `append_dedup(OPTIONS_PATH, df, key_cols, mode)`, byte-identical.
    - Enabled: group `df` by `pd.to_datetime(df['date']).dt.strftime('%Y-%m')` and run `append_dedup(OPTIONS_DIR/f'{m}.parquet', part, key_cols, mode)` per month.
    - Returns `row_count(options_path())`.
    - Emits one `[options_store] mode=<file|monthly> months=<m1,m2> rows_in=<n> total=<t>` line.
  - `write_options(rows)` → `append_options(pd.DataFrame(list(rows)))`. The empty case returns `row_count(options_path())`.
- Produces in `alpaca_options`:
  - `_merge_write` calls `append_options(new_df, key_cols=['date','contract_symbol'], mode='replace')` when `options_monthly_enabled()` and `Path(parquet_path).resolve() == OPTIONS_PATH.resolve()`. Otherwise (including the `OPTIONS_EOD_PARQUET` dry-run override at `:39-42`/`:451`) it keeps today's `append_dedup(parquet_path, …)`.
  - `_master_readable` validates each month file's footer when the directory is active.
  - `backfill_universe_5y.py:918` and `backfill_options_eod_cutover_gap.py` reach the store through `_append_parquet` → `_merge_write`, so they need no edit.
- Produces in `scripts/split_options_master.py`:
  - `split(src: Path, out_dir: Path, *, months: list[str] | None, apply: bool, spill_dir: Path, memory_limit: str = '1GB', threads: int = 1) -> dict`
    - Takes `_file_lock(src)`.
    - Lists the months with `SELECT DISTINCT substr(CAST(date AS VARCHAR),1,7)`.
    - Per month: `COPY (SELECT * FROM read_parquet(src, hive_partitioning=false) WHERE substr(CAST(date AS VARCHAR),1,7)='<m>') TO '<out>/<m>.parquet.tmp' (FORMAT PARQUET, COMPRESSION 'snappy')`.
    - Verifies the per-month count against the source, then `os.replace`.
    - `--months 2026-10` re-splits only that month. This is the pre-flip refresh that closes the gap between split and flip.
    - `--verify` (no writes) prints per-month `src_rows` vs `dir_rows` and exits 2 on any mismatch.
  - Run line: `[split_options] months=… rows_src=… rows_dir=… mismatches=… applied=…`

- [ ] **Step 1: Write the failing tests** (`tests/pipeline/test_options_monthly_store.py`, tickers `ZZTEST1/2`, a 3-month synthetic chain, `monkeypatch.setattr(ps, 'OPTIONS_PATH'/'OPTIONS_DIR', tmp…)`)
- `test_flag_unset_writes_single_file_byte_identical`: with the env unset, `write_options(rows)` makes the same `append_dedup` call (spy) on `OPTIONS_PATH` as before, and `OPTIONS_DIR` is not created.
- `test_append_options_splits_batch_by_month` (Review Focus 1):
  - Flag `=1`; one batch spans `2026-09-30` and `2026-10-01`.
  - Two files exist; each holds only its month.
  - Re-sending the `2026-10-01` rows with new prices replaces them in `2026-10.parquet`, leaves `2026-09.parquet` byte-identical (sha256), and leaves the total unchanged.
- `test_dataset_unifies_null_typed_month`:
  - Write `2026-08.parquet` with `open_interest` all-None (`null` type) and `2026-09.parquet` with float values.
  - `options_read_table(['ticker','open_interest'])` returns both months with type `double`.
- `test_read_filtered_max_date_row_count_dir_aware`: with the flag on, `read_filtered(options_path(), where="ticker='ZZTEST1'")`, `max_date(options_path())` and `row_count(options_path())` agree with a pandas oracle over the union.
- `test_alpaca_merge_write_routes_and_keeps_keys(monkeypatch)`:
  - Flag on, `parquet_path` = the patched `OPTIONS_PATH`: `append_options` is called with `key_cols=['date','contract_symbol']`.
  - With `parquet_path` = another tmp file: plain `append_dedup` on that file.
- `test_options_store_log_line(caplog)`: one `[options_store]` line per write.

`tests/scripts/test_split_options_master.py`:
- `test_split_roundtrip_counts(tmp_path)`: after `split(..., apply=True)`, per-month counts match and the union multiset of rows equals the source.
- `test_split_month_refresh(tmp_path)`: append rows to the source for the last month; `split(months=['2026-10'], apply=True)` updates only `2026-10.parquet` (the other files' mtimes are unchanged).
- `test_verify_detects_mismatch(tmp_path)`: delete one row from a month file via rewrite; `main(['--verify', …])` raises `SystemExit(2)`.

- [ ] **Step 2: Run to verify failure**

Run:
- `nice -n 19 python3 -m pytest tests/pipeline/test_options_monthly_store.py -W error -q`
- `nice -n 19 python3 -m pytest tests/scripts/test_split_options_master.py -W error -q`

Expected: FAIL.

- [ ] **Step 3: Implement** per the Interfaces block.

- [ ] **Step 4: Run to verify pass**

Run the same two commands, plus the regressions:
- `nice -n 19 python3 -m pytest tests/pipeline/test_parquet_store.py -W error -q`
- `nice -n 19 python3 -m pytest tests/pipeline/test_options_archive.py -W error -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/data/parquet_store.py src/pipeline/backfillers/alpaca_options.py scripts/populate_master_parquets.py scripts/split_options_master.py tests/pipeline/test_options_monthly_store.py tests/scripts/test_split_options_master.py
git commit -m "feat(storage): A6 per-month options_eod store behind OPENCLAW_OPTIONS_MONTHLY; both writers routed; split script"
```

---

### Task 9: A6 — move every options_eod reader and freshness consumer onto the resolver

The spec says "the 21 non-strategy readers (listed in NOTES-timescaledb.md F3)". F3 gives only a count. A grep of `options_eod` across `src/ scripts/` (excluding `src/strategies/implementations/`) finds 46 files. Each one is classified below.

**Opens the master for reading → change (this task):**

| # | File:line | Change |
|---|-----------|--------|
| 1 | `src/execution/engine.py:1100` (`opts_path`), `:1142` → `_load_options_window` `:338-343` (`_pq.read_table(path, …, read_dictionary=[…])`) | `opts_path = ps.options_path()`; `_load_options_window` calls `ps.options_read_table(columns, filters, read_dictionary)` |
| 2 | `src/data/parquet_reader.py:91` (`op_options`), `:191` (`op_freshness_per_ticker` raw `read_parquet('{ps.OPTIONS_PATH}')`), `:232` (`op_freshness`) | `ps.options_path()`; `:191` uses `ps._sql_source(ps.options_path())` |
| 3 | `src/maintenance/doctor.py:257-259` (`options_archive_freshness` → `_parquet_last_date`, `:124`) | Directory-aware: max over `options_month_files()` footers when active |
| 4 | `src/system_checks/checks/master_freshness.py:40`, `:107` `_max_from_stats`, `:167` glob | When active, judge `options_eod.parquet` by the newest month file's stats. The directory is not a `*.parquet` glob hit, so no "undeclared" noise. |
| 5 | `src/strategies/sync_data_ledger.py:105`, `:172` | `_stats` accepts a directory (`ParquetFile` → `options_month_files` + `_sql_source`) |
| 6 | `src/pipeline/backfillers/__init__.py:76-83` (`_parquet_stats` full `pd.read_parquet`) | For stem `options_eod` use `ps.row_count(options_path())` + `ps.max_date` / DuckDB `min` (no full read) |
| 7 | `src/ingestion/ingest_iv_history.py:43`, `:85`, `:123-130` | `ps.options_read_table(...)`; existence via `ps.options_path().exists()` |
| 8 | `scripts/build_options_aggregates.py:48`, `:73`, `:78` | `ps.options_read_table(...)` |
| 9 | `scripts/build_options_surface.py:35`, `:48` | `ps.options_read_table(..., read_dictionary=['ticker','option_type'])` |
| 10 | `scripts/options_parity_check.py:49`, `:111` | `ps.options_read_table(...)` |
| 11 | `scripts/run_acting_ingest.py:159-164` (`_master_coverage`) | For `options_eod`: `ps.options_read_table(['ticker'])` |

**Writes the master → routed in Task 8:**

| # | File:line |
|---|-----------|
| 12 | `src/data/parquet_store.py:239`, `:262-266` |
| 13 | `src/pipeline/backfillers/alpaca_options.py:39-42`, `:234-275` |
| 14 | `scripts/backfill_universe_5y.py:73`, `:918` (via `_append_parquet`) |
| 15 | `scripts/backfill_options_eod_cutover_gap.py` (via `_append_parquet`) |
| 16 | `src/pipeline/backfillers/universe_options.py` (delegates to `_append_parquet`) |
| 17 | `scripts/populate_master_parquets.py:95` (refusal guard) |

**Indirect → no change:**

| # | File:line | Why no change |
|---|-----------|---------------|
| 18 | `scripts/refresh_options_aggregates.py:46` | Subprocesses `build_options_surface.py` |
| 19 | `src/data/parquet_store.js` / `src/channels/api/server.js` | Go through the `parquet_reader.py` CLI |
| 20 | `src/system_checks/checks/storage.py:20` | Existence of the single file. It persists, frozen and never deleted, after the flip. |
| 21 | `src/pipeline/quarantine_filter.py:20` | Name-only docstring; no caller passes `options_eod.parquet` |

**Name/label/regex/prompt only → no change:** `src/channels/discord/bot.js:465`, `src/channels/api/server.js:1435-1455,3902,9674`, `src/strategies/generate_sidecars.py:20-21`, `src/strategies/registry.py:463`, `src/maintenance/doctor.py:68,130`, `src/backtest/vol_index.py:7`, `src/engine/cron-schedule.js:774`, `src/agent/curators/data_tier_filter.js:69-77`, `src/pipeline/collector.js:706-2626` (log text; flushes go through `write_options`), `src/pipeline/alpaca_options.js:13`, `src/pipeline/data-task-executor.js:17,101`, `src/system_checks/checks/acting_ingest_coverage.py:55`, `src/system_checks/checks/options_aux_freshness.py:8`, `src/services/mastermind_chat/persona.js`, `src/execution/acting_ingest_plan.py:18`, `src/ingestion/ingest_cboe_chains.py`, `src/ingestion/intraday_options.py`, `scripts/generate_missing_requirements.py`, `scripts/sp7_c3_guarded_flip.py`, `src/database/migrations/114_data_quarantine.sql`, `src/strategies/manifest.json`, the `*.md` prompts/skills.

**Files:** rows 1–11 above.
- Test: `tests/pipeline/test_options_readers_resolver.py`

**Interfaces:**
- Consumes: `options_path`, `options_month_files`, `options_dataset`, `options_read_table`, `_sql_source`, `row_count`, `max_date` (Task 8)
- Produces: no new public names. `doctor._parquet_last_date(path)` accepts a directory path.

- [ ] **Step 1: Write the failing tests**
- `test_freshness_consumers_follow_monthly_dir` (Review Focus 2):
  - Set up a frozen single file with max date `2026-09-01` and a monthly dir whose newest row is today − 1.
  - Flag on: `doctor._check_options_archive_freshness()` passes, and `master_freshness._master_freshness()` does not report `options_eod.parquet` stale.
  - Patch `doctor.ROOT` / `chk._MASTER_DIR` to tmp.
- `test_engine_options_window_reads_dir(monkeypatch)`: flag on, `_load_options_window(ps.options_path(), cols, 14, today)` returns the same rows as the single-file read of identical data with the flag off (frames equal after `sort_values`).
- `test_parquet_reader_ops_dir_aware`: `op_options({'ticker':'ZZTEST1'})`, `op_freshness_per_ticker({'tickers':['ZZTEST1']})` and `op_freshness({})['options_data']` agree across modes.
- `test_sync_ledger_and_backfiller_stats_dir_aware`: `sync_data_ledger._stats(dir)` and `backfillers._parquet_stats('options_eod')` return the union's min/max/row_count; monkeypatch `pd.read_parquet` to raise, to prove there is no full read.
- `test_scripts_use_resolver`: for each of rows 7–11, import the module (scripts via `spec_from_file_location`), monkeypatch `ps.options_read_table` with a recorder, call the module's read function on a tiny range, and assert the recorder was hit.

- [ ] **Step 2: Run to verify failure**

Run: `nice -n 19 python3 -m pytest tests/pipeline/test_options_readers_resolver.py -W error -q`
Expected: FAIL.

- [ ] **Step 3: Implement** the changes in rows 1–11.

- [ ] **Step 4: Run to verify pass**

Run the same command, plus these regressions:
- `tests/system_checks/test_master_freshness_shares_outstanding.py`
- `tests/scripts/test_build_options_surface.py`
- `tests/scripts/test_build_options_surface_oi.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/execution/engine.py src/data/parquet_reader.py src/maintenance/doctor.py src/system_checks/checks/master_freshness.py src/strategies/sync_data_ledger.py src/pipeline/backfillers/__init__.py src/ingestion/ingest_iv_history.py scripts/build_options_aggregates.py scripts/build_options_surface.py scripts/options_parity_check.py scripts/run_acting_ingest.py tests/pipeline/test_options_readers_resolver.py
git commit -m "feat(storage): A6 every options_eod reader + freshness consumer goes through the options resolver"
```

**Flip runbook (controller):**
1. `split_options_master.py --apply` (transient unit, R1 window).
2. Immediately before the flip: `--months <current YYYY-MM> --apply`, then `--verify`.
3. Set `OPENCLAW_OPTIONS_MONTHLY=1` in `/root/openclaw/.env` so every Python entrypoint sees it via `load_dotenv`. Restart user-scope johnbot.
4. After one collect plus one options-archive run, `row_count(options_path())` must equal `row_count(OPTIONS_PATH)` plus the rows written since the split. The `[options_store] mode=monthly` lines must be present.

---

### Task 10: A7 — hive dual-write, `build_prices_hive.py`, `prices_hive_parity.py`

Ships after A1 has soaked (§1 A7, R6). The single file stays authoritative.

**Files:**
- Modify: `src/data/parquet_store.py`
  - `append_dedup` `:145`, `:151`, `:154`: add `hive_partitioning=false` to every `read_parquet(...)` literal. This is harmless for flat paths and required under `year=YYYY/`.
  - `write_prices` `:255-259`
- Create: `scripts/build_prices_hive.py`, `scripts/prices_hive_parity.py`
- Test: `tests/pipeline/test_prices_hive_store.py`, `tests/scripts/test_prices_hive_parity.py`

**Interfaces:**
- Produces in `parquet_store`:
  - `PRICES_HIVE_DIR = MASTER_DIR / 'prices'`
  - `prices_hive_enabled() -> bool`: env `OPENCLAW_PRICES_HIVE == '1'`
  - `prices_hive_part(year: int) -> Path`: `PRICES_HIVE_DIR / f'year={year}' / 'part-0.parquet'`. One file per year, deduped in place by `append_dedup`. No per-day part files, so a `mode='replace'` re-write of a day cannot duplicate keys.
  - `write_prices(rows)`:
    1. Unchanged authoritative `append_dedup(PRICES_PATH, …)` first.
    2. Then, when enabled: `df2 = df.assign(date_d=pd.to_datetime(df['date']).dt.date)`, grouped by year, `append_dedup(prices_hive_part(y), part, PRICES_KEYS, 'replace')` per year.
    3. A hive failure is caught and logged as `[prices_hive] status=error:<cls> …`, and never raises into the collector. The parity script is the drift detector.
    4. One `[prices_hive] status=ok|error:<cls> years=<y…> rows_in=<n> total_hive=<t>` line per call while enabled.
    5. The return value is still the single-file count.
- Produces in `scripts/build_prices_hive.py`:
  - `build(src: Path, out_dir: Path, *, years: list[int] | None, apply: bool, spill_dir: Path, memory_limit: str = '1GB', threads: int = 1, row_group_size: int = 122880) -> dict`
  - Under `_file_lock(src)`, per year: `COPY (SELECT *, CAST(date AS DATE) AS date_d FROM read_parquet(src, hive_partitioning=false) WHERE date >= 'Y-01-01' AND date < 'Y+1-01-01' ORDER BY date, ticker) TO '<part>.tmp' (FORMAT PARQUET, COMPRESSION 'snappy', ROW_GROUP_SIZE n)`; verify the count, then `os.replace`.
  - Run line `[build_prices_hive] years=… rows_src=… rows_hive=… applied=…`.
- Produces in `scripts/prices_hive_parity.py`:
  - `parity(src: Path, hive_dir: Path, *, sample: int = 2000, seed: int = 0, memory_limit: str = '1GB') -> dict` with keys `rows_file, rows_hive, dup_keys_hive, ticker_maxdate_mismatch, sample_n, sample_mismatch, verdict`. It uses DuckDB over `read_parquet('<hive>/*/*.parquet', hive_partitioning=true)`.
  - `rows_*` come from footers.
  - `dup_keys_hive` = `count(*) - count(DISTINCT (ticker,date))`.
  - Per-ticker `max(date)` is compared via `FULL OUTER JOIN`.
  - The sample is `USING SAMPLE reservoir(<n> ROWS) REPEATABLE (<seed>)` from the single file, joined on (ticker,date) and compared on `close` with `IS NOT DISTINCT FROM`.
  - Exit 0 on PASS, 1 on FAIL. One `[prices_hive_parity] … verdict=PASS|FAIL` line.

- [ ] **Step 1: Write the failing tests**

`tests/pipeline/test_prices_hive_store.py` (patch `PRICES_PATH`, `PRICES_HIVE_DIR` to tmp; tickers `ZZTEST1/2`):
- `test_flag_unset_no_hive_tree`: with the env unset, `write_prices(rows)` creates no `prices/` directory and calls `append_dedup` exactly once (spy).
- `test_dual_write_splits_by_year_and_dedups`:
  - Flag on; a batch spans `2025-12-31` and `2026-01-02`; one part file per year.
  - Re-writing `2026-01-02` with new closes leaves exactly one row per key in `year=2026`.
  - `date_d` has type `date32`.
- `test_hive_part_file_has_no_year_column` (Review Focus 3): after two successive writes into `year=2026`, `pq.read_schema(part).names` contains no `year`; `pyarrow.dataset.dataset(PRICES_HIVE_DIR, partitioning='hive').to_table()` succeeds.
- `test_hive_failure_never_breaks_authoritative_write(monkeypatch, caplog)`:
  - Make the hive `append_dedup` raise `OSError`.
  - `write_prices` returns the single-file count, the single file holds the rows, and one `[prices_hive] status=error:OSError` line is logged.

`tests/scripts/test_prices_hive_parity.py`:
- `test_build_then_parity_pass(tmp_path)`: three years of synthetic data; `build(apply=True)` then `parity()` gives verdict `PASS`, 0 dups and 0 mismatches.
- `test_parity_detects_drift(tmp_path)`: after the build, append one date to the single file only, so `ticker_maxdate_mismatch > 0`, verdict `FAIL`, and `main()` exits 1.

- [ ] **Step 2: Run to verify failure**

Run:
- `nice -n 19 python3 -m pytest tests/pipeline/test_prices_hive_store.py -W error -q`
- `nice -n 19 python3 -m pytest tests/scripts/test_prices_hive_parity.py -W error -q`

Expected: FAIL.

- [ ] **Step 3: Implement** per the Interfaces block.

- [ ] **Step 4: Run to verify pass**

Run the same two commands, plus `tests/pipeline/test_parquet_store.py` (regression for the `hive_partitioning=false` edit). Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/data/parquet_store.py scripts/build_prices_hive.py scripts/prices_hive_parity.py tests/pipeline/test_prices_hive_store.py tests/scripts/test_prices_hive_parity.py
git commit -m "feat(storage): A7 hive-partitioned prices dual-write behind OPENCLAW_PRICES_HIVE + build and parity scripts"
```

---

### Task 11: A7 — the four heavy readers read via `pyarrow.dataset` when `OPENCLAW_PRICES_HIVE=1`

**Files:**
- Modify: `src/data/parquet_store.py` (reader helper)
- Modify:
  - `src/execution/engine.py:786-807` (`_parquet_date_axis`) and `:854-859` (`load_prices` read)
  - `src/backtest/unified_backtest.py:319-326` (`load_prices_panels`)
  - `src/backtest/backtest_panel.py:224-244` (`load_prices`)
  - `src/execution/asset_correlation.py:126-127` (`_load_returns`)
- Test: `tests/pipeline/test_prices_hive_readers.py`; regressions `tests/execution/test_load_prices_universe_pushdown.py`, `tests/execution/test_load_prices_trailing_window.py`, `tests/test_arrow_dictionary_read_equivalence.py`

**Interfaces:**
- Consumes: `PRICES_HIVE_DIR`, `prices_hive_enabled` (Task 10)
- Produces:
  - `parquet_store.prices_read_table(columns: Sequence[str], filters=None, read_dictionary: Sequence[str] | None = None) -> pa.Table`
    - When `prices_hive_enabled()` and at least one `year=*/part-0.parquet` exists: `ds.dataset(PRICES_HIVE_DIR, format=ds.ParquetFileFormat(read_options=ds.ParquetReadOptions(dictionary_columns=read_dictionary or [])), partitioning='hive').to_table(columns=list(columns), filter=<expr>)`.
    - Otherwise: `pq.read_table(PRICES_PATH, columns=…, filters=…, read_dictionary=…)`, byte-identical to today.
    - `filters` accepts a DNF list or a `pc.Expression`.
    - When enabled but the tree is empty: fall back to the file and log `[prices_source] layout=file reason=hive_empty`.
    - Otherwise one `[prices_source] layout=hive|file` line per call while the flag is on (no line when unset — the default stays byte-identical in logs too).
  - Each reader replaces its `pd.read_parquet` / `pq.read_table` on the prices master with `prices_read_table(...)` (`.to_pandas()` where a frame was returned). Test fixtures still monkeypatch the module path constants, so each reader passes its own `PRICES_PARQUET` / `PARQUET` / `master_path` through an optional `path` kwarg: `prices_read_table(columns, filters=None, read_dictionary=None, *, path: Path | None = None)`. The hive branch applies only when `path` is `None` or equals `PRICES_PATH`.

  `engine._read_parquet_window` for HV20 (`:274-282`) is NOT switched. The spec names only `load_prices`, and the single file stays authoritative.

- [ ] **Step 1: Write the failing tests** (`tests/pipeline/test_prices_hive_readers.py`)
- Build a 3-year synthetic master plus its hive tree via Task 10's `build()` in `tmp_path`. Patch every module's path constant and `ps.PRICES_PATH` / `ps.PRICES_HIVE_DIR`.
- `test_engine_load_prices_hive_equals_file`: `load_prices(u)` with the flag on equals the flag-off frame (`assert_frame_equal`, including `last_parquet_max_date`).
- `test_unified_panels_hive_equals_file`: `close_wide` and `bars_by_ticker` are equal across modes (columns order included), with and without `tickers=`.
- `test_backtest_panel_and_asset_corr_hive_equals_file`: `backtest_panel.load_prices(['ZZTEST1'])` (after `sort_values(['ticker','date'])`) and `asset_correlation._load_returns(['ZZTEST1','ZZTEST2'], 20, as_of=…)` are equal across modes.
- `test_prices_source_log_and_empty_fallback(caplog)`: flag on with an empty `prices/` directory reads the file and logs `reason=hive_empty`; flag unset logs no `[prices_source]` line.

- [ ] **Step 2: Run to verify failure**

Run: `nice -n 19 python3 -m pytest tests/pipeline/test_prices_hive_readers.py -W error -q`
Expected: FAIL.

- [ ] **Step 3: Implement** per the Interfaces block.

- [ ] **Step 4: Run to verify pass**

Run the same command, plus the three regressions listed under Files. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/data/parquet_store.py src/execution/engine.py src/backtest/unified_backtest.py src/backtest/backtest_panel.py src/execution/asset_correlation.py tests/pipeline/test_prices_hive_readers.py
git commit -m "feat(storage): A7 four heavy prices readers read the hive tree via pyarrow.dataset when OPENCLAW_PRICES_HIVE=1"
```

**Flip runbook (controller):**
1. `build_prices_hive.py --apply` (R1 window).
2. Set `OPENCLAW_PRICES_HIVE=1` for writers.
3. Run `prices_hive_parity.py` daily for ≥5 sessions. All must be PASS.
4. Only then does the operator decide whether readers stay on the hive tree. The single file remains authoritative until that decision.

---

## Self-review notes

- **Coverage:**
  - A1 → Tasks 1–2
  - A2 → Task 3
  - A3 → Tasks 6–7
  - A4 → Task 4
  - A5 → Task 5
  - A6 → Tasks 8–9
  - A7 → Tasks 10–11
  - §7 host-op evidence: resort run line (A1); `pg_relation_size` in the Task 5 runbook (A5); `--verify` / parity scripts (A6/A7).
- **Order:** Tier 1 (Tasks 1–5) is independent. Within Tier 2:
  - Task 7 is independent of Task 6.
  - Task 9 needs Task 8.
  - Task 11 needs Task 10, and Task 6 (both edit `load_prices`; Task 11 rebases on Task 6's `lookback_days`/`cutoff`).
- **Names used across tasks:**
  - `options_path`, `options_month_files`, `options_dataset`, `options_read_table`, `_sql_source`, `append_options`
  - `prices_hive_enabled`, `prices_hive_part`, `prices_read_table`
  - `rowgroup_span_report`, `layout_verdict`
  - `trailing_window_days`, `PRICES_TRAILING_FLOOR_DAYS`
