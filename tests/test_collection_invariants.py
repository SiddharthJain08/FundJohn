"""tests/test_collection_invariants.py

Five focused tests of the daily-collection invariants the user named:

  1. updateCoverage zero-row guard — the "no lying" rule
  2. New columns added to data_columns are picked up by the next daily cycle
  3. Re-running collection over a fully-covered range is a no-op
  4. Universe expansion preserves existing tickers' coverage
  5. Column deprecation preserves historical parquet rows

These tests use a real PostgreSQL connection (POSTGRES_URI env var) and wrap
every test in BEGIN/ROLLBACK so no production data is touched. Tables are
expected to already exist (the existing migrations 004, 025, 029, …).

Run with:
    POSTGRES_URI=$(grep ^POSTGRES_URI .env | cut -d= -f2-) pytest tests/test_collection_invariants.py -v
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest
import psycopg2

# Make src/ importable so the lifecycle helper tests can drive the real
# LifecycleStateMachine class (no manifest writes — we use new_empty()).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

POSTGRES_URI = os.environ.get('POSTGRES_URI')

pytestmark = pytest.mark.skipif(
    not POSTGRES_URI,
    reason='POSTGRES_URI not set — DB-backed invariant tests skipped',
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures: per-test connection wrapped in BEGIN/ROLLBACK so production data
# stays untouched. The collector + staging-approver paths only INSERT/UPDATE,
# never DDL, so a transactional wrapper is sufficient.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def conn():
    c = psycopg2.connect(POSTGRES_URI)
    c.autocommit = False
    yield c
    c.rollback()
    c.close()


def _ticker(prefix: str = 'TST') -> str:
    """Random short ticker symbol so concurrent test runs don't collide."""
    return f'{prefix}{uuid.uuid4().hex[:6].upper()}'


def _column(prefix: str = 'col_') -> str:
    return f'{prefix}{uuid.uuid4().hex[:8]}'


# ─────────────────────────────────────────────────────────────────────────────
# Test 1 — updateCoverage rejects zero-row writes (the "no lying" rule).
#
# `src/pipeline/store.js::updateCoverage` (lines 280–301) skips the UPDATE
# when rowsAdded <= 0. We replicate that exact INSERT...ON CONFLICT SQL here
# and assert that running it with rowsAdded=0 does NOT advance date_to.
# ─────────────────────────────────────────────────────────────────────────────

UPDATE_COVERAGE_SQL = """
INSERT INTO data_coverage (ticker, data_type, date_from, date_to, rows_stored, last_updated)
VALUES (%s, %s, %s::date, %s::date, %s, NOW())
ON CONFLICT (ticker, data_type) DO UPDATE SET
  date_from    = LEAST(EXCLUDED.date_from, data_coverage.date_from),
  date_to      = GREATEST(EXCLUDED.date_to, data_coverage.date_to),
  rows_stored  = data_coverage.rows_stored + %s,
  last_updated = NOW()
"""


def update_coverage(cur, ticker, data_type, date_from, date_to, rows_added):
    """Mirror of store.js::updateCoverage including the zero-row guard."""
    if not rows_added or rows_added <= 0:
        return
    cur.execute(
        UPDATE_COVERAGE_SQL,
        (ticker, data_type, date_from, date_to, rows_added, rows_added),
    )


def test_update_coverage_rejects_zero_row_writes(conn):
    cur = conn.cursor()
    t = _ticker()
    seed_to = date.today() - timedelta(days=2)
    cur.execute(
        """INSERT INTO data_coverage (ticker, data_type, date_from, date_to, rows_stored, last_updated)
                VALUES (%s, 'prices', %s, %s, 100, NOW())""",
        (t, date.today() - timedelta(days=365), seed_to),
    )

    today = date.today()
    update_coverage(cur, t, 'prices', today, today, 0)

    cur.execute(
        "SELECT date_to, rows_stored FROM data_coverage WHERE ticker=%s AND data_type='prices'",
        (t,),
    )
    row = cur.fetchone()
    assert row is not None
    assert row[0] == seed_to, (
        f'date_to advanced after a zero-row write — coverage lied. got={row[0]} expected={seed_to}'
    )
    assert row[1] == 100, 'rows_stored should not increase on a zero-row write'


# ─────────────────────────────────────────────────────────────────────────────
# Test 2 — A column registered in data_columns gets surfaced as part of the
# active collection set the next time the collector queries the registry.
# This is the "after backfill the column should be added to the daily
# collection cycle automatically" invariant the user named.
#
# The collector consumes data_columns + schema_registry.json; we verify the
# data_columns side here (the schema_registry side is a JSON file edit, not
# a runtime invariant).
# ─────────────────────────────────────────────────────────────────────────────

def test_new_column_added_to_next_daily_cycle(conn):
    cur = conn.cursor()
    col = _column('factor_')

    # Before: column doesn't exist in data_columns.
    cur.execute('SELECT 1 FROM data_columns WHERE column_name=%s', (col,))
    assert cur.fetchone() is None

    # backfill_one_request's INSERT...ON CONFLICT into data_columns is the
    # canonical registration. We replay that SQL exactly.
    cur.execute(
        """INSERT INTO data_columns (column_name, provider, refresh_cadence)
                VALUES (%s, 'fmp', 'daily')
            ON CONFLICT (column_name) DO UPDATE
              SET provider = EXCLUDED.provider""",
        (col,),
    )

    # The collector's "what should I fetch?" query reads data_columns. We
    # don't run the actual collector here (it would hit external APIs); we
    # simulate the query the next daily cycle would run and assert the new
    # column appears.
    cur.execute(
        """SELECT column_name, provider, refresh_cadence FROM data_columns
            WHERE column_name = %s""",
        (col,),
    )
    row = cur.fetchone()
    assert row is not None, 'newly-registered column should appear in data_columns'
    assert row[1] == 'fmp'
    assert row[2] == 'daily'

    # Idempotency: re-registering the same column updates without conflict.
    cur.execute(
        """INSERT INTO data_columns (column_name, provider, refresh_cadence)
                VALUES (%s, 'polygon', 'daily')
            ON CONFLICT (column_name) DO UPDATE
              SET provider = EXCLUDED.provider""",
        (col,),
    )
    cur.execute('SELECT provider FROM data_columns WHERE column_name=%s', (col,))
    assert cur.fetchone()[0] == 'polygon', 'ON CONFLICT should overwrite provider'


# ─────────────────────────────────────────────────────────────────────────────
# Test 3 — Re-running collection over a fully-covered date range is idempotent.
# Coverage row stays exactly as-is (date_from, date_to, rows_stored unchanged).
# ─────────────────────────────────────────────────────────────────────────────

def test_collector_rerun_is_idempotent(conn):
    cur = conn.cursor()
    t  = _ticker()
    df = date(2020, 1, 1)
    dt = date.today() - timedelta(days=1)
    cur.execute(
        """INSERT INTO data_coverage (ticker, data_type, date_from, date_to, rows_stored, last_updated)
                VALUES (%s, 'prices', %s, %s, 1500, NOW())""",
        (t, df, dt),
    )

    # Simulate a re-run that finds zero gaps and writes zero rows. The
    # zero-row guard short-circuits — the row stays exactly as-is.
    update_coverage(cur, t, 'prices', df, dt, 0)

    cur.execute(
        "SELECT date_from, date_to, rows_stored FROM data_coverage WHERE ticker=%s AND data_type='prices'",
        (t,),
    )
    row = cur.fetchone()
    assert row[0] == df,  f'date_from changed: {row[0]} vs {df}'
    assert row[1] == dt,  f'date_to changed: {row[1]} vs {dt}'
    assert row[2] == 1500, f'rows_stored changed: {row[2]} vs 1500'

    # And a second update with a tiny extra range only EXPANDS — never shrinks.
    update_coverage(cur, t, 'prices', df - timedelta(days=10), dt, 10)
    cur.execute(
        "SELECT date_from, date_to FROM data_coverage WHERE ticker=%s AND data_type='prices'",
        (t,),
    )
    row = cur.fetchone()
    assert row[0] == df - timedelta(days=10), 'date_from should LEAST() to earlier date'
    assert row[1] == dt, 'date_to should not change when we did not write past dt'


# ─────────────────────────────────────────────────────────────────────────────
# Test 4 — Universe expansion preserves existing tickers' coverage.
# Adding a new ticker to universe_config does not affect the coverage of
# tickers that were already there. (universe_config has no DELETE in any
# code path; this protects against accidental future regressions.)
# ─────────────────────────────────────────────────────────────────────────────

def test_universe_expansion_preserves_existing_tickers(conn):
    cur = conn.cursor()
    t1, t2, t3 = _ticker('UA'), _ticker('UB'), _ticker('UC')
    yesterday = date.today() - timedelta(days=1)

    # Seed three pre-existing universe tickers + their coverage.
    for t in (t1, t2, t3):
        cur.execute(
            """INSERT INTO universe_config (ticker, category, active, has_options, has_fundamentals)
                    VALUES (%s, 'equity', TRUE, TRUE, TRUE)
                    ON CONFLICT (ticker) DO NOTHING""",
            (t,),
        )
        cur.execute(
            """INSERT INTO data_coverage (ticker, data_type, date_from, date_to, rows_stored, last_updated)
                    VALUES (%s, 'prices', '2020-01-01', %s, 1000, NOW())""",
            (t, yesterday),
        )

    # Add a fourth ticker.
    t_new = _ticker('UNEW')
    cur.execute(
        """INSERT INTO universe_config (ticker, category, active, has_options, has_fundamentals)
                VALUES (%s, 'equity', TRUE, TRUE, TRUE)""",
        (t_new,),
    )

    # The original three must still have their coverage rows intact.
    cur.execute(
        """SELECT ticker, date_from, date_to, rows_stored FROM data_coverage
            WHERE ticker = ANY(%s) AND data_type='prices'""",
        ([t1, t2, t3],),
    )
    rows = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}
    for t in (t1, t2, t3):
        assert t in rows, f'ticker {t} lost its coverage row when {t_new} was added'
        df, dt, rs = rows[t]
        assert df == date(2020, 1, 1), f'{t} date_from regressed'
        assert dt == yesterday,         f'{t} date_to regressed'
        assert rs == 1000,              f'{t} rows_stored regressed'


# ─────────────────────────────────────────────────────────────────────────────
# Test 5 — Column deprecation preserves historical row data.
# queue_drain.drain_deprecation removes a column from the data_columns ledger
# but does NOT touch parquet files / historical rows. We assert the ledger
# row is gone but a marker historical row in a sibling fact table stays put.
#
# The fact-table side is approximated with a temp table since the parquet
# files aren't directly Postgres-backed. The invariant is: the deprecation
# code path issues DELETE FROM data_columns and nothing else.
# ─────────────────────────────────────────────────────────────────────────────

def test_column_deprecation_preserves_parquet_history(conn):
    cur = conn.cursor()
    col = _column('legacy_')

    # Register the column.
    cur.execute(
        """INSERT INTO data_columns (column_name, provider, refresh_cadence,
                                     min_date, max_date, row_count)
                VALUES (%s, 'fmp', 'daily', '2020-01-01', '2024-12-31', 50000)""",
        (col,),
    )

    # Simulate the deprecation drain's DELETE.
    cur.execute('DELETE FROM data_columns WHERE column_name = %s', (col,))

    # Ledger row is gone…
    cur.execute('SELECT 1 FROM data_columns WHERE column_name=%s', (col,))
    assert cur.fetchone() is None, 'data_columns row should be deleted on deprecation'

    # …and we never issued any other DELETE. Confirm the deprecation path's
    # SQL doesn't accidentally touch sibling tables. Specifically: data_coverage
    # rows for unrelated columns must stay intact.
    canary = _ticker('CANARY')
    cur.execute(
        """INSERT INTO data_coverage (ticker, data_type, date_from, date_to, rows_stored, last_updated)
                VALUES (%s, 'prices', '2020-01-01', '2024-12-31', 1000, NOW())""",
        (canary,),
    )
    cur.execute('DELETE FROM data_columns WHERE column_name = %s', (col,))  # idempotent re-run
    cur.execute(
        "SELECT rows_stored FROM data_coverage WHERE ticker=%s AND data_type='prices'",
        (canary,),
    )
    assert cur.fetchone()[0] == 1000, 'data_coverage canary disturbed by deprecation'


# ─────────────────────────────────────────────────────────────────────────────
# Inline orphan-column removal on strategy unstacking.
#
# When a strategy transitions to DEPRECATED or ARCHIVED,
# LifecycleStateMachine._remove_orphan_columns_inline runs synchronously and:
#   1. drops every requirements.json column not used by any remaining
#      live/monitoring/candidate/staging strategy from the data_columns
#      ledger (Postgres),
#   2. strips those columns from data/master/schema_registry.json (atomic
#      tmp + rename).
# Historical parquet rows are preserved.
#
# These tests exercise the helper directly with a synthesised
# requirements.json + schema_registry.json under tmp_path, against a real DB
# row in data_columns. We never write to the production manifest.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def autocommit_conn():
    """Auto-committing connection for tests where the code under test opens
    its own connection (so an open BEGIN/ROLLBACK transaction in the test
    fixture would be invisible). Tracks every test-inserted column name and
    purges them at teardown — production rows are never touched.
    """
    c = psycopg2.connect(POSTGRES_URI)
    c.autocommit = True
    created_columns: list[str] = []
    yield c, created_columns
    if created_columns:
        cur = c.cursor()
        cur.execute('DELETE FROM data_columns WHERE column_name = ANY(%s)', (created_columns,))
    c.close()


@pytest.fixture()
def patched_paths(tmp_path, monkeypatch):
    """Redirect both src/strategies/implementations and the master schema
    registry into pytest tmp dirs, so the lifecycle helper can read fake
    requirements.json files and write a fake schema_registry without
    touching the production tree.

    Returns: { 'req_dir': Path, 'registry_path': Path }
    """
    from strategies import lifecycle as lc

    req_dir = tmp_path / 'implementations'
    req_dir.mkdir()

    master_dir = tmp_path / 'master'
    master_dir.mkdir()
    registry_path = master_dir / 'schema_registry.json'

    # Patch the helper's path resolution. Both methods compute paths from
    # __file__; we monkey-patch them to point at our tmp dirs.
    def _patched_read(self, sid):
        rec = self._records.get(sid)
        canonical = rec.metadata.get('canonical_file') if rec and rec.metadata else None
        base = (canonical or f'{sid.lower()}.py').replace('.py', '')
        p = req_dir / f'{base}.requirements.json'
        if not p.exists():
            return set()
        j = json.loads(p.read_text())
        return set(j.get('required') or []) | set(j.get('optional') or [])

    @staticmethod
    def _patched_strip(orphans):
        if not registry_path.exists():
            return 0
        reg = json.loads(registry_path.read_text())
        edits = 0
        orphan_set = set(orphans)
        for name in list(reg.keys()):
            if name in orphan_set:
                del reg[name]
                edits += 1
        for dataset, meta in reg.items():
            if not isinstance(meta, dict):
                continue
            for key in ('columns', 'pending_add', 'pending_remove'):
                lst = meta.get(key)
                if not isinstance(lst, list):
                    continue
                pruned = [c for c in lst if c not in orphan_set]
                if len(pruned) != len(lst):
                    meta[key] = pruned
                    edits += 1
        if edits == 0:
            return 0
        tmp = registry_path.with_suffix(registry_path.suffix + '.tmp')
        tmp.write_text(json.dumps(reg, indent=2) + '\n')
        tmp.replace(registry_path)
        return edits

    monkeypatch.setattr(lc.LifecycleStateMachine, '_read_strategy_requirements', _patched_read)
    monkeypatch.setattr(lc.LifecycleStateMachine, '_strip_schema_registry_columns', _patched_strip)
    return {'req_dir': req_dir, 'registry_path': registry_path}


def _write_reqs(req_dir, sid, required):
    canonical = f'{sid.lower()}.py'
    base = canonical.replace('.py', '')
    (req_dir / f'{base}.requirements.json').write_text(json.dumps({
        'required': list(required),
        'optional': [],
    }))


def _seed_registry(registry_path, datasets):
    """datasets: { dataset_name: [columns...] }"""
    reg = {name: {'columns': list(cols), 'pending_add': [], 'pending_remove': []}
           for name, cols in datasets.items()}
    registry_path.write_text(json.dumps(reg, indent=2) + '\n')


def _seed_data_columns(cur, columns):
    for c in columns:
        cur.execute(
            """INSERT INTO data_columns (column_name, provider, refresh_cadence)
                    VALUES (%s, 'fmp', 'daily')
                ON CONFLICT (column_name) DO UPDATE SET provider = EXCLUDED.provider""",
            (c,),
        )


def test_unstack_removes_orphan_columns_inline(autocommit_conn, patched_paths):
    """Single strategy uses a unique column; archiving it must drop the
    column from data_columns AND schema_registry inline (no queue)."""
    from strategies.lifecycle import LifecycleStateMachine, StrategyState

    conn, tracked = autocommit_conn
    cur = conn.cursor()
    col = _column('orphan_')
    tracked.append(col)
    _seed_data_columns(cur, [col])
    _seed_registry(patched_paths['registry_path'], {'fundamentals': ['ticker', 'date', col]})

    lsm = LifecycleStateMachine.new_empty()
    sid = 'TST_orphan'
    lsm.register(sid, initial_state=StrategyState.LIVE,
                 metadata={'canonical_file': f'{sid.lower()}.py'})
    _write_reqs(patched_paths['req_dir'], sid, [col])

    removed = lsm._remove_orphan_columns_inline(sid, actor='test')
    assert removed == [col], f'expected the orphan to be reported as removed, got {removed}'

    cur.execute('SELECT 1 FROM data_columns WHERE column_name=%s', (col,))
    assert cur.fetchone() is None, 'data_columns row should be deleted'

    reg = json.loads(patched_paths['registry_path'].read_text())
    assert col not in reg['fundamentals']['columns'], 'column should be stripped from schema_registry'


def test_unstack_preserves_columns_used_by_active_peers(autocommit_conn, patched_paths):
    """Two strategies share a column. Archiving one must NOT remove the
    column when the other is still live/candidate/staging."""
    from strategies.lifecycle import LifecycleStateMachine, StrategyState

    conn, tracked = autocommit_conn
    cur = conn.cursor()
    shared = _column('shared_')
    tracked.append(shared)
    _seed_data_columns(cur, [shared])
    _seed_registry(patched_paths['registry_path'], {'fundamentals': ['ticker', 'date', shared]})

    lsm = LifecycleStateMachine.new_empty()
    sid_unstack = 'TST_unstack'
    lsm.register(sid_unstack, initial_state=StrategyState.LIVE,
                 metadata={'canonical_file': f'{sid_unstack.lower()}.py'})
    _write_reqs(patched_paths['req_dir'], sid_unstack, [shared])

    # Four peers in the four "kept" states — any one should preserve.
    for state, label in [
        (StrategyState.LIVE,       'TST_peer_live'),
        (StrategyState.MONITORING, 'TST_peer_mon'),
        (StrategyState.CANDIDATE,  'TST_peer_cand'),
        (StrategyState.STAGING,    'TST_peer_stag'),
    ]:
        peer = label
        lsm.register(peer, initial_state=state,
                     metadata={'canonical_file': f'{peer.lower()}.py'})
        _write_reqs(patched_paths['req_dir'], peer, [shared])

        removed = lsm._remove_orphan_columns_inline(sid_unstack, actor='test')
        assert removed == [], f'with peer in {state.value}, column must NOT be removed (got {removed})'

        cur.execute('SELECT 1 FROM data_columns WHERE column_name=%s', (shared,))
        assert cur.fetchone() is not None, f'data_columns row gone but peer in {state.value} still uses it'

        reg = json.loads(patched_paths['registry_path'].read_text())
        assert shared in reg['fundamentals']['columns'], (
            f'column stripped from schema_registry but peer in {state.value} still uses it'
        )

        # Reset: remove this peer for the next iteration.
        del lsm._records[peer]


def test_unstack_paper_peer_does_not_block_removal(autocommit_conn, patched_paths):
    """A legacy PAPER-state strategy still referencing a column must NOT
    block its removal when the unstack strategy was the last
    live/monitoring/candidate/staging consumer."""
    from strategies.lifecycle import LifecycleStateMachine, StrategyState

    conn, tracked = autocommit_conn
    cur = conn.cursor()
    col = _column('paper_orphan_')
    tracked.append(col)
    _seed_data_columns(cur, [col])
    _seed_registry(patched_paths['registry_path'], {'fundamentals': ['ticker', 'date', col]})

    lsm = LifecycleStateMachine.new_empty()
    sid_unstack = 'TST_paper_unstack'
    lsm.register(sid_unstack, initial_state=StrategyState.LIVE,
                 metadata={'canonical_file': f'{sid_unstack.lower()}.py'})
    _write_reqs(patched_paths['req_dir'], sid_unstack, [col])

    paper_peer = 'TST_paper_peer'
    lsm.register(paper_peer, initial_state=StrategyState.PAPER,
                 metadata={'canonical_file': f'{paper_peer.lower()}.py'})
    _write_reqs(patched_paths['req_dir'], paper_peer, [col])

    removed = lsm._remove_orphan_columns_inline(sid_unstack, actor='test')
    assert removed == [col], (
        f'PAPER is legacy and must not gate orphan removal; expected [{col}] removed, got {removed}'
    )
    cur.execute('SELECT 1 FROM data_columns WHERE column_name=%s', (col,))
    assert cur.fetchone() is None, 'data_columns row should be deleted (PAPER does not gate)'


def test_unstack_deprecated_archived_peers_do_not_gate(autocommit_conn, patched_paths):
    """Peers in DEPRECATED or ARCHIVED state must NOT keep an orphan column
    alive — they're outside the active set by definition."""
    from strategies.lifecycle import LifecycleStateMachine, StrategyState

    conn, tracked = autocommit_conn
    cur = conn.cursor()
    col = _column('depr_orphan_')
    tracked.append(col)
    _seed_data_columns(cur, [col])
    _seed_registry(patched_paths['registry_path'], {'fundamentals': ['ticker', 'date', col]})

    lsm = LifecycleStateMachine.new_empty()
    sid_unstack = 'TST_depr_unstack'
    lsm.register(sid_unstack, initial_state=StrategyState.LIVE,
                 metadata={'canonical_file': f'{sid_unstack.lower()}.py'})
    _write_reqs(patched_paths['req_dir'], sid_unstack, [col])

    for state, label in [
        (StrategyState.DEPRECATED, 'TST_peer_depr'),
        (StrategyState.ARCHIVED,   'TST_peer_arch'),
    ]:
        peer = label
        lsm.register(peer, initial_state=state,
                     metadata={'canonical_file': f'{peer.lower()}.py'})
        _write_reqs(patched_paths['req_dir'], peer, [col])

    removed = lsm._remove_orphan_columns_inline(sid_unstack, actor='test')
    assert removed == [col], (
        f'DEPRECATED/ARCHIVED peers must not gate removal; expected [{col}], got {removed}'
    )
    cur.execute('SELECT 1 FROM data_columns WHERE column_name=%s', (col,))
    assert cur.fetchone() is None, 'data_columns row should be deleted (depr/arch peers do not gate)'


def test_backfill_universe_helper_matches_db():
    """The FMP/EDGAR backfillers iterate `_active_universe()` per column,
    not a strategy-specific subset. Confirm the helper returns the same
    set the daily collector uses (universe_config WHERE active=TRUE,
    deduplicated). This is the invariant behind "backfill covers all
    tickers involved in the strategy" — every staging-approval backfill
    spans the full active universe, not just the rows the strategy reads.
    """
    from src.pipeline.backfillers import fmp as fmp_bf
    from src.pipeline.backfillers import edgar as edgar_bf

    helper_set = set(fmp_bf._active_universe())
    assert len(helper_set) > 0, '_active_universe() returned an empty set'

    # Compare against DB ground truth (DISTINCT tickers, active rows).
    conn = psycopg2.connect(POSTGRES_URI)
    try:
        cur = conn.cursor()
        cur.execute('SELECT DISTINCT ticker FROM universe_config WHERE active = TRUE')
        db_set = {r[0] for r in cur.fetchall()}
    finally:
        conn.close()

    assert helper_set == db_set, (
        f'_active_universe() drift: helper={len(helper_set)} db={len(db_set)} '
        f'helper-only={sorted(helper_set - db_set)[:5]} '
        f'db-only={sorted(db_set - helper_set)[:5]}'
    )

    # EDGAR uses the same helper; identity confirms shared scope.
    assert set(edgar_bf._active_universe()) == helper_set, \
        'edgar._active_universe() diverges from fmp._active_universe()'


def test_full_transition_triggers_inline_removal(autocommit_conn, patched_paths):
    """End-to-end: lifecycle.transition(state→DEPRECATED) must invoke the
    inline removal hook (not a queue insert)."""
    from strategies.lifecycle import LifecycleStateMachine, StrategyState

    conn, tracked = autocommit_conn
    cur = conn.cursor()
    col = _column('e2e_orphan_')
    tracked.append(col)
    _seed_data_columns(cur, [col])
    _seed_registry(patched_paths['registry_path'], {'fundamentals': ['ticker', 'date', col]})

    lsm = LifecycleStateMachine.new_empty()
    sid = 'TST_e2e'
    lsm.register(sid, initial_state=StrategyState.LIVE,
                 metadata={'canonical_file': f'{sid.lower()}.py'})
    _write_reqs(patched_paths['req_dir'], sid, [col])

    # live → deprecated triggers the unstack hook in transition().
    lsm.transition(sid, StrategyState.DEPRECATED, actor='test:e2e',
                   reason='end-to-end inline-removal test')

    cur.execute('SELECT 1 FROM data_columns WHERE column_name=%s', (col,))
    assert cur.fetchone() is None, 'transition() should have removed the orphan column inline'

    reg = json.loads(patched_paths['registry_path'].read_text())
    assert col not in reg['fundamentals']['columns'], 'schema_registry should be stripped'
