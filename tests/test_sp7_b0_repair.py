"""SP-7 Phase B Task 3 — B0 repair: gate, month enumeration, diff logic,
missing-row INSERT path."""
from __future__ import annotations
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from scripts import sp7_b0_repair_metadata as b0


def test_gate_refuses_without_env():
    with patch.dict('os.environ', {}, clear=False):
        import os
        os.environ.pop('OPENCLAW_BACKFILL_ALLOW_OVERWRITE', None)
        with pytest.raises(SystemExit) as e:
            b0.check_overwrite_gate()
        assert e.value.code == 2


def test_gate_passes_with_env():
    with patch.dict('os.environ', {'OPENCLAW_BACKFILL_ALLOW_OVERWRITE': '1'}):
        b0.check_overwrite_gate()  # no raise


def test_month_ends_span():
    ends = b0.month_ends(date(2021, 1, 1), date(2021, 3, 15))
    assert ends == [date(2021, 1, 31), date(2021, 2, 28), date(2021, 3, 15)]
    # final partial month capped at end date


def test_diff_updates_only_changed_rows():
    existing = pd.DataFrame([
        {'symbol': 'AAPL', 'in_sp500': True, 'in_r1000': False, 'in_r3000': False, 'market_cap': None},
        {'symbol': 'OK',   'in_sp500': False, 'in_r1000': True, 'in_r3000': True, 'market_cap': 5e9},
    ])
    rebuilt = pd.DataFrame([
        {'symbol': 'AAPL', 'in_sp500': True, 'in_r1000': True, 'in_r3000': True, 'market_cap': 2.9e12},
        {'symbol': 'OK',   'in_sp500': False, 'in_r1000': True, 'in_r3000': True, 'market_cap': 5e9},
    ])
    updates = b0.diff_derived(existing, rebuilt)
    assert [u['symbol'] for u in updates] == ['AAPL']
    assert updates[0]['in_r1000'] is True and updates[0]['market_cap'] == 2.9e12


# ── missing_rows tests ─────────────────────────────────────────────────────────

def test_missing_rows_returns_absent_symbols_only():
    """missing_rows returns full dicts for symbols in rebuilt but not in existing."""
    existing = pd.DataFrame([
        {'symbol': 'AAPL', 'in_sp500': True, 'in_r1000': True, 'in_r3000': True,
         'market_cap': 3e12},
    ])
    rebuilt = pd.DataFrame([
        {'symbol': 'AAPL', 'in_sp500': True, 'in_r1000': True, 'in_r3000': True,
         'market_cap': 3e12},
        {'symbol': 'ADP', 'in_sp500': True, 'in_r1000': True, 'in_r3000': True,
         'market_cap': 1e11},
        {'symbol': 'NEW', 'in_sp500': False, 'in_r1000': False, 'in_r3000': False,
         'market_cap': None},
    ])
    result = b0.missing_rows(existing, rebuilt)
    syms = [r['symbol'] for r in result]
    assert 'AAPL' not in syms, "existing symbol must not appear in missing_rows"
    assert 'ADP' in syms
    assert 'NEW' in syms
    assert len(result) == 2


def test_missing_rows_contains_all_rebuilt_columns():
    """Each dict from missing_rows has ALL columns from rebuilt, not just symbol."""
    existing = pd.DataFrame([{'symbol': 'X', 'in_sp500': False, 'in_r1000': False,
                               'in_r3000': False, 'market_cap': None}])
    rebuilt = pd.DataFrame([
        {'symbol': 'X',   'in_sp500': False, 'in_r1000': False, 'in_r3000': False,
         'market_cap': None},
        {'symbol': 'NEW', 'in_sp500': True,  'in_r1000': True,  'in_r3000': True,
         'market_cap': 5e10},
    ])
    result = b0.missing_rows(existing, rebuilt)
    assert len(result) == 1
    row = result[0]
    for col in rebuilt.columns:
        assert col in row, f"column {col!r} missing from missing_rows output"
    assert row['in_sp500'] is True
    assert row['market_cap'] == 5e10


def test_missing_rows_empty_when_all_present():
    """Returns empty list when rebuilt has no new symbols."""
    existing = pd.DataFrame([
        {'symbol': 'A', 'in_sp500': True, 'in_r1000': True,
         'in_r3000': True, 'market_cap': 1e9},
        {'symbol': 'B', 'in_sp500': False, 'in_r1000': False,
         'in_r3000': False, 'market_cap': None},
    ])
    rebuilt = existing.copy()
    assert b0.missing_rows(existing, rebuilt) == []


# ── dry-run never writes ────────────────────────────────────────────────────────

def test_dry_run_never_writes_to_db():
    """dry_run=True: missing_rows + diff_derived are computed but no DB write occurs.

    We verify by asserting that after a dry-run call the `write_month` path is
    not reached.  Since repair_month does an early return before the cursor
    write block, we can detect writes via a call tracker inserted via
    monkeypatching the pure helpers.
    """
    existing = pd.DataFrame([
        {'symbol': 'AAPL', 'in_sp500': False, 'in_r1000': False,
         'in_r3000': False, 'market_cap': None},
    ])
    rebuilt = pd.DataFrame([
        {'symbol': 'AAPL', 'in_sp500': True, 'in_r1000': True,
         'in_r3000': True, 'market_cap': 3e12},
        {'symbol': 'ADP',  'in_sp500': True, 'in_r1000': True,
         'in_r3000': True, 'market_cap': 9e10},
    ])

    # Verify diff_derived and missing_rows produce the expected values
    updates = b0.diff_derived(existing, rebuilt)
    missing = b0.missing_rows(existing, rebuilt)

    assert [u['symbol'] for u in updates] == ['AAPL'], \
        "diff_derived must detect the changed AAPL row"
    assert [m['symbol'] for m in missing] == ['ADP'], \
        "missing_rows must identify ADP as absent from existing"
    assert missing[0]['in_sp500'] is True

    # Confirm the dry-run guard in repair_month short-circuits before any DB write.
    # We simulate by calling _write_month_to_db if it existed; instead we just
    # verify the return value branch: dry_run returns len(updates) not touching DB.
    # This is tested indirectly — but the pure-function tests above confirm
    # both branches produce correct data, and the integration test in
    # test_diff_updates_only_changed_rows covers the UPDATE path.
    #
    # Direct structural test: build a minimal fake pg and confirm commit never fires.
    write_log: list[str] = []

    class _FakeCur:
        description = [type('C', (), {'name': c})() for c in
                       ['symbol', 'in_sp500', 'in_r1000', 'in_r3000', 'market_cap']]
        def execute(self, sql, *a):
            if any(kw in sql for kw in ('UPDATE', 'INSERT')):
                write_log.append(sql.strip()[:40])
        def fetchall(self):
            return [('AAPL', False, False, False, None)]
        def __enter__(self):  return self
        def __exit__(self, *a): return False

    class _FakePg:
        def cursor(self): return _FakeCur()
        def commit(self):
            write_log.append('COMMIT')

    import sys as _sys
    universe_mod = MagicMock()
    universe_mod.build_month_snapshot = MagicMock(return_value=rebuilt)
    universe_mod._sp500_membership_on = MagicMock(return_value={'AAPL', 'ADP'})
    caps_mod = MagicMock()
    caps_mod.build_market_cap_lookup = MagicMock(return_value={})

    orig_mods = {}
    for key in ('src.pipeline.backfillers.universe_metadata',
                'src.pipeline.market_cap_lookup'):
        orig_mods[key] = _sys.modules.pop(key, None)

    _sys.modules['src.pipeline.backfillers.universe_metadata'] = universe_mod
    _sys.modules['src.pipeline.market_cap_lookup'] = caps_mod
    try:
        b0.repair_month(_FakePg(), date(2023, 6, 30), dry_run=True)
    finally:
        for key, val in orig_mods.items():
            if val is None:
                _sys.modules.pop(key, None)
            else:
                _sys.modules[key] = val

    assert write_log == [], \
        f"dry_run=True must not write to DB; got: {write_log}"


# ── write-path execution test (recording fake pg/cursor) ──────────────────────

def test_write_path_sql_emission():
    """dry_run=False: exercise the real execute_batch/mogrify path against a
    recording fake cursor.

    Scenario
    --------
    existing : AAA (unchanged), BBB (will change on in_r1000/market_cap)
    rebuilt  : AAA (same), BBB (changed), CCC (new — missing from existing)

    Expected SQL output
    -------------------
    * UPDATE emitted with ALL %(name)s placeholders resolved (in_sp500,
      in_r1000, in_r3000, market_cap, snap, symbol).
    * INSERT contains 'ON CONFLICT (snapshot_date, symbol) DO NOTHING'
      and source_tag.
    * Audit INSERT contains 'backfill_audit'.
    * Exactly one commit() fires.
    * repair_month returns 2  (1 update + 1 insert).
    """
    import sys as _sys
    from datetime import date as _date
    import pandas as pd

    SNAP = _date(2023, 6, 30)

    # ── synthetic frames ──────────────────────────────────────────────────────
    _SCHEMA_COLS = [
        'snapshot_date', 'symbol', 'asset_class', 'exchange', 'status',
        'tradable', 'shortable', 'fractionable', 'easy_to_borrow',
        'market_cap', 'adv_usd_20d', 'sector', 'industry', 'options_eligible',
        'in_sp500', 'in_r1000', 'in_r3000', 'listed_date', 'delisted_date',
    ]

    def _row(sym, in_sp500, in_r1000, in_r3000, market_cap, **extra):
        base = {
            'snapshot_date': SNAP, 'symbol': sym, 'asset_class': 'us_equity',
            'exchange': 'NASDAQ', 'status': 'active',
            'tradable': True, 'shortable': True, 'fractionable': True,
            'easy_to_borrow': True, 'market_cap': market_cap,
            'adv_usd_20d': 1e6, 'sector': None, 'industry': None,
            'options_eligible': False,
            'in_sp500': in_sp500, 'in_r1000': in_r1000, 'in_r3000': in_r3000,
            'listed_date': None, 'delisted_date': None,
        }
        base.update(extra)
        return base

    # existing DB rows (SELECT returns these) — only DERIVED cols queried
    existing_rows = [
        # (symbol, in_sp500, in_r1000, in_r3000, market_cap)
        ('AAA', True, True, True, 1.0e12),   # unchanged
        ('BBB', False, False, False, None),   # will change
    ]

    # rebuilt frame returned by build_month_snapshot mock
    rebuilt_df = pd.DataFrame([
        _row('AAA', True, True, True, 1.0e12),      # same — no update
        _row('BBB', False, True, True, 5.0e11),     # changed in_r1000/market_cap
        _row('CCC', True, True, True, 2.0e11),      # new — INSERT needed
    ], columns=_SCHEMA_COLS)

    # ── recording cursor ──────────────────────────────────────────────────────
    executed_sqls: list[str] = []
    commit_count: list[int] = [0]

    class _RecordingCursor:
        """Fake cursor that records SQL calls and supports mogrify for execute_batch."""

        # description for the SELECT (symbol, in_sp500, in_r1000, in_r3000, market_cap)
        description = [
            type('C', (), {'name': c})()
            for c in ['symbol', 'in_sp500', 'in_r1000', 'in_r3000', 'market_cap']
        ]

        def execute(self, sql, args=None):
            if args:
                try:
                    rendered = sql % args if isinstance(args, dict) else sql % args
                except Exception:
                    rendered = sql
            else:
                rendered = sql
            executed_sqls.append(rendered.strip() if isinstance(rendered, str) else rendered.decode().strip())

        def mogrify(self, sql: str, args=None) -> bytes:
            """Simulate psycopg2 mogrify: substitute %(name)s placeholders.

            execute_batch calls mogrify per row; a KeyError here means a
            placeholder was not resolved — which is exactly the bug we guard.
            """
            if args is None:
                return sql.encode()
            if isinstance(args, dict):
                # Named-param substitution — raises KeyError on missing param
                result = sql % args
            else:
                result = sql % args
            return result.encode() if isinstance(result, str) else result

        def fetchall(self):
            return existing_rows

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    # Shared cursor instance so both cursor() calls go to same recorder
    _cur = _RecordingCursor()

    class _RecordingPg:
        def cursor(self):
            return _cur
        def commit(self):
            commit_count[0] += 1

    # ── patch build_month_snapshot, build_market_cap_lookup, _sp500_membership_on
    # repair_month does `from ... import ...` inside the function body, so we
    # patch the attributes on the source modules (which are already importable).
    with patch('src.pipeline.backfillers.universe_metadata.build_month_snapshot',
               return_value=rebuilt_df), \
         patch('src.pipeline.market_cap_lookup.build_market_cap_lookup',
               return_value={}), \
         patch('src.pipeline.backfillers.universe_metadata._sp500_membership_on',
               return_value={'AAA', 'BBB', 'CCC'}):

        result = b0.repair_month(_RecordingPg(), SNAP, dry_run=False)

    # ── assertions ────────────────────────────────────────────────────────────

    # 1. Return value = 1 update + 1 insert
    assert result == 2, f"expected 2 (1 update + 1 insert), got {result}"

    # 2. Exactly one commit
    assert commit_count[0] == 1, \
        f"expected exactly 1 commit(), got {commit_count[0]}"

    # 3. Collect all SQL as one big string for inspection
    all_sql = '\n'.join(executed_sqls)

    # 4. UPDATE was emitted with all named params resolved (no %(name)s remnants)
    update_stmts = [s for s in executed_sqls if 'UPDATE ticker_metadata_snapshots' in s]
    assert update_stmts, "No UPDATE statement recorded"
    for stmt in update_stmts:
        assert '%(' not in stmt, \
            f"UPDATE still contains unresolved placeholder: {stmt[:200]}"
    # Key param names must appear (resolved as values, not template)
    # Their template forms are gone — the values (True/False/number) are there
    update_joined = ' '.join(update_stmts)
    # spot-check: BBB is the changed row
    assert 'BBB' in update_joined, "UPDATE does not reference changed symbol BBB"

    # 5. INSERT contains ON CONFLICT clause and source_tag
    insert_stmts = [s for s in executed_sqls
                    if 'INSERT INTO ticker_metadata_snapshots' in s
                    and 'backfill_audit' not in s]
    assert insert_stmts, "No INSERT INTO ticker_metadata_snapshots statement recorded"
    insert_joined = ' '.join(insert_stmts)
    assert 'ON CONFLICT (snapshot_date, symbol) DO NOTHING' in insert_joined, \
        f"INSERT missing ON CONFLICT clause: {insert_joined[:300]}"
    assert 'source_tag' in insert_joined, \
        f"INSERT missing source_tag: {insert_joined[:300]}"
    assert '%(' not in insert_joined, \
        f"INSERT still contains unresolved placeholders: {insert_joined[:200]}"

    # 6. Audit INSERT was emitted
    audit_stmts = [s for s in executed_sqls if 'backfill_audit' in s]
    assert audit_stmts, "No backfill_audit INSERT recorded"
