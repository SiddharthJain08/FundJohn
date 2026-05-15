#!/usr/bin/env python3
"""Backfill per-strategy per-regime backtest snapshots into strategy_regime_backtests.

Iterates over strategies in manifest.json (filtered by --states), runs
auto_backtest.run_backtest() on each strategy file, and writes one row per
canonical regime (LOW_VOL, TRANSITIONING, HIGH_VOL, CRISIS) under a single
run_id.

Idempotent on (run_id, strategy_id, regime_state). A partial run that crashes
can be re-invoked with --resume-run-id <uuid> to skip already-written rows.

Used weekly by docs/strategy-backtest-refresh.timer; also runnable on-demand
for the Phase 3 gate-B walk-forward refresh.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
import traceback
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
load_dotenv(ROOT / '.env')

from strategies.auto_backtest import run_backtest

CANONICAL_REGIMES = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
IMPLEMENTATIONS_DIR = ROOT / 'src' / 'strategies' / 'implementations'


def _db_uri() -> str:
    return (
        os.environ.get('DATABASE_URL')
        or os.environ.get('POSTGRES_URI')
        or 'postgresql://openclaw:password@localhost:5432/openclaw'
    )


def _norm(name: str) -> str:
    """Normalize strategy id / filename for fuzzy match.

    The manifest uses ids like 'S_HV7_iv_crush_fade' but files are 'shv7_iv_crush_fade.py'
    (no underscore after the single-letter prefix). Lowercase and collapse 'X_YY' → 'XYY'
    when X is one letter, which handles the S_/SHV split.
    """
    s = name.removesuffix('.py').lower()
    # Collapse leading single-letter prefix: 's_hv7_...' → 'shv7_...'
    if len(s) > 2 and s[1] == '_' and s[0].isalpha() and s[2].isalpha():
        s = s[0] + s[2:]
    return s


_FILE_MAP: dict[str, Path] | None = None


def _file_map() -> dict[str, Path]:
    """Cached map of normalized filename → Path for all implementation files."""
    global _FILE_MAP
    if _FILE_MAP is None:
        # Skip __init__.py and underscore-prefixed files (templates / reference examples;
        # see src/strategies/implementations/_template_example.py — Phase 2E).
        _FILE_MAP = {
            _norm(p.name): p
            for p in IMPLEMENTATIONS_DIR.glob('*.py')
            if p.name != '__init__.py' and not p.name.startswith('_')
        }
    return _FILE_MAP


def resolve_filepath(strategy_id: str, entry: dict | None = None) -> Path | None:
    """Map strategy_id to its implementation file.

    Order: manifest metadata.canonical_file → exact `<id>.py` → normalized fuzzy.
    """
    if entry:
        canonical = entry.get('metadata', {}).get('canonical_file')
        if canonical:
            p = IMPLEMENTATIONS_DIR / canonical
            if p.exists():
                return p
    direct = IMPLEMENTATIONS_DIR / f'{strategy_id}.py'
    if direct.exists():
        return direct
    return _file_map().get(_norm(strategy_id))


def load_strategies(states: set[str]) -> list[tuple[str, dict, Path]]:
    """Return (strategy_id, manifest_entry, filepath) for strategies in `states`."""
    manifest = json.loads((ROOT / 'src' / 'strategies' / 'manifest.json').read_text())
    strategies = manifest.get('strategies', manifest)
    out: list[tuple[str, dict, Path]] = []
    skipped: list[tuple[str, str]] = []
    for sid, entry in strategies.items():
        if entry.get('state') not in states:
            continue
        fp = resolve_filepath(sid, entry)
        if fp is None:
            skipped.append((sid, 'file_not_found'))
            continue
        out.append((sid, entry, fp))
    if skipped:
        print(f'[backfill] Skipped {len(skipped)} strategies without files:')
        for sid, why in skipped:
            print(f'  - {sid}: {why}')
    return out


def already_written(conn, run_id: str, strategy_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            'SELECT 1 FROM strategy_regime_backtests WHERE run_id = %s AND strategy_id = %s LIMIT 1',
            (run_id, strategy_id),
        )
        return cur.fetchone() is not None


def write_strategy(conn, run_id: str, strategy_id: str, entry: dict, result: dict) -> int:
    """Insert one row per canonical regime. Returns count of rows written."""
    breakdown = result.get('regime_breakdown', {})
    declared = list(result.get('declared_regimes') or entry.get('eligible_regimes') or [])
    rows = []
    for regime in CANONICAL_REGIMES:
        rb = breakdown.get(regime, {'note': 'not_declared'})
        rows.append((
            run_id,
            strategy_id,
            regime,
            rb.get('sharpe'),
            rb.get('max_dd'),
            rb.get('total_return_pct'),
            rb.get('trade_count'),
            rb.get('oos_days'),
            rb.get('window_count'),
            rb.get('note'),
            declared,
            None,  # period_start — auto_backtest doesn't report a single span
            None,  # period_end
        ))
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO strategy_regime_backtests
                 (run_id, strategy_id, regime_state, sharpe, max_dd, total_return_pct,
                  trade_count, oos_days, window_count, note, declared_regimes,
                  period_start, period_end)
               VALUES %s
               ON CONFLICT (run_id, strategy_id, regime_state) DO NOTHING""",
            rows,
        )
    conn.commit()
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--states', default='live',
                    help='Comma-separated manifest states to backfill (default: live)')
    ap.add_argument('--resume-run-id', default=None,
                    help='UUID of a prior run to resume — skips strategies already written.')
    ap.add_argument('--limit', type=int, default=None,
                    help='Cap strategies processed (for testing).')
    ap.add_argument('--dry-run', action='store_true',
                    help='Print what would happen without writing rows.')
    args = ap.parse_args()

    states = {s.strip() for s in args.states.split(',') if s.strip()}
    run_id = args.resume_run_id or str(uuid.uuid4())
    print(f'[backfill] run_id = {run_id}')
    print(f'[backfill] states = {sorted(states)}')

    strategies = load_strategies(states)
    if args.limit:
        strategies = strategies[: args.limit]
    print(f'[backfill] {len(strategies)} strategies to process')

    if args.dry_run:
        for sid, _, fp in strategies:
            print(f'  would backtest {sid} ← {fp.relative_to(ROOT)}')
        return 0

    conn = psycopg2.connect(_db_uri())
    ok = 0
    fail = 0
    skip = 0
    t_start = time.monotonic()
    for i, (sid, entry, fp) in enumerate(strategies, 1):
        if args.resume_run_id and already_written(conn, run_id, sid):
            print(f'[backfill] [{i}/{len(strategies)}] {sid} — already written, skip')
            skip += 1
            continue
        t0 = time.monotonic()
        try:
            result = run_backtest(str(fp))
        except Exception:
            print(f'[backfill] [{i}/{len(strategies)}] {sid} — FAILED:')
            traceback.print_exc()
            fail += 1
            continue
        if result.get('error'):
            print(f'[backfill] [{i}/{len(strategies)}] {sid} — backtest error: {result["error"]}')
            fail += 1
            continue
        n = write_strategy(conn, run_id, sid, entry, result)
        dt = time.monotonic() - t0
        sharpe = result.get('sharpe')
        trades = result.get('trade_count')
        print(f'[backfill] [{i}/{len(strategies)}] {sid} — {n} regime rows, '
              f'agg sharpe={sharpe}, trades={trades}, {dt:.1f}s')
        ok += 1
    conn.close()
    dt_total = time.monotonic() - t_start
    print(f'[backfill] DONE. ok={ok} fail={fail} skip={skip} total_time={dt_total:.0f}s run_id={run_id}')
    return 0 if fail == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
