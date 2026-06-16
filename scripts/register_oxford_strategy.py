#!/usr/bin/env python3
"""Register an oxf_* strategy as a research candidate: manifest state=candidate +
strategy_registry row (pending_approval). Idempotent. Usage:
  python3 scripts/register_oxford_strategy.py <strategy_id> <ClassName> "<name>" "<description>"

NOTE: The real LifecycleStateMachine API (verified in src/strategies/lifecycle.py):
  - factory is `from_manifest(path)` (NOT a `.load()` or path-taking constructor)
  - registered records live in `lsm._records` (no public `.strategies`)
  - `register(strategy_id, initial_state, metadata)` returns the new StrategyRecord
    and sets NEITHER instrument_class NOR eligible_regimes unless the SP-4 mint
    gate is on, so we set them on the returned record before save_manifest().
"""
import os, sys, psycopg2
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from strategies.lifecycle import LifecycleStateMachine, StrategyState  # noqa


def register_one(sid: str, cls: str, name: str, desc: str,
                 manifest_path: str, conn) -> None:
    """Register a single oxf_* strategy at the manifest + registry level.

    Idempotent: manifest register is a no-op if the id already exists;
    the registry upsert is ON CONFLICT DO NOTHING.
    """
    lsm = LifecycleStateMachine.from_manifest(manifest_path)
    if sid not in lsm._records:
        rec = lsm.register(sid, initial_state=StrategyState.CANDIDATE, metadata={
            'canonical_file': f'{sid}.py', 'class': cls, 'description': desc})
        # register() does not set these without the SP-4 mint gate; set them on
        # the returned record so to_dict() emits the same shape existing entries
        # use (top-level instrument_class + eligible_regimes).
        rec.instrument_class = 'etp'
        rec.eligible_regimes = ['LOW_VOL', 'TRANSITIONING']
        lsm.save_manifest(manifest_path)
        print(f'manifest: registered {sid} as candidate (etp)')
    else:
        print(f'manifest: {sid} already present ({lsm._records[sid].state.value})')

    # strategy_registry row (pending_approval) so the page join + status are present.
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO strategy_registry (id, name, description, tier, status,
            implementation_path, parameters, regime_conditions, universe,
            signal_frequency)
        VALUES (%s,%s,%s,%s,'pending_approval',%s,'{}'::jsonb,'{}'::jsonb,%s,%s)
        ON CONFLICT (id) DO NOTHING
    """, (sid, name, desc, 3, f'src/strategies/implementations/{sid}.py',
          ['SPY'], 'daily'))  # universe[] documentation only; strategy filters internally
    conn.commit()
    print(f'registry: {cur.rowcount} row(s) upserted for {sid}')
    cur.close()


def main():
    sid, cls, name, desc = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    root = os.path.join(os.path.dirname(__file__), '..')
    manifest_path = os.path.join(root, 'src', 'strategies', 'manifest.json')
    conn = psycopg2.connect(os.environ['POSTGRES_URI'])
    try:
        register_one(sid, cls, name, desc, manifest_path, conn)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
