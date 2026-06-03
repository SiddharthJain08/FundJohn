#!/usr/bin/env python3
"""SP-5.1c real-DB seam round-trip (ROLLED BACK).

Exercises the full chain:
  signal -> _params_with_option_spec -> execution_signals (option_spec in signal_params)
  upsert_hedge_ledger_on_fill -> option_hedge_ledger (active ledger row)
  compute_option_hedge_targets -> strategy_registry (FK anchor) + execution_signals (APPROVED hedge row)

All writes are within ONE transaction, rolled back in the finally block.
NEVER commits — execution_signals, strategy_registry, option_hedge_ledger are canonical.

Design decisions (per advisor review):
  - Insert a probe strategy_registry row first (status=pending_approval) to anchor the FK
    for the probe execution_signals INSERT, rather than borrowing a real approved id
    (which would muddy the cleanup assertion).
  - Use exact id match (not LIKE) for the post-rollback cleanup assertion (SQL _ is a wildcard).
  - Keep connection open post-rollback; close in finally.
  - All assertions filtered to exact probe ids so live active hedges don't cause interference.
"""
import os, sys, datetime as dt

REPO = '/root/openclaw/.claude/worktrees/sp5.1a-single-leg-options-exec'
os.chdir(REPO)
sys.path.insert(0, os.path.join(REPO, 'src'))

import psycopg2, psycopg2.extras
import json

# Read DB URI without sourcing .env (avoids bash unquoted-parens issues)
pg = os.popen("grep '^POSTGRES_URI=' /root/openclaw/.env | head -1 | cut -d= -f2-").read().strip().strip('"').strip("'")

import execution.option_hedge as oh
from execution.engine import _params_with_option_spec
from strategies.base import Signal, OptionSpec

# Probe identifiers — rolled back, never persisted
PROBE_SID = '__rt_probe_sid__'
HEDGE_SID = f'__hedge__{PROBE_SID}'   # = '__hedge____rt_probe_sid__'
UND = 'SPY'
AS_OF = dt.date.today()

# Stub live greeks: net structure delta = +110.0 (2 contracts × ~0.55 per leg × 100 shares)
# target_shares = -110 → direction SHORT
oh.compute_structure_delta = lambda legs, contracts: 110.0

conn = psycopg2.connect(pg)
try:
    cur = conn.cursor()

    # ── Pre-flight: confirm probe rows absent ────────────────────────────────
    cur.execute(
        "SELECT COUNT(*) FROM strategy_registry WHERE id IN (%s, %s)",
        (PROBE_SID, HEDGE_SID))
    pre_count = cur.fetchone()[0]
    assert pre_count == 0, (
        f'probe registry rows already exist ({pre_count}) — pick fresher probe ids')

    # ── Step 1a: insert probe strategy_registry row (FK anchor for probe ES row) ─
    cur.execute(
        """INSERT INTO strategy_registry (id, name, implementation_path, status, description)
           VALUES (%s, %s, %s, 'pending_approval', %s)""",
        (PROBE_SID,
         'SP-5.1c round-trip probe strategy',
         'scripts/sp5_1c_roundtrip.py',
         'Synthetic probe for SP-5.1c seam round-trip. Rolled back — never persisted.'))

    # ── Step 1b: build option signal_params via _params_with_option_spec ────
    probe_spec = OptionSpec(
        underlying='SPY',
        structure='straddle',
        hedge='delta',
        strike_rule='atm',
    )
    probe_signal = Signal(
        ticker='SPY',
        direction='BUY_VOL',
        entry_price=100.0,
        stop_loss=90.0,
        target_1=110.0,
        target_2=120.0,
        target_3=130.0,
        position_size_pct=0.05,
        confidence='MED',
        option_spec=probe_spec,
    )
    params_clean = _params_with_option_spec(probe_signal)
    assert 'option_spec' in params_clean, 'FAIL: option_spec not folded into params_clean'
    assert params_clean['option_spec']['structure'] == 'straddle', (
        f"FAIL: structure not 'straddle' in params_clean: {params_clean['option_spec']}")

    # Resolve workspace_id (mirrors engine.write_signals)
    import psycopg2.extras as _pge
    with conn.cursor(cursor_factory=_pge.RealDictCursor) as wcur:
        from execution.engine import resolve_workspace
        workspace_id = resolve_workspace(wcur, os.environ.get('WORKSPACE_ID', 'default'))
    assert len(str(workspace_id)) == 36, (
        f'FAIL: workspace_id not a 36-char UUID, got {workspace_id!r}')

    # Insert probe execution_signals row with the option_spec-carrying params
    now_utc = dt.datetime.now(dt.timezone.utc)
    cur.execute(
        """INSERT INTO execution_signals
             (strategy_id, workspace_id, signal_date, ticker, direction,
              entry_price, stop_loss, target_1, target_2, target_3,
              position_size_pct, regime_state, signal_params, status,
              lifecycle_state, computed_at, target_date)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)""",
        (PROBE_SID, workspace_id, AS_OF, 'SPY', 'BUY_VOL',
         100.0, 90.0, 110.0, 120.0, 130.0,
         0.05, None, json.dumps(params_clean), 'open',
         'COMPUTED', now_utc, AS_OF))

    # Assert option_spec persists through the DB (read back from DB — not just the dict)
    cur.execute(
        """SELECT signal_params->'option_spec'->>'structure'
           FROM execution_signals
           WHERE strategy_id=%s AND signal_date=%s AND ticker='SPY'""",
        (PROBE_SID, AS_OF))
    db_structure = cur.fetchone()
    assert db_structure is not None, 'FAIL: probe execution_signals row not written'
    assert db_structure[0] == 'straddle', (
        f"FAIL: DB signal_params->option_spec->structure = {db_structure[0]!r}, expected 'straddle'")
    print(f'Step 1 PASS: option_spec.structure={db_structure[0]!r} persists through DB')

    # ── Step 2: simulate structure fill — seed ledger row via upsert_hedge_ledger_on_fill ─
    legs = [
        {'occ': 'SPY260618C00500000', 'right': 'call', 'strike': 500.0, 'expiry': '2026-06-18'},
        {'occ': 'SPY260618P00500000', 'right': 'put',  'strike': 500.0, 'expiry': '2026-06-18'},
    ]
    oh.upsert_hedge_ledger_on_fill(
        cur,
        option_strategy_id=PROBE_SID,
        underlying=UND,
        legs=legs,
        contracts=2)

    # Assert the ledger row exists with status='active'
    cur.execute(
        """SELECT option_strategy_id, underlying, contracts, status, target_hedge_qty
           FROM option_hedge_ledger
           WHERE option_strategy_id=%s AND underlying=%s""",
        (PROBE_SID, UND))
    ledger_row = cur.fetchone()
    assert ledger_row is not None, 'FAIL: option_hedge_ledger row not written by upsert_hedge_ledger_on_fill'
    assert ledger_row[3] == 'active', (
        f"FAIL: ledger status must be 'active', got {ledger_row[3]!r}")
    # target_hedge_qty starts at 0 (the EOD compute step will set the real value)
    assert ledger_row[4] == 0 or ledger_row[4] is None, (
        f'FAIL: target_hedge_qty must be 0 after fill seed, got {ledger_row[4]}')
    print(f'Step 2 PASS: ledger row active, contracts={ledger_row[2]}, target_hedge_qty={ledger_row[4]}')

    # ── Step 3: compute_option_hedge_targets — FK seam + APPROVED hedge row ─
    # compute_structure_delta stubbed to 110.0 (no live Alpaca greeks call)
    oh.compute_option_hedge_targets(cur, AS_OF)

    # Assert: __hedge____rt_probe_sid__ registered in strategy_registry (FK anchor)
    cur.execute(
        "SELECT id, status, implementation_path FROM strategy_registry WHERE id=%s",
        (HEDGE_SID,))
    reg = cur.fetchone()
    print(f'registry row: {reg}')
    assert reg is not None, (
        f'FAIL: synthetic hedge registry row {HEDGE_SID!r} not written')
    assert reg[1] == 'pending_approval', (
        f'FAIL: hedge registry status must be pending_approval, got {reg[1]!r}')

    # Assert: FK anchor invisible to the engine's approved query
    cur.execute(
        "SELECT COUNT(*) FROM strategy_registry WHERE id=%s AND status='approved'",
        (HEDGE_SID,))
    assert cur.fetchone()[0] == 0, 'FAIL: hedge row visible to approved query (wrong status)'

    # Assert: APPROVED is_hedge execution_signals row written with correct fields
    cur.execute(
        """SELECT strategy_id, ticker, direction, lifecycle_state, status,
                  workspace_id,
                  signal_params->>'is_hedge',
                  (signal_params->>'hedge_shares')::float
           FROM execution_signals
           WHERE strategy_id=%s AND signal_date=%s""",
        (HEDGE_SID, AS_OF))
    es = cur.fetchone()
    print(f'execution_signals row: {es}')
    assert es is not None, 'FAIL: APPROVED is_hedge execution_signals row not written'
    assert es[2] == 'SHORT', (
        f'FAIL: expected SHORT (offsets +110 delta), got {es[2]!r}')
    assert es[3] == 'APPROVED', (
        f'FAIL: lifecycle_state must be APPROVED (gate-bypass), got {es[3]!r}')
    assert len(str(es[5])) == 36, (
        f'FAIL: workspace_id not a 36-char UUID, got {es[5]!r}')
    assert es[6] == 'true', (
        f"FAIL: is_hedge must be 'true', got {es[6]!r}")
    assert es[7] == 110.0, (
        f'FAIL: hedge_shares must be 110.0 (abs(-110)), got {es[7]}')
    print(f'Step 3 PASS: FK seam connects — registry+APPROVED hedge row, workspace={es[5]}, direction={es[2]}, hedge_shares={es[7]}')

    # Assert: ledger target_hedge_qty updated to -110 by compute_option_hedge_targets
    cur.execute(
        "SELECT target_hedge_qty FROM option_hedge_ledger WHERE option_strategy_id=%s AND underlying=%s",
        (PROBE_SID, UND))
    ledger_target = cur.fetchone()
    print(f'ledger target_hedge_qty after compute: {ledger_target}')
    assert ledger_target is not None and float(ledger_target[0]) == -110.0, (
        f'FAIL: ledger target_hedge_qty must be -110.0, got {ledger_target}')

    # ── Step 4: ROLLBACK + verify 0 probe rows persisted ────────────────────
    conn.rollback()

    # Fresh SELECTs after rollback (new implicit txn) — must see 0 probe rows
    cur.execute(
        "SELECT COUNT(*) FROM strategy_registry WHERE id IN (%s, %s)",
        (PROBE_SID, HEDGE_SID))
    assert cur.fetchone()[0] == 0, (
        'FAIL: strategy_registry probe rows persisted after rollback')

    cur.execute(
        "SELECT COUNT(*) FROM execution_signals WHERE strategy_id IN (%s, %s)",
        (PROBE_SID, HEDGE_SID))
    assert cur.fetchone()[0] == 0, (
        'FAIL: execution_signals probe rows persisted after rollback')

    cur.execute(
        "SELECT COUNT(*) FROM option_hedge_ledger WHERE option_strategy_id=%s",
        (PROBE_SID,))
    assert cur.fetchone()[0] == 0, (
        'FAIL: option_hedge_ledger probe rows persisted after rollback')

    print('\n*** SP-5.1c ROUND-TRIP PASS: signal->ledger->hedge-row, no FK violation, 0 rows persisted ***')

finally:
    conn.rollback()   # NEVER commit — canonical tables (also ends any read txn post-rollback)
    conn.close()
    print('rolled back (no persistence)')
