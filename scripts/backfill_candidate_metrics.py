#!/usr/bin/env python3
"""scripts/backfill_candidate_metrics.py

For every candidate-state strategy whose implementation file exists but
whose strategy_registry row is missing backtest metrics (or never existed),
run src/strategies/auto_backtest.py and persist the results. No LLM
spend — pure Python compute.

Idempotent: skips strategies that already have non-NULL metrics.

Usage:
    POSTGRES_URI=... python3 scripts/backfill_candidate_metrics.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parents[1]
IMPL_DIR = ROOT / 'src' / 'strategies' / 'implementations'
MANIFEST = ROOT / 'src' / 'strategies' / 'manifest.json'
AUTO_BT  = ROOT / 'src' / 'strategies' / 'auto_backtest.py'


def candidates_needing_metrics(conn) -> list[tuple[str, str, bool]]:
    """Return (sid, canonical_file, in_registry) for every candidate-state
    manifest entry whose registry metrics are all NULL — or that's missing
    from the registry entirely.
    """
    m = json.loads(MANIFEST.read_text())
    cur = conn.cursor()

    cur.execute("""
        SELECT id FROM strategy_registry
         WHERE backtest_sharpe IS NULL
           AND backtest_max_dd_pct IS NULL
           AND backtest_return_pct IS NULL
           AND backtest_trade_count IS NULL
    """)
    target_ids = {r[0] for r in cur.fetchall()}

    out = []
    for sid, rec in (m.get('strategies') or {}).items():
        if rec.get('state') != 'candidate':
            continue
        canonical = (rec.get('metadata') or {}).get('canonical_file') or f'{sid.lower()}.py'
        p = IMPL_DIR / canonical
        if not p.exists():
            continue
        cur.execute('SELECT 1 FROM strategy_registry WHERE id = %s', (sid,))
        in_registry = cur.fetchone() is not None
        if in_registry and sid not in target_ids:
            continue  # already has metrics
        out.append((sid, canonical, in_registry))
    return out


def run_auto_backtest(impl_path: Path, timeout_s: int = 600) -> dict:
    """Spawn auto_backtest.py on impl_path; return its JSON result."""
    proc = subprocess.run(
        ['python3', str(AUTO_BT), str(impl_path)],
        cwd=str(ROOT),
        capture_output=True, text=True, timeout=timeout_s,
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError(f'auto_backtest exit={proc.returncode}: {proc.stderr[:500]}')
    out = (proc.stdout or '').strip()
    # auto_backtest.py pretty-prints (json.dumps(..., indent=2)). Use a
    # streaming JSON decoder so any stray pre/post text (LAPACK warnings,
    # numpy noise) doesn't break parsing.
    decoder = json.JSONDecoder()
    brace = 0
    while brace < len(out):
        idx = out.find('{', brace)
        if idx < 0:
            break
        try:
            obj, _ = decoder.raw_decode(out[idx:])
            if isinstance(obj, dict) and 'sharpe' in obj:
                return obj
            brace = idx + 1
        except json.JSONDecodeError:
            brace = idx + 1
    raise RuntimeError(f'auto_backtest stdout has no parseable result JSON: {out[:400]}')


def envelope(x):
    if x is None:
        return None
    try:
        v = float(x)
    except Exception:
        return None
    if v != v or abs(v) > 100 or v in (float('inf'), float('-inf')):
        return None
    return v


def persist(conn, sid: str, canonical: str, in_registry: bool, result: dict,
            dry_run: bool, overwrite: bool = False):
    if 'error' in (result or {}) and result['error']:
        print(f'  [{sid}] auto_backtest reported error: {result["error"]}')
        return False

    sharpe = envelope(result.get('sharpe'))
    max_dd = envelope(result.get('max_dd'))
    ret    = envelope(result.get('total_return_pct'))
    trades = result.get('trade_count')
    trades = int(trades) if trades is not None else 0

    dd_pct = round(max_dd * 100 * 100) / 100 if max_dd is not None else None
    breakdown   = result.get('regime_breakdown')
    breakdown_j = json.dumps(breakdown) if isinstance(breakdown, dict) else None

    print(f'  [{sid}] sharpe={sharpe} dd={dd_pct}% return={ret} trades={trades}')
    if dry_run:
        return True

    cur = conn.cursor()
    if not in_registry:
        # Use the canonical upsert path so the row carries the FK target
        # used by lifecycle_events. Falling back to a minimal direct insert.
        cur.execute("""
            INSERT INTO strategy_registry (id, name, implementation_path, status,
                                           backtest_sharpe, backtest_max_dd_pct,
                                           backtest_return_pct, backtest_trade_count,
                                           backtest_regime_breakdown)
                 VALUES (%s, %s, %s, 'pending_approval', %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (id) DO UPDATE
              SET backtest_sharpe           = COALESCE(strategy_registry.backtest_sharpe, EXCLUDED.backtest_sharpe),
                  backtest_max_dd_pct       = COALESCE(strategy_registry.backtest_max_dd_pct, EXCLUDED.backtest_max_dd_pct),
                  backtest_return_pct       = COALESCE(strategy_registry.backtest_return_pct, EXCLUDED.backtest_return_pct),
                  backtest_trade_count      = COALESCE(strategy_registry.backtest_trade_count, EXCLUDED.backtest_trade_count),
                  backtest_regime_breakdown = COALESCE(EXCLUDED.backtest_regime_breakdown, strategy_registry.backtest_regime_breakdown)
        """, (sid, sid, f'src/strategies/implementations/{canonical}',
              sharpe, dd_pct, ret, trades, breakdown_j))
    else:
        if overwrite:
            # Operator-driven re-backtest. Replace existing metrics —
            # fresh trade count and breakdown, no COALESCE smearing.
            cur.execute("""
                UPDATE strategy_registry
                   SET backtest_sharpe           = %s,
                       backtest_max_dd_pct       = %s,
                       backtest_return_pct       = %s,
                       backtest_trade_count      = %s,
                       backtest_regime_breakdown = %s::jsonb
                 WHERE id = %s
            """, (sharpe, dd_pct, ret, trades, breakdown_j, sid))
        else:
            cur.execute("""
                UPDATE strategy_registry
                   SET backtest_sharpe           = COALESCE(backtest_sharpe, %s),
                       backtest_max_dd_pct       = COALESCE(backtest_max_dd_pct, %s),
                       backtest_return_pct       = COALESCE(backtest_return_pct, %s),
                       backtest_trade_count      = COALESCE(backtest_trade_count, %s),
                       backtest_regime_breakdown = COALESCE(backtest_regime_breakdown, %s::jsonb)
                 WHERE id = %s
            """, (sharpe, dd_pct, ret, trades, breakdown_j, sid))
    conn.commit()
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    conn = psycopg2.connect(os.environ['POSTGRES_URI'])
    todo = candidates_needing_metrics(conn)
    print(f'candidates needing metrics (with impl file): {len(todo)}')
    if not todo:
        return 0

    succeeded = 0
    failed    = 0
    for i, (sid, canonical, in_registry) in enumerate(todo, 1):
        print(f'[{i}/{len(todo)}] {sid} (in_registry={in_registry})')
        impl_path = IMPL_DIR / canonical
        try:
            result = run_auto_backtest(impl_path)
            ok = persist(conn, sid, canonical, in_registry, result, args.dry_run)
            if ok:
                succeeded += 1
            else:
                failed += 1
        except Exception as e:
            failed += 1
            print(f'  [{sid}] FAILED: {e}')
    print(f'\nbackfill complete — succeeded={succeeded} failed={failed}')
    conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
