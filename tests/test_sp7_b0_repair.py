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
