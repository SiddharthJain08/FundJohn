# Stream E — Database and Ops (hot backtest-trades table + job-stat table) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop every hot backtest-trades reader from seq-scanning the 31 M-row `strategy_backtest_trades` (E1), and give the 26 in-process crons and the systemd timers one `scheduled_job_runs` table plus an overdue check (E2).

**Architecture:** E1 adds a cache table `strategy_backtest_trades_primary`. It holds exactly the trade rows of runs with `primary_window=TRUE`. Migration 166 backfills it once. `unified_backtest` keeps it in sync inside the demotion transaction, the tournament re-attribution mirrors its rename, and readers switch to it behind `OPENCLAW_BT_TRADES_HOT=1`. Each reader swaps only the table name, so run selection stays identical. A system check proves the sync. E2 adds `scheduled_job_runs`, written by a Python CLI/lib (systemd `ExecStartPre`/`ExecStopPost` drop-in, shipped as an uninstalled snapshot) and a JS twin used by one `_sched()` wrapper around all 26 `cron.schedule` entries. `scheduled_jobs_overdue` judges each job against an explicit interval policy.

**Tech Stack:** PostgreSQL 16 DDL (plain SQL + one PL/pgSQL `DO` block), psycopg2, node-postgres (`pg`), node-cron, pytest, `node --test`.

**Spec:** `docs/specs/2026-10-19-six-repo-adoptions-spec.md` §5 (Stream E), rulings R3; notes `/root/.learnings/reviews/2026-10-19-six-repo-fit-review/NOTES-timescaledb.md` F2, F6, D2, T10.

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

Stream E specifics:
- Migrations are **166** (`166_backtest_trades_primary.sql`) and **167** (`167_scheduled_job_runs.sql`). 164 = Stream A, 165 = Stream B. Never edit `093_unified_backtest_tables.sql`, because Stream A owns its lines 84-88.
- The migration runner (`src/database/postgres.js:42-55`, called at `src/channels/discord/bot.js:1663`) re-runs EVERY file on EVERY johnbot start, as one implicit transaction per file. It swallows any error whose message contains `already exists`, which rolls back the whole file silently. So every statement is `IF NOT EXISTS`, and the backfill runs at most once.
- `strategy_backtest_trades_primary` is a **cache**, not a canonical table. Its `DELETE … WHERE strategy_id=%s` is allowed by R3 and spec §5. `strategy_backtest_trades` itself is never DELETEd from or UPDATEd. The tournament rename at `research-orchestrator.js:2014` is pre-existing and left alone.
- `OPENCLAW_BT_TRADES_HOT=1` switches the six readers to the hot table. Default unset means every reader's SQL is byte-identical to today's. The writer maintains the hot table whenever it exists, regardless of this flag.
- `OPENCLAW_JOB_STAT=0` disables every job-stat write (JS and Python). Default unset means writes are on. Job-stat changes no sizing, signal or gate.
- Stay out of Stream D's regions of `src/backtest/unified_backtest.py`: the per-bar loop and the `config_json` literal at `:1828-1900`. Stream E touches only `:1987-2013`.
- Drop-ins are NOT installed by implementers. They live in `docs/systemd/job-stat/`, which `scripts/install_systemd.sh` does not glob.
- No backoff/retry semantics anywhere (spec §5 E2; notes §5).

## Review Focus

1. **Tournament winner re-attribution** (`research-orchestrator.js:2012-2015`) renames `strategy_id` on runs and base trades after the backtest has persisted under the `_tvN` id. The hot rows must follow, otherwise similarity/S_adj is keyed to a dead id and the next backtest's DELETE misses them. Covered by: Task 2 dry-run check `hot table re-attributed`, and Task 4 `test_strategy_id_mismatch_fails`.
2. **A new primary run with zero trades** must still clear the strategy's previous hot rows, because base readers see zero rows for it. Covered by: Task 2 `test_sync_zero_trades_still_deletes`.
3. **A Mon–Fri job checked on a Monday morning after a Friday success** must PASS. A NYSE holiday must not count as a missed session. Covered by: Task 7 `test_session_job_monday_after_friday_passes` and `test_holiday_not_counted_as_missed`.
4. **A systemd job killed by the OOM killer or a signal** (`EXIT_CODE=killed`, `EXIT_STATUS=KILL`, `SERVICE_RESULT=oom-kill`) must record `success=false`, `rc=NULL` and the result in `note`. It must never crash the hook or record success. Covered by: Task 5 `test_systemd_outcome_oom_kill`.
5. **A hung DB write** must not delay a cron callback such as the 15:55 execute, and a callback that throws must still throw exactly as before. Covered by: Task 6 `runTracked calls fn before the start write resolves` and `runTracked rethrows the original error`.

---

### Task 1: Migration 166 — hot table + one-shot backfill

**Files:**
- Create: `src/database/migrations/166_backtest_trades_primary.sql`
- Test: `tests/database/test_migration_166_trades_primary.py`

**Interfaces:**
- Produces: table `strategy_backtest_trades_primary` with the 15 columns of `strategy_backtest_trades` (`093_unified_backtest_tables.sql:62-79`): same names, same types, same NOT NULL. `PRIMARY KEY (run_id, trade_seq)`, no FK. Index `idx_bt_trades_primary_strategy ON (strategy_id, entry_date)`. Backfill marker: `COMMENT ON TABLE strategy_backtest_trades_primary IS 'backfilled:166'`.

Contents, in order. Every statement is idempotent:
1. `CREATE TABLE IF NOT EXISTS strategy_backtest_trades_primary (…15 columns…, PRIMARY KEY (run_id, trade_seq));`
2. One `DO $$ … $$;` block:
   - If `obj_description('strategy_backtest_trades_primary'::regclass, 'pg_class') = 'backfilled:166'`, then `RAISE NOTICE '[166] backfill already done — skipped'` and `RETURN`.
   - Otherwise:
     - `PERFORM set_config('lock_timeout', '60s', true)` and `PERFORM set_config('statement_timeout', '0', true)`.
     - `LOCK TABLE strategy_backtest_runs IN SHARE MODE`. This blocks a concurrent persist or rename from committing mid-backfill, so the cache cannot drift on day one.
     - `FOR rid IN SELECT run_id FROM strategy_backtest_runs WHERE primary_window ORDER BY run_id LOOP INSERT INTO strategy_backtest_trades_primary (cols) SELECT cols FROM strategy_backtest_trades WHERE run_id = rid ON CONFLICT (run_id, trade_seq) DO NOTHING; END LOOP;`. Chunking per run uses the base PK `(run_id, trade_seq)` as an index range on contiguous heap. It never seq-scans the 5 GB heap.
     - `RAISE NOTICE '[166] backfilled % rows from % primary runs'`, then `COMMENT ON TABLE … IS 'backfilled:166'`.
3. `CREATE INDEX IF NOT EXISTS idx_bt_trades_primary_strategy ON strategy_backtest_trades_primary (strategy_id, entry_date);`. This comes AFTER the backfill (bulk build).

A header comment states:
- this is a cache (R3);
- the runner semantics;
- **expected first-run cost:** ≈5.1 M rows / 304 runs, 2–6 min on this box. Reads ≈0.85 GB of contiguous heap and writes ≈0.8 GB heap + ≈0.37 GB PK + ≈0.05 GB secondary index, so disk grows by ≈1.3 GB. ≈1.5–2 GB of transient WAL is recycled by checkpoints, because an open transaction does not retain WAL. Backtest persists block for that duration;
- later starts cost one catalog lookup;
- the controller pre-applies it with `psql -1 -f` (see Runbook).

- [ ] **Step 1: Write the failing test** (static DDL text only, no DB, following `tests/database/test_stream_b_migration_shape.py`)

```python
def _base_columns():  # parse 093's CREATE TABLE strategy_backtest_trades body up to PRIMARY KEY (never its index lines)
    ...
def test_166_declares_every_base_column_with_same_type():
    hot = _create_body(_sql('166_backtest_trades_primary.sql'), 'strategy_backtest_trades_primary')
    for name, decl in _base_columns():          # 15 entries, e.g. ('entry_price', 'DOUBLE PRECISION NOT NULL')
        assert re.search(rf'^\s*{name}\s+{re.escape(decl)}', hot, re.M), name
def test_166_primary_key_run_id_trade_seq():
    assert re.search(r'PRIMARY KEY\s*\(\s*run_id\s*,\s*trade_seq\s*\)', _sql('166_backtest_trades_primary.sql'))
def test_166_every_create_is_idempotent():
    sql = _sql('166_backtest_trades_primary.sql')
    assert not re.search(r'CREATE (TABLE|INDEX)(?! IF NOT EXISTS)', sql)
def test_166_backfill_is_guarded_locked_and_marked():
    sql = _sql('166_backtest_trades_primary.sql')
    assert "'backfilled:166'" in sql and 'obj_description' in sql
    assert 'LOCK TABLE strategy_backtest_runs IN SHARE MODE' in sql
    assert "set_config('lock_timeout'" in sql
    assert 'ON CONFLICT (run_id, trade_seq) DO NOTHING' in sql
def test_166_secondary_index_built_after_backfill():
    sql = _sql('166_backtest_trades_primary.sql')
    assert sql.index('idx_bt_trades_primary_strategy') > sql.index('END LOOP')
def test_166_never_mutates_the_base_table():
    sql = _sql('166_backtest_trades_primary.sql')
    assert not re.search(r'(DELETE FROM|UPDATE|TRUNCATE)\s+strategy_backtest_trades\b(?!_primary)', sql)
    assert not re.search(r'^\s*COMMIT\b', sql, re.M | re.I)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw/.claude/worktrees/six-e && nice -n 19 python3 -m pytest tests/database/test_migration_166_trades_primary.py -v -W error`
Expected: FAIL (`FileNotFoundError` on `166_backtest_trades_primary.sql`)

- [ ] **Step 3: Write `src/database/migrations/166_backtest_trades_primary.sql`** as specified above.

- [ ] **Step 4: Run test to verify it passes**

Run: same command. Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/database/migrations/166_backtest_trades_primary.sql tests/database/test_migration_166_trades_primary.py
```
```bash
git commit -m "feat(db): migration 166 strategy_backtest_trades_primary hot cache + one-shot locked backfill (E1)"
```

---

### Task 2: Writer-side maintenance (persist transaction + tournament rename)

**Files:**
- Create: `src/backtest/trades_primary.py`
- Modify: `src/backtest/unified_backtest.py:1987-2013` (trade-row build + after the demotion UPDATE; nothing else)
- Modify: `src/agent/research/research-orchestrator.js:2014-2015` (mirror UPDATE)
- Modify: `scripts/tournament_dryrun.js:161-180` (one new check)
- Test: `tests/backtest/test_trades_primary_writer.py`

**Interfaces:**
- Produces (in `src/backtest/trades_primary.py`; imports only `os`, plus `psycopg2.extras` lazily):
  - `HOT_TABLE = 'strategy_backtest_trades_primary'`
  - `TRADE_COLUMNS: tuple[str, ...]`: the 15 columns, in the order of the INSERT at `unified_backtest.py:1996-2000`.
  - `hot_table_exists(cur) -> bool`: `SELECT to_regclass('strategy_backtest_trades_primary') IS NOT NULL`. Never try/except, because a failed statement aborts the caller's transaction.
  - `sync_primary_trades(cur, strategy_id: str, run_id: str, trade_rows: list[tuple]) -> int | None`:
    - Table absent: print `[bt_trades_primary] table absent (migration 166 not applied) — skip strategy=<sid> run_id=<rid>` and return `None`.
    - Otherwise: `DELETE FROM strategy_backtest_trades_primary WHERE strategy_id = %s`, then `execute_values` INSERT of `trade_rows` (`page_size=500`), then print `[bt_trades_primary] strategy=<sid> run_id=<rid> deleted=<n> inserted=<m>` and return `m`.
    - It uses the caller's cursor and never commits.
  - `resync_strategy(cur, strategy_id: str) -> int`:
    - `LOCK TABLE strategy_backtest_runs IN SHARE MODE`;
    - `DELETE … WHERE strategy_id=%s`;
    - `INSERT INTO hot (cols) SELECT t.cols FROM strategy_backtest_trades t JOIN strategy_backtest_runs r ON r.run_id=t.run_id AND r.primary_window WHERE r.strategy_id=%s`;
    - returns rows inserted, and never commits. Task 4's CLI uses it.
- Consumes (unified_backtest): `trade_rows` already built at `:1988-1994`.

- [ ] **Step 1: Write the failing tests** (`FakeCursor` records `(sql, params)`, answers `to_regclass` from a constructor flag, and sets `rowcount`; `psycopg2.extras.execute_values` is monkeypatched to record its call)

```python
def test_sync_absent_table_skips_and_logs(capsys):
    cur = FakeCursor(table_exists=False)
    assert tp.sync_primary_trades(cur, 'S_ZZTEST1', 'r1', [ROW]) is None
    assert not any('DELETE' in s for s, _ in cur.calls)
    assert '[bt_trades_primary] table absent' in capsys.readouterr().out
def test_sync_deletes_then_inserts_in_order(capsys, monkeypatch):
    cur = FakeCursor(table_exists=True, delete_rowcount=7); ev = record_execute_values(monkeypatch)
    assert tp.sync_primary_trades(cur, 'S_ZZTEST1', 'r2', [ROW, ROW2]) == 2
    assert cur.calls[-1] == ('DELETE FROM strategy_backtest_trades_primary WHERE strategy_id = %s', ('S_ZZTEST1',))
    assert ev.sql.strip().startswith('INSERT INTO strategy_backtest_trades_primary') and ev.rows == [ROW, ROW2]
    assert 'deleted=7 inserted=2' in capsys.readouterr().out
def test_sync_zero_trades_still_deletes(monkeypatch):
    cur = FakeCursor(table_exists=True, delete_rowcount=40); ev = record_execute_values(monkeypatch)
    assert tp.sync_primary_trades(cur, 'S_ZZTEST1', 'r3', []) == 0
    assert any(s.startswith('DELETE FROM strategy_backtest_trades_primary') for s, _ in cur.calls) and ev.calls == 0
def test_trade_columns_match_unified_backtest_insert():
    src = Path(ub.__file__).read_text()
    block = src[src.index('INSERT INTO strategy_backtest_trades\n'):]
    assert tuple(re.findall(r'\w+', block[block.index('(')+1:block.index(')')])) == tp.TRADE_COLUMNS
def test_persist_syncs_after_demotion_before_commit():
    src = Path(ub.__file__).read_text()
    i_dem = src.index('SET primary_window = FALSE')
    i_sync = src.index('sync_primary_trades(cur, strategy_id, run_id, trade_rows)')
    i_commit = src.index('conn.commit()', i_dem)
    assert i_dem < i_sync < i_commit
    assert 'trade_rows = []' in src[src.index('regime_rows = []'):i_dem]   # defined even when trades is empty
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `nice -n 19 python3 -m pytest tests/backtest/test_trades_primary_writer.py -v -W error`
Expected: FAIL (`ModuleNotFoundError: backtest.trades_primary`)

- [ ] **Step 3: Implement `src/backtest/trades_primary.py`** (interfaces above). **Then edit `unified_backtest.py`:**
  - add `trade_rows = []` immediately before `if trades:` (`:1987`);
  - immediately after the demotion `cur.execute(…)` (`:2008-2012`), add `from backtest.trades_primary import sync_primary_trades` (local import, matching the file's style at `:1915`/`:1939`) and `sync_primary_trades(cur, strategy_id, run_id, trade_rows)`.
  - It runs for both `commit=True` and `commit=False`, because a rolled-back ephemeral run rolls the cache back with it.

- [ ] **Step 4: Run tests to verify they pass**

Run: same command. Expected: 5 passed.

- [ ] **Step 5: Write the failing dry-run check.** In `scripts/tournament_dryrun.js` `scenarioWinner`, add `const hotReattributed = dbCalls.some(c => c.sql.startsWith('UPDATE strategy_backtest_trades_primary SET strategy_id') && c.params[0] === STRAT_ID && c.params[1] === 'dryrun-run-2');` and the entry `'hot table re-attributed': hotReattributed` in `checks`.

- [ ] **Step 6: Run it to verify it fails**

Run: `nice -n 19 node scripts/tournament_dryrun.js`
Expected: `[FAIL] hot table re-attributed`, `DRY RUN FAILED`, exit 1

- [ ] **Step 7: Add the mirror UPDATE** in `research-orchestrator.js` right after the base-trades UPDATE at `:2014-2015`:
  - `await this._query('UPDATE strategy_backtest_trades_primary SET strategy_id = $1 WHERE run_id = $2', [stratId, winner.btResult.run_id])`, wrapped in its own `try/catch`.
  - On error: `notify?.(\`  ⚠️ tournament: hot-table re-attribution failed (${e.message}) — run python3 -m backtest.trades_primary --resync ${stratId}\`)`. Never throw. A missing table before migration 166 lands here.

- [ ] **Step 8: Run it to verify it passes**

Run: `nice -n 19 node scripts/tournament_dryrun.js`
Expected: every check `[PASS]`, `DRY RUN OK`, exit 0

- [ ] **Step 9: Commit**

```bash
git add src/backtest/trades_primary.py src/backtest/unified_backtest.py src/agent/research/research-orchestrator.js scripts/tournament_dryrun.js tests/backtest/test_trades_primary_writer.py
```
```bash
git commit -m "feat(backtest): maintain strategy_backtest_trades_primary inside the demotion transaction + mirror tournament re-attribution (E1)"
```

---

### Task 3: Readers switched behind `OPENCLAW_BT_TRADES_HOT`

Every reader swaps ONLY the trades table name. The run-selection SQL is kept verbatim: the `JOIN strategy_backtest_runs … primary_window` in the three similarity queries, server.js's `DISTINCT ON … run_at DESC` subquery, and the `run_id = (latest primary)` lookups. That makes the result set identical even under the pre-existing double-primary case, and it hides any orphan hot row. Flag unset means the SQL string is byte-identical to today's. No log line is needed: this is not a gate, and the rows are identical.

**Files:**
- Modify: `src/backtest/trades_primary.py` (add `trades_source`)
- Create: `src/lib/bt_trades_source.js`
- Modify: `src/execution/strategy_similarity.py:262,296,950`
- Modify: `src/channels/api/server.js:1887-1893`
- Modify: `src/backtest/backtest_panel.py:251,327`
- Modify: `src/execution/backtest_coupled_recs.py:255`
- Modify: `scripts/run_universe_shrink.py:191-193`
- Test: `tests/backtest/test_trades_hot_readers.py`, `tests/lib/bt_trades_source.test.js`

**Interfaces:**
- Produces:
  - `trades_source() -> str` in `src/backtest/trades_primary.py`: `'strategy_backtest_trades_primary'` iff `os.environ.get('OPENCLAW_BT_TRADES_HOT') == '1'`, else `'strategy_backtest_trades'`. Read on every call, never cached at import.
  - `btTradesTable() -> string` in `src/lib/bt_trades_source.js`, with the same rule.
- Consumes: `HOT_TABLE` (Task 2).

- [ ] **Step 1: Write the failing tests**

Python tests use a `FakeConn`/`FakeCursor` that records SQL and returns canned rows, with `ss._db`, `psycopg2.connect` (coupled_recs) and `pd.read_parquet` (`_cross_regime_exit_share`) monkeypatched. Each reader runs twice, once with `OPENCLAW_BT_TRADES_HOT` unset and once with it set to `'1'`.

```python
LEGACY, HOT = 'strategy_backtest_trades ', 'strategy_backtest_trades_primary '
@pytest.mark.parametrize('flag,table', [(None, LEGACY), ('1', HOT)])
def test_similarity_returns_reader(monkeypatch, flag, table):     # ss._returns_by_regime_backtest()
    sql = run_and_capture(monkeypatch, flag, ss._returns_by_regime_backtest)
    assert f'FROM {table}t' in sql and 'JOIN strategy_backtest_runs r ON r.run_id = t.run_id AND r.primary_window' in sql
# same parametrised shape for: ss._cofiring_sets_by_regime_backtest, ss._cross_regime_exit_share,
# backtest_panel._trade_tickers (asserts the 'WHERE strategy_id=%s AND primary_window=TRUE' subquery survives),
# backtest_panel.build_panel (first fetchone returns {'run_id':'r1','total_sharpe':1.0,'avg_holding_days':5}, trades []),
# backtest_coupled_recs._stored_baseline (fetchone ('r1', 1.0, date(2026,1,2), '{}') then (0.02, 0.04))
def test_legacy_sql_is_byte_identical_when_flag_unset(monkeypatch):
    monkeypatch.delenv('OPENCLAW_BT_TRADES_HOT', raising=False)
    assert run_and_capture(monkeypatch, None, ss._returns_by_regime_backtest) == LEGACY_RETURNS_SQL  # copied from main @4c4f921c
def test_universe_shrink_reads_via_trades_source():
    src = (ROOT / 'scripts/run_universe_shrink.py').read_text()
    assert 'FROM {trades_source()} WHERE run_id=%s' in src and 'FROM strategy_backtest_trades WHERE' not in src
def test_reader_imports_follow_path_bootstrap():
    for rel, anchor in (('src/execution/strategy_similarity.py', 'from execution.orthogonalization import'),
                        ('src/backtest/backtest_panel.py', "sys.path.insert(0, str(ROOT / 'src'))"),
                        ('scripts/run_universe_shrink.py', "sys.path.insert(0, str(ROOT / 'src'))")):
        src = (ROOT / rel).read_text()
        assert src.index(anchor) < src.index('from backtest.trades_primary import trades_source'), rel
    import inspect, execution.backtest_coupled_recs as bcr
    assert 'from backtest.trades_primary import trades_source' in inspect.getsource(bcr._stored_baseline)
    assert 'from backtest.trades_primary' not in Path(bcr.__file__).read_text().split('def _stored_baseline')[0]
def test_server_bw_query_uses_bt_trades_table():
    src = (ROOT / 'src/channels/api/server.js').read_text()
    blk = src[src.index('Best/worst realized trade %'):src.index('const bwById')]
    assert 'FROM ${btTradesTable()} t' in blk and 'DISTINCT ON (strategy_id) strategy_id, run_id' in blk
```

```js
// tests/lib/bt_trades_source.test.js
test('btTradesTable default legacy, =1 hot, other values legacy', () => { /* delete env → 'strategy_backtest_trades'; '1' → '_primary'; '0'/'true' → legacy */ });
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `nice -n 19 python3 -m pytest tests/backtest/test_trades_hot_readers.py -v -W error` and `nice -n 19 node --test tests/lib/bt_trades_source.test.js`
Expected: FAIL (`ImportError: cannot import name 'trades_source'`; `Cannot find module '…/bt_trades_source.js'`)

- [ ] **Step 3: Implement.**
  - Add `trades_source()` and `bt_trades_source.js`.
  - In each reader, replace the literal `strategy_backtest_trades` in the FROM clause with an f-string / template interpolation of `trades_source()` / `btTradesTable()`.
  - Keep whitespace so the flag-unset string equals main's.
  - Import placement, so the import can never run before `src` is on `sys.path` (verified launchers in brackets):
    - `strategy_similarity.py`: top-level, directly under the existing `from execution.orthogonalization import _dir_to_int` (`:17`). That line already proves `src` is importable [`-m execution.strategy_similarity` from `weekly_live_sharpe.js:95`, `run_maintenance.js:1047`, `saturday_brain_finisher.js:408`].
    - `backtest_panel.py`: top-level, after the `sys.path.insert` at `:32`, next to `:34-36` [`-m backtest.backtest_panel`, in-process from `unified_backtest.py:2032`].
    - `run_universe_shrink.py`: after the bootstrap at `:41-42`, next to `:44-48` [`python3 scripts/run_universe_shrink.py`, from `staging_approver.js:588` and `fleet_overnight_resume.sh:143`].
    - `backtest_coupled_recs.py`: has no `sys.path` bootstrap (`-m execution.backtest_coupled_recs` from `weekend_saturday.sh:36`), so use a LOCAL import inside `_stored_baseline`, beside its existing local `import json`/`import psycopg2`. Its query string becomes an f-string.

- [ ] **Step 4: Run tests to verify they pass**

Run: same two commands. Expected: all passed. Then `nice -n 19 python3 -m pytest tests/execution/test_similarity_backtest_source.py tests/backtest/test_backtest_panel.py tests/execution/test_backtest_coupled_recs.py -v -W error` → unchanged pass count.

- [ ] **Step 5: Commit**

```bash
git add src/backtest/trades_primary.py src/lib/bt_trades_source.js src/execution/strategy_similarity.py src/channels/api/server.js src/backtest/backtest_panel.py src/execution/backtest_coupled_recs.py scripts/run_universe_shrink.py tests/backtest/test_trades_hot_readers.py tests/lib/bt_trades_source.test.js
```
```bash
git commit -m "feat(readers): six backtest-trades readers read strategy_backtest_trades_primary behind OPENCLAW_BT_TRADES_HOT=1 (E1)"
```

---

### Task 4: `backtest_trades_primary_sync` system check + repair CLI

**Files:**
- Create: `src/system_checks/checks/backtest_trades_primary.py`
- Modify: `src/system_checks/checks/__init__.py` (one import line)
- Modify: `src/backtest/trades_primary.py` (add `main(argv) -> int` CLI)
- Modify: `src/system_checks/README.md` (add a `backtest_trades_primary.py` line to the architecture tree)
- Test: `tests/system_checks/test_backtest_trades_primary_sync.py`

**Interfaces:**
- Produces:
  - `evaluate(*, table_exists: bool, count_mismatches: list[tuple[str, str, int, int]], orphan_runs: int, id_mismatches: int, n_primary_runs: int) -> tuple[Status, str]`. `count_mismatches` items are `(run_id, strategy_id, base_n, hot_n)`.
  - `@check(name='backtest_trades_primary_sync', tags=['storage'], requires=['db'])`: connect with `connect_timeout=5`, `SET statement_timeout='60s'`, run the three queries below, then call `evaluate`.
  - CLI `python3 -m backtest.trades_primary --resync <strategy_id>` / `--resync-drifted`. Each strategy is resynced in its own transaction via `resync_strategy`, which commits per strategy. The CLI prints `[bt_trades_primary] resync strategy=<sid> inserted=<n>` and returns 0. `--resync-drifted` takes its strategy list from the check's three queries.
- The three queries (one round-trip each):
  1. Count per primary run, base vs hot: primary runs `p` LEFT JOIN base counts (restricted to `p`'s run_ids) LEFT JOIN hot counts, WHERE `coalesce(base,0) <> coalesce(hot,0)`.
  2. Orphans: `count(DISTINCT h.run_id)` of hot runs whose run row is missing or `NOT primary_window`.
  3. Id mismatches: hot `(run_id, strategy_id)` pairs whose `strategy_id <> runs.strategy_id`.
- Evaluate rules:
  - Table absent → `WARN 'strategy_backtest_trades_primary absent — migration 166 not applied'`.
  - Any of the three non-zero → `FAIL`. The detail names the counts, up to 3 `strategy_id`s, and `repair: python3 -m backtest.trades_primary --resync-drifted`, in under 200 chars.
  - Otherwise `PASS f'{n_primary_runs} primary runs in sync'`.

- [ ] **Step 1: Write the failing tests** (pure `evaluate`, no DB)

```python
def test_pass_when_in_sync():
    st, d = chk.evaluate(table_exists=True, count_mismatches=[], orphan_runs=0, id_mismatches=0, n_primary_runs=304)
    assert st is Status.PASS and '304 primary runs in sync' in d
def test_warn_when_table_absent():
    st, d = chk.evaluate(table_exists=False, count_mismatches=[], orphan_runs=0, id_mismatches=0, n_primary_runs=0)
    assert st is Status.WARN and 'migration 166' in d
def test_count_mismatch_fails_and_names_strategy():
    st, d = chk.evaluate(table_exists=True, count_mismatches=[('r1', 'S_ZZTEST1', 120, 0)],
                         orphan_runs=0, id_mismatches=0, n_primary_runs=304)
    assert st is Status.FAIL and 'S_ZZTEST1' in d and '--resync-drifted' in d and len(d) < 200
def test_orphan_rows_fail():
    assert chk.evaluate(table_exists=True, count_mismatches=[], orphan_runs=2, id_mismatches=0, n_primary_runs=304)[0] is Status.FAIL
def test_strategy_id_mismatch_fails():
    assert chk.evaluate(table_exists=True, count_mismatches=[], orphan_runs=0, id_mismatches=1, n_primary_runs=304)[0] is Status.FAIL
def test_check_is_registered():
    import src.system_checks.checks  # noqa: F401  (side-effect registration)
    from src.system_checks.registry import all_checks
    assert 'backtest_trades_primary_sync' in all_checks()
def test_cli_resync_commits_per_strategy(monkeypatch):   # psycopg2.connect → FakeConn; asserts resync_strategy called per sid and commit() count == 2
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `nice -n 19 python3 -m pytest tests/system_checks/test_backtest_trades_primary_sync.py -v -W error`
Expected: FAIL (`ModuleNotFoundError … backtest_trades_primary`)

- [ ] **Step 3: Implement the check module, the `__init__.py` import, the CLI, and the README tree line.**

- [ ] **Step 4: Run tests to verify they pass**

Run: same command. Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/system_checks/checks/backtest_trades_primary.py src/system_checks/checks/__init__.py src/system_checks/README.md src/backtest/trades_primary.py tests/system_checks/test_backtest_trades_primary_sync.py
```
```bash
git commit -m "feat(checks): backtest_trades_primary_sync (count/orphan/id parity) + --resync CLI (E1)"
```

---

### Task 5: Migration 167 + `src/lib/job_stat.py`

**Files:**
- Create: `src/database/migrations/167_scheduled_job_runs.sql`
- Create: `src/lib/job_stat.py`
- Test: `tests/database/test_migration_167_job_runs.py`, `tests/lib/test_job_stat.py`

**Interfaces:**
- Produces, table `scheduled_job_runs`:
  - Columns: `id BIGSERIAL PRIMARY KEY`, `job_name TEXT NOT NULL`, `source TEXT NOT NULL CHECK (source IN ('systemd','cron'))`, `started_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`, `finished_at TIMESTAMPTZ`, `success BOOLEAN`, `rc INT`, `host TEXT`, `pid INT`, `invocation_id TEXT`, `note TEXT`.
  - Indexes: `idx_scheduled_job_runs_job (job_name, started_at DESC)`, and partial `idx_scheduled_job_runs_open (job_name, invocation_id) WHERE finished_at IS NULL`.
  - `invocation_id` is an ADDITION to the spec's column list: it is the only key that pairs a systemd start with its finish (see Spec defects). The shape mirrors `pipeline_runs` (`002_pipeline.sql:120-130`): BIGSERIAL id, TIMESTAMPTZ.
- Produces, `src/lib/job_stat.py` (stdlib + lazy `psycopg2`). Every public function NEVER raises. On failure it prints `[job_stat] <job> <start|finish> write failed: <ExcClass>: <msg>` and returns `None`/`False`. All are no-ops returning `None`/`False` with a `[job_stat] disabled` line when `OPENCLAW_JOB_STAT == '0'`. Connections use `POSTGRES_URI`, `connect_timeout=5`, `SET statement_timeout='5s'`, autocommit. If the URI is unset, print `[job_stat] skipped: POSTGRES_URI unset`.
  - `record_start(job: str, *, source: str = 'cron', invocation_id: str | None = None, pid: int | None = None, note: str | None = None) -> int | None`: INSERT … RETURNING id, with `host=socket.gethostname()`.
  - `record_finish(job: str, rc: int | None, *, source: str = 'cron', success: bool | None = None, invocation_id: str | None = None, note: str | None = None) -> bool`:
    - `success` defaults to `rc == 0`.
    - It UPDATEs the newest open row matching `(job_name, source, invocation_id IS NOT DISTINCT FROM %s)`.
    - If none matches, it INSERTs a complete row with `started_at=finished_at=NOW()` and `note='no-start-row; ' + (note or '')`.
  - `systemd_outcome(env: Mapping[str, str]) -> tuple[int | None, bool, str]`:
    - `rc = int(EXIT_STATUS)` iff `EXIT_CODE == 'exited'` and EXIT_STATUS is numeric, else `None`.
    - `success = SERVICE_RESULT == 'success'`.
    - `note = f'result={SERVICE_RESULT} exit_code={EXIT_CODE} exit_status={EXIT_STATUS}'`.
  - `main(argv: list[str] | None = None) -> int`:
    - `job_stat.py start <unit>` records a start with `source='systemd'`, `invocation_id=$INVOCATION_ID`, `pid=None`.
    - `job_stat.py finish <unit>` reads `systemd_outcome(os.environ)` and records the finish.
    - It ALWAYS returns 0, so the hook can never fail or delay the unit beyond the 5 s timeouts.

- [ ] **Step 1: Write the failing tests**

```python
# tests/database/test_migration_167_job_runs.py — static DDL shape
def test_167_columns_and_source_check(): ...   # every column above declared; "CHECK (source IN ('systemd','cron'))"
def test_167_idempotent_and_indexed(): ...     # no CREATE without IF NOT EXISTS; both index names present
# tests/lib/test_job_stat.py — psycopg2.connect monkeypatched to FakeConn recording SQL
def test_systemd_outcome_success():
    assert js.systemd_outcome({'SERVICE_RESULT': 'success', 'EXIT_CODE': 'exited', 'EXIT_STATUS': '0'}) == (0, True, 'result=success exit_code=exited exit_status=0')
def test_systemd_outcome_exit_code_nonzero():
    rc, ok, _ = js.systemd_outcome({'SERVICE_RESULT': 'exit-code', 'EXIT_CODE': 'exited', 'EXIT_STATUS': '2'}); assert (rc, ok) == (2, False)
def test_systemd_outcome_oom_kill():
    rc, ok, note = js.systemd_outcome({'SERVICE_RESULT': 'oom-kill', 'EXIT_CODE': 'killed', 'EXIT_STATUS': 'KILL'})
    assert rc is None and ok is False and 'result=oom-kill' in note
def test_finish_without_start_inserts_complete_row(fake_db):
    fake_db.update_returns = []              # UPDATE … RETURNING id matched nothing
    assert js.record_finish('openclaw-zztest.service', 1, source='systemd', invocation_id='abc') is True
    assert fake_db.last_insert_note.startswith('no-start-row; ')
def test_db_failure_never_raises(monkeypatch, capsys):
    monkeypatch.setattr(js, '_connect', lambda: (_ for _ in ()).throw(OSError('down')))
    assert js.record_start('zz-job') is None and '[job_stat] zz-job start write failed: OSError' in capsys.readouterr().out
def test_kill_switch(monkeypatch, fake_db):
    monkeypatch.setenv('OPENCLAW_JOB_STAT', '0'); assert js.record_start('zz-job') is None and fake_db.calls == []
def test_main_always_returns_zero(monkeypatch):
    monkeypatch.setattr(js, '_connect', lambda: (_ for _ in ()).throw(OSError('down')))
    assert js.main(['finish', 'openclaw-zztest.service']) == 0 and js.main(['bogus']) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `nice -n 19 python3 -m pytest tests/database/test_migration_167_job_runs.py tests/lib/test_job_stat.py -v -W error`
Expected: FAIL (missing migration file; `ModuleNotFoundError: lib.job_stat`)

- [ ] **Step 3: Write the migration and implement `src/lib/job_stat.py`** (`if __name__ == '__main__': sys.exit(main())`).

- [ ] **Step 4: Run tests to verify they pass**

Run: same command. Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add src/database/migrations/167_scheduled_job_runs.sql src/lib/job_stat.py tests/database/test_migration_167_job_runs.py tests/lib/test_job_stat.py
```
```bash
git commit -m "feat(ops): migration 167 scheduled_job_runs + src/lib/job_stat.py (systemd hook CLI, never raises) (E2)"
```

---

### Task 6: `src/lib/job_stat.js` + one `_sched()` wrapper for the 26 cron entries

What a cron row means: the wrapper times the **callback**. For the callbacks that `spawn(..., {detached: true}).unref()` (e.g. `:414`, `:708`, `:777`), finishing means the job was dispatched, not that the child exited. For `runPython`/`execSync` and `async` callbacks (e.g. `:362`, `:655`, `:760`, `:862`), it is the real completion. This is stated in `job_stat.js`'s header. Tracking each child's `exit` would edit 26 bodies, which is not "one wrapper". The overdue check therefore detects "cron never fired" (johnbot dead, schedule unregistered), which is what the 26 entries lack today.

**Files:**
- Create: `src/lib/job_stat.js`
- Modify: `src/engine/cron-schedule.js` (add `_sched` after `log()` at `:104-110`; rewrite the 26 `cron.schedule(` call sites, nothing else)
- Test: `tests/lib/job_stat.test.js`, `tests/engine/test_cron_job_stat_wrapper.test.js`

**Interfaces:**
- Produces, in `src/lib/job_stat.js`, the JS twin of `job_stat.py`: same table, same `[job_stat]` prefix, same kill switch. It never throws.
  - `enabled() -> boolean`
  - `recordStart(pool, job, { source = 'cron', pid = process.pid, host = os.hostname(), note = null, startedAt = new Date() } = {}) -> Promise<number|null>`: `started_at` is passed as a PARAMETER, never `DEFAULT NOW()`. A `runPython`/`execSync` callback (`:362`, `:655`, `:760`) blocks the event loop for minutes, and node-postgres only writes the INSERT to the socket after the callback returns, so the DB clock would stamp the finish time.
  - `recordFinish(pool, { id, job, rc, success, note, source = 'cron', finishedAt = new Date() }) -> Promise<boolean>`: UPDATE by `id` when `id != null`, else INSERT a complete row with the `no-start-row; ` note prefix (`started_at = finished_at = finishedAt`).
  - Every query is issued as `{ text, values, query_timeout: 5000 }`, matching the Python twin's 5 s. A hung DB therefore cannot pin connections in the shared module pool at `cron-schedule.js:21`, which `weekly-maintenance-sun0800` also uses.
  - `runTracked(pool, job, fn) -> ReturnType<fn>`:
    - Fires `recordStart` without awaiting it, then calls `fn()` synchronously.
    - If `fn` returns a non-promise: finish with `rc=0, success=true, note='callback-returned'`.
    - If `fn` throws: finish with `rc=1, success=false, note='threw: '+msg.slice(0,200)`, then rethrow the same error.
    - If `fn` returns a promise: it returns that same promise. On resolve it records `rc=0, note='callback-resolved'`; on reject it records `rc=1`, and the rejection still propagates.
    - Every finish is chained on the start promise (`startP.then(id => recordFinish(...))`), so a slow DB only delays the bookkeeping.
- Produces, in `cron-schedule.js`: `function _sched(job, expr, fn, opts) { return cron.schedule(expr, (...a) => jobStat.runTracked(pool, job, () => fn(...a)), opts); }`. It uses the existing module pool at `:21`. After this task, `cron.schedule(` appears exactly once in the file, inside `_sched`. Every existing `if (...)` registration guard stays as it is.
- Job names (unique, kebab-case). Task 7's `CRON_POLICIES` keys must equal this set:

| line | name | line | name |
|---|---|---|---|
| 256 | `token-budget-reset` | 558 | `tier1-acting-ingest-1430` |
| 292 | `legacy-daily-cycle-1000` | 582 | `sameday-compute-1500` |
| 352 | `closeexec-compute-1510` | 595 | `sameday-execute-1555` |
| 357 | `closeexec-execute-1555` | 611 | `sameday-eod-collect-1615` |
| 362 | `closeexec-sod-refresh-0935` | 624 | `sameday-evening-marks-1620` |
| 398 | `eod-signal-register-1615` | 645 | `sameday-sod-refresh-0935` |
| 414 | `premarket-news-0905` | 655 | `morning-regime-0900` |
| 444 | `premarket-gate-0915` | 708 | `intraday-hmm-tick` |
| 478 | `open-reconcile-0925` | 733 | `crypto-regime-tick` |
| 503 | `opg-sweep-0932` | 760 | `intraday-hmm-refit-1800` |
| 533 | `eod-into-close-fill-1555` | 777 | `options-aggregates-refresh-0605` |
| 801 | `cadence-recompute-2355` | 825 | `circuit-breaker-tick` |
| 862 | `weekly-maintenance-sun0800` | 934 | `curator-priors-snapshot-0305` |

- [ ] **Step 1: Write the failing tests**

```js
// tests/lib/job_stat.test.js — FakePool whose query() returns a controllable promise and records (sql, params)
test('runTracked calls fn before the start write resolves', () => { /* pool.query never resolves; fn sets flag; assert flag true synchronously after runTracked */ });
test('started_at is captured before a synchronously blocking fn returns', () => { /* fn busy-waits 50 ms; the INSERT's started_at param (Date) <= the time recorded at fn entry; finished_at param >= fn exit */ });
test('every query carries query_timeout 5000', () => { /* FakePool records config objects */ });
test('runTracked rethrows the original error', () => { /* fn throws e; assert.throws(() => runTracked(pool,'zz',fn), e) ; after microtasks an UPDATE/INSERT with rc=1, success=false was issued */ });
test('runTracked returns the same promise and records resolve/reject', async () => { ... });
test('a failing pool never throws and logs [job_stat]', async () => { /* query rejects → no throw; console.log captured contains "[job_stat] zz start write failed" */ });
test('OPENCLAW_JOB_STAT=0 issues no queries', () => { ... });
// tests/engine/test_cron_job_stat_wrapper.test.js — mock node-cron (as test_sameday_exec_crons.test.js) AND 'pg' (FakePool)
test('cron.schedule appears exactly once (inside _sched)', () => { /* count in file text === 1 */ });
test('26 _sched names, all unique, equal the documented set', () => { /* regex /_sched\('([a-z0-9-]+)'/g over file text */ });
test('a captured callback writes a cron start row named for its job', async () => { /* sameday ON; invoke callback for '35 9 * * 1-5' with child_process mocked; first INSERT params[0] === 'sameday-sod-refresh-0935', params[1] === 'cron' */ });
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `nice -n 19 node --test tests/lib/job_stat.test.js tests/engine/test_cron_job_stat_wrapper.test.js`
Expected: FAIL (`Cannot find module '…/job_stat.js'`; `cron.schedule` count 26)

- [ ] **Step 3: Implement `src/lib/job_stat.js`, add `_sched`, and rewrite the 26 call sites** (`cron.schedule(EXPR, FN, OPTS)` → `_sched('<name>', EXPR, FN, OPTS)`).

- [ ] **Step 4: Run tests to verify they pass, plus the existing cron tests**

Run: `nice -n 19 node --test tests/lib/job_stat.test.js tests/engine/test_cron_job_stat_wrapper.test.js tests/engine/test_sameday_exec_crons.test.js tests/engine/test_close_exec_crons.test.js tests/engine/test_daily_health_digest_exit_hook.test.js`
Expected: all pass. The existing tests capture the same `expr`s.

- [ ] **Step 5: Commit**

```bash
git add src/lib/job_stat.js src/engine/cron-schedule.js tests/lib/job_stat.test.js tests/engine/test_cron_job_stat_wrapper.test.js
```
```bash
git commit -m "feat(ops): job_stat.js + single _sched wrapper records all 26 in-process cron entries (E2)"
```

---

### Task 7: systemd drop-in snapshot + `scheduled_jobs_overdue` check

**Files:**
- Create: `docs/systemd/job-stat/job-stat.conf`
- Modify: `docs/systemd/README.md` (new section "Job-stat drop-in (not installed)")
- Create: `src/system_checks/checks/scheduled_jobs.py`
- Modify: `src/system_checks/checks/__init__.py` (one import line)
- Modify: `src/system_checks/README.md` (add a `scheduled_jobs.py` line to the architecture tree)
- Test: `tests/system_checks/test_scheduled_jobs_overdue.py`

**Interfaces:**
- `job-stat.conf`, exactly:
  ```ini
  # Job-stat hooks (spec 2026-10-19 §5 E2). NOT installed by install_systemd.sh — see docs/systemd/README.md.
  [Service]
  ExecStartPre=-/usr/bin/python3 /root/openclaw/src/lib/job_stat.py start %n
  ExecStopPost=-/usr/bin/python3 /root/openclaw/src/lib/job_stat.py finish %n
  ```
  - It uses `ExecStartPre`, not the spec's `ExecStartPost`. The timer services are `Type=oneshot` (e.g. `docs/systemd/openclaw-eod-refresh.service`), and for oneshot units `ExecStartPost` runs after the main process exits.
  - `-` means a hook failure never fails the unit.
  - `%n` keeps template instances distinct (`openclaw-edgar-8k@0715.service`).
  - `ExecStopPost` reads `$SERVICE_RESULT`/`$EXIT_CODE`/`$EXIT_STATUS`/`$INVOCATION_ID` from its environment (Task 5).
- README section:
  - why the drop-in lives outside `*.service.d/`, which `install_systemd.sh:40-46` installs verbatim;
  - the operator install loop: for each `docs/systemd/*.timer`, the target is `Unit=` or the same stem `.service`; template instances use `name@.service.d`. Copy to `/etc/systemd/system/<target>.d/job-stat.conf`, then `daemon-reload`;
  - that units without `EnvironmentFile=/root/openclaw/.env` log `[job_stat] skipped: POSTGRES_URI unset`.
- Check module `src/system_checks/checks/scheduled_jobs.py`:
  - `Policy = tuple[str, float]`, with kinds `'session'` (once per NYSE session; WARN when ≥1 full session with no success lies strictly between `last_success.date()` and `now_et.date()`), `'hours'` (WARN when `now − last_success > 2×h`), `'intraday_min'` (judged only while `lib.trading_calendar.is_open(now_et)` and the session has been open ≥ 2×m min; WARN when `now − last_success > 2×m` min).
  - `CRON_POLICIES: dict[str, Policy]`. Keys are exactly Task 6's 26 names:
    - `('hours', 24)`: `token-budget-reset`, `intraday-hmm-refit-1800`, `cadence-recompute-2355`, `curator-priors-snapshot-0305`.
    - `('hours', 1)`: `crypto-regime-tick`.
    - `('hours', 168)`: `weekly-maintenance-sun0800`.
    - `('intraday_min', 15)`: `intraday-hmm-tick`.
    - `('intraday_min', 5)`: `circuit-breaker-tick`.
    - Every other name: `('session', 1)`.
  - `policy_from_oncalendar(lines: list[str]) -> Policy | None`:
    - `Mon..Fri` → `('session', 1)`; a single weekday (`Sat`/`Sun`) → `('hours', 168)`; no weekday or `*-*-*` daily → `('hours', 24)`.
    - Anything else (e.g. `Tue..Fri`), or several `OnCalendar=` lines of different kinds → `None`, meaning unpolicied: listed in the detail, never WARN.
  - `systemd_policies(systemd_dir: Path) -> dict[str, Policy]`: keyed by the service unit name as `%n` renders it (`Unit=` value, else `<timer stem>.service`).
  - `evaluate(rows: list[dict], policies: dict[str, Policy], *, now: datetime) -> tuple[Status, str]`. `rows` holds one dict per job seen in the last 30 days: `{job_name, source, last_success_at: datetime|None, n_failures_since_success: int, last_note: str|None}`.
    - A policied job is overdue, or has failures but no success in the window → WARN listing up to 4 job names.
    - Empty `rows` → `WARN 'no scheduled_job_runs rows in 30d — job_stat wiring inactive'`.
    - Otherwise PASS `f'{n_ok} on schedule, {n_unpolicied} unpolicied, {len(rows)} seen in 30d'`.
    - Jobs never seen in 30 days are not judged: mode-gated crons that are never registered must not be wallpaper.
  - `@check(name='scheduled_jobs_overdue', tags=['maintenance'], requires=['db'])`:
    - If the table is absent (`to_regclass`) → `WARN 'migration 167 not applied'`.
    - Otherwise, one GROUP BY query over `scheduled_job_runs WHERE started_at >= NOW() - interval '30 days'`; policies = `CRON_POLICIES | systemd_policies(ROOT/'docs/systemd')`; then `evaluate`.

- [ ] **Step 1: Write the failing tests** (pure functions; `now` values are tz-aware UTC; `lib.trading_calendar` is used as-is because it reads only the small calendar master or its fallback)

```python
MON_1000_ET = datetime(2026, 10, 19, 14, 0, tzinfo=timezone.utc); FRI_1600_ET = datetime(2026, 10, 16, 20, 0, tzinfo=timezone.utc)
def _row(job, last_ok, fails=0): return {'job_name': job, 'source': 'cron', 'last_success_at': last_ok, 'n_failures_since_success': fails, 'last_note': None}
def test_session_job_monday_after_friday_passes():
    assert chk.evaluate([_row('sameday-compute-1500', FRI_1600_ET)], chk.CRON_POLICIES, now=MON_1000_ET)[0] is Status.PASS
def test_session_job_missed_monday_warns_tuesday():
    st, d = chk.evaluate([_row('sameday-compute-1500', FRI_1600_ET)], chk.CRON_POLICIES, now=MON_1000_ET + timedelta(days=1))
    assert st is Status.WARN and 'sameday-compute-1500' in d
def test_holiday_not_counted_as_missed():   # Thu 2026-11-26 Thanksgiving: Wed success, checked Fri 10:00 ET → PASS
    ...
def test_hours_policy_overdue_after_2x():
    assert chk.evaluate([_row('crypto-regime-tick', MON_1000_ET - timedelta(hours=2, minutes=1))], chk.CRON_POLICIES, now=MON_1000_ET)[0] is Status.WARN
def test_intraday_not_judged_outside_session():   # circuit-breaker last ok Fri 16:00 ET, checked Sat → PASS
    ...
def test_failures_without_success_warn():
    assert chk.evaluate([_row('premarket-gate-0915', None, fails=3)], chk.CRON_POLICIES, now=MON_1000_ET)[0] is Status.WARN
def test_unseen_and_unpolicied_jobs_never_warn():
    st, d = chk.evaluate([_row('openclaw-rf-flip.service', None, fails=0)], {}, now=MON_1000_ET)
    assert st is Status.PASS and '1 unpolicied' in d
def test_no_rows_warns():
    assert chk.evaluate([], chk.CRON_POLICIES, now=MON_1000_ET)[0] is Status.WARN
def test_policy_from_oncalendar():
    assert chk.policy_from_oncalendar(['Mon..Fri *-*-* 16:15:00 America/New_York']) == ('session', 1)
    assert chk.policy_from_oncalendar(['Sat *-*-* 06:00:00 UTC']) == ('hours', 168)
    assert chk.policy_from_oncalendar(['Tue..Fri *-*-* 21:45:00 UTC']) is None
def test_systemd_policies_keys_template_instance(tmp_path):   # timer with Unit=openclaw-zz@0715.service → key 'openclaw-zz@0715.service'
    ...
def test_cron_policy_keys_equal_cron_schedule_names():
    names = set(re.findall(r"_sched\('([a-z0-9-]+)'", (ROOT / 'src/engine/cron-schedule.js').read_text()))
    assert names == set(chk.CRON_POLICIES) and len(names) == 26
def test_installer_never_installs_job_stat_dropin():
    sh = (ROOT / 'scripts/install_systemd.sh').read_text()
    assert 'job-stat' not in sh and '"$SRC"/*.service.d' in sh            # globs only *.service.d / weekend-swap / user
    assert (ROOT / 'docs/systemd/job-stat/job-stat.conf').exists()
    assert not list((ROOT / 'docs/systemd').glob('*.service.d/job-stat.conf'))
def test_dropin_uses_startpre_and_stoppost_with_dash():
    conf = (ROOT / 'docs/systemd/job-stat/job-stat.conf').read_text()
    assert 'ExecStartPre=-/usr/bin/python3 /root/openclaw/src/lib/job_stat.py start %n' in conf
    assert 'ExecStopPost=-/usr/bin/python3 /root/openclaw/src/lib/job_stat.py finish %n' in conf
    assert 'ExecStartPost' not in conf
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `nice -n 19 python3 -m pytest tests/system_checks/test_scheduled_jobs_overdue.py -v -W error`
Expected: FAIL (`ModuleNotFoundError … scheduled_jobs`)

- [ ] **Step 3: Create the drop-in, the README section, the check module, and the `__init__.py` import.**

- [ ] **Step 4: Run tests to verify they pass**

Run: same command. Expected: 13 passed.

- [ ] **Step 5: Commit**

```bash
git add docs/systemd/job-stat/job-stat.conf docs/systemd/README.md src/system_checks/README.md src/system_checks/checks/scheduled_jobs.py src/system_checks/checks/__init__.py tests/system_checks/test_scheduled_jobs_overdue.py
```
```bash
git commit -m "feat(ops): job-stat systemd drop-in snapshot (uninstalled) + scheduled_jobs_overdue check (E2)"
```

---

## Runbook (controller, after the Stream E merge — never an implementer)

1. **Pre-apply 166 off-hours, BEFORE the first johnbot restart after the merge.** `bot.js` runs `await migrate()` (`:1663`) before `cronSchedule.start()` (`:1763-1765`). A restart with 166 unapplied therefore registers no cron (including the 15:00/15:55 legs) until the 2–6 min backfill finishes. Do this outside 18:45Z–20:45Z on a trading day, with no fleet unit active (R1). Run it as a transient unit:
   - First check whether `psql` exists on the host (`command -v psql`). Postgres runs in the `openclaw-postgres` container.
   - If it does not, use `systemd-run --unit=openclaw-mig166 --wait /bin/sh -c 'docker exec -i openclaw-postgres psql -U <user> -d <db> -1 -v ON_ERROR_STOP=1 < /root/openclaw/src/database/migrations/166_backtest_trades_primary.sql'`. Take the user and db from `POSTGRES_URI`, and write them literally into the unit command, because a transient unit does not inherit the caller's shell environment.
   - `-1` makes it one transaction, matching the johnbot runner.
   - Expect 2–6 min and a `[166] backfilled ≈5.1M rows from ≈304 primary runs` NOTICE.
   - Backtest persists wait on the SHARE lock meanwhile.
   - This keeps johnbot's startup `migrate()` from blocking for minutes. The next restart sees the `backfilled:166` marker and skips.
   - Apply 167 the same way (instant).
2. `python3 -m system_checks --check backtest_trades_primary_sync` → PASS.
3. Add `OPENCLAW_BT_TRADES_HOT=1` to `.env`, then restart user-scope johnbot (`XDG_RUNTIME_DIR=/run/user/0 systemctl --user restart johnbot`).
   - The next similarity rebuild (`weekly_live_sharpe.js:95`, Saturday maintenance, or the finisher) must produce a matrix identical to the previous one for unchanged strategies.
   - Rollback: unset the flag.
4. After the next nightly backtest, the sync check stays PASS and the persist log shows `[bt_trades_primary] strategy=… deleted=… inserted=…`.
5. After the johnbot restart, `SELECT job_name, count(*) FROM scheduled_job_runs GROUP BY 1` shows cron rows within the hour (`crypto-regime-tick`, `circuit-breaker-tick` during RTH). `scheduled_jobs_overdue` PASS.
6. Systemd drop-in install is an operator decision, per the README section. Until then only cron rows exist.

## Spec defects found (not silently reinterpreted)

1. **"Chunked by strategy to bound WAL" cannot be met inside migration 166.** `postgres.js:42-55` sends each file as one simple-query message, which is one implicit transaction. A COMMIT between chunks is impossible: `CALL`/`COMMIT` inside it raises `invalid transaction termination`. Per-run chunking still avoids seq-scanning the 5 GB heap, and an open transaction does not retain WAL (checkpoints recycle it), so disk stays bounded by `max_wal_size`. The cost that remains is a single 2–6 min transaction, so the Runbook pre-applies it off-hours.
2. **`ExecStartPost` cannot record a start for `Type=oneshot` timer services.** It runs after ExecStart exits. The plan uses `ExecStartPre=-` + `ExecStopPost=-`.
3. **`record_finish(job, rc)` has no key to pair a systemd finish with its start.** The two hooks are separate processes, and the hook pid is meaningless. The plan adds `invocation_id TEXT` (`$INVOCATION_ID`) to `scheduled_job_runs`, beyond the spec's column list.
4. **Drop-ins "under `docs/systemd/`" would be installed by `scripts/install_systemd.sh:40-46`**, which installs every `*.service.d/*.conf`, and would break that directory's "byte-exact deployed snapshot" contract. The plan uses `docs/systemd/job-stat/`.
5. **Readers switched (no flag in the spec):** merging to main makes Python readers live before migration 166 exists. The plan gates the switch behind `OPENCLAW_BT_TRADES_HOT=1` (default = today's SQL) and keeps the writer maintaining the table whenever it exists.
6. **A seventh writer the spec omits:** `research-orchestrator.js:2012-2015` (tournament re-attribution). Task 2 mirrors it.
7. **Cron rows measure the callback, not the child.** Most of the 26 entries spawn detached, unref'd children, so a one-wrapper design records dispatch for those. Documented in Task 6.
8. **Four of the six "readers" gain no I/O.** `backtest_panel.py:251,327`, `backtest_coupled_recs.py:255` and `run_universe_shrink.py:192` already filter `run_id = %s` on the base PK. They switch for uniformity. The real gain is the three `strategy_similarity` queries and `server.js:1887-1893`, which seq-scan 31 M rows today.
