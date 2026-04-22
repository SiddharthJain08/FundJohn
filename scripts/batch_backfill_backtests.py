#!/usr/bin/env python3
"""
Batch-runs auto_backtest.py on every candidate/paper/staging strategy that
doesn't already have measured backtest metrics in strategy_registry, and
persists the results.

Mirrors the wiring in research-orchestrator.js so the next dashboard fetch
surfaces the metrics for all strategies that successfully backtest.

Usage:
    python3 scripts/batch_backfill_backtests.py [--dry-run] [--timeout 300] [--limit N]

Output:
    logs/batch_backfill_backtests.jsonl  # one JSON line per strategy
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / 'src' / 'strategies' / 'manifest.json'
IMPL_DIR = ROOT / 'src' / 'strategies' / 'implementations'
LOG_DIR  = ROOT / 'logs'
LOG_DIR.mkdir(exist_ok=True)
LOG_PATH = LOG_DIR / 'batch_backfill_backtests.jsonl'

NEED_STATES = {'candidate', 'paper', 'staging'}


def pg_rows(sql: str):
    """Run a SELECT inside the openclaw-postgres container. Returns list of dicts.

    Pure deterministic — no LLM calls. Uses docker exec + psql -A -F '\t' for
    stable parsing.
    """
    cmd = [
        'docker', 'exec', 'openclaw-postgres',
        'psql', '-U', 'openclaw', '-d', 'openclaw',
        '-A', '-F', '\t', '-t', '-c', sql,
    ]
    out = subprocess.check_output(cmd, text=True).strip()
    rows = []
    if not out:
        return rows
    for line in out.splitlines():
        rows.append(line.split('\t'))
    return rows


def pg_exec(sql: str):
    cmd = [
        'docker', 'exec', 'openclaw-postgres',
        'psql', '-U', 'openclaw', '-d', 'openclaw', '-c', sql,
    ]
    subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def q(s: str) -> str:
    """Single-quote-escape for psql literal."""
    return "'" + str(s).replace("'", "''") + "'"


def already_covered_ids() -> set[str]:
    rows = pg_rows("SELECT id FROM strategy_registry WHERE backtest_sharpe IS NOT NULL")
    return {r[0] for r in rows}


def load_candidate_strategies() -> list[tuple[str, Path]]:
    manifest = json.loads(MANIFEST.read_text())
    out = []
    for sid, rec in manifest.get('strategies', {}).items():
        if rec.get('state') not in NEED_STATES:
            continue
        canon = (rec.get('metadata') or {}).get('canonical_file')
        if not canon:
            continue
        p = IMPL_DIR / canon
        if p.exists():
            out.append((sid, p))
    out.sort(key=lambda x: x[0])
    return out


def run_backtest_once(impl_path: Path, timeout: int) -> dict:
    """Run auto_backtest.py and parse its JSON stdout. Non-zero exit is expected
    on gate-fail — we still parse stdout to capture metrics."""
    t0 = time.time()
    try:
        res = subprocess.run(
            ['python3', 'src/strategies/auto_backtest.py', str(impl_path)],
            cwd=str(ROOT),
            capture_output=True, text=True,
            timeout=timeout,
        )
        raw = res.stdout or ''
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {
                'ok': False,
                'elapsed_s': round(time.time() - t0, 1),
                'error': 'non-json output',
                'stdout_tail': raw[-500:],
                'stderr_tail': (res.stderr or '')[-500:],
                'exit_code': res.returncode,
            }
        payload['ok'] = True
        payload['elapsed_s'] = round(time.time() - t0, 1)
        payload['exit_code'] = res.returncode
        return payload
    except subprocess.TimeoutExpired:
        return {'ok': False, 'elapsed_s': round(time.time() - t0, 1), 'error': f'timeout after {timeout}s'}
    except Exception as e:
        return {'ok': False, 'elapsed_s': round(time.time() - t0, 1), 'error': str(e)[:500]}


def persist(sid: str, bt: dict) -> None:
    """Write measured metrics to strategy_registry. COALESCE so we don't clobber
    existing numbers with NULL. auto_backtest max_dd is a fraction (0.058 =
    5.8%); registry stores percent."""
    def num(v):
        try:
            f = float(v)
            if f != f or f in (float('inf'), float('-inf')):
                return None
            return f
        except (TypeError, ValueError):
            return None

    sharpe = num(bt.get('sharpe'))
    max_dd_frac = num(bt.get('max_dd'))
    ret_pct = num(bt.get('total_return_pct'))
    dd_pct = round(max_dd_frac * 100, 2) if max_dd_frac is not None else None

    # Skip non-backtests. auto_backtest emits garbage sharpe values (e.g.
    # -3.15M) whenever the strategy generates 0 trades across all windows —
    # daily returns are all ≈ -RISK_FREE_DAILY and std → 0, so the ratio
    # explodes. Any run with 0 trades has no usable metric.
    trades = bt.get('trade_count') or 0
    if trades == 0:
        return
    # Also reject obviously-out-of-range values just in case.
    if sharpe is not None and abs(sharpe) > 100:
        return

    def lit(v):
        return 'NULL' if v is None else str(v)

    sql = f"""
      UPDATE strategy_registry
         SET backtest_sharpe     = COALESCE({lit(sharpe)}, backtest_sharpe),
             backtest_max_dd_pct = COALESCE({lit(dd_pct)}, backtest_max_dd_pct),
             backtest_return_pct = COALESCE({lit(ret_pct)}, backtest_return_pct)
       WHERE id = {q(sid)}
    """
    pg_exec(sql)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--timeout', type=int, default=300, help='per-strategy timeout (s)')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--limit', type=int, default=0, help='process at most N (0 = all)')
    args = ap.parse_args()

    covered = already_covered_ids()
    candidates = load_candidate_strategies()
    todo = [(sid, p) for (sid, p) in candidates if sid not in covered]
    if args.limit > 0:
        todo = todo[:args.limit]

    total = len(todo)
    print(f'[batch] candidates total={len(candidates)} covered={len(covered)} todo={total}')
    if args.dry_run:
        for sid, p in todo:
            print(f'  would run: {sid} ({p.name})')
        return

    start = time.time()
    passed = failed = errored = 0
    with LOG_PATH.open('a') as logf:
        for i, (sid, p) in enumerate(todo, 1):
            tag = f'[{i}/{total}]'
            print(f'{tag} {sid}  ...', flush=True)
            bt = run_backtest_once(p, args.timeout)
            bt['_sid'] = sid
            bt['_impl'] = str(p)
            bt['_ts'] = time.time()
            logf.write(json.dumps(bt) + '\n')
            logf.flush()
            if not bt.get('ok'):
                errored += 1
                print(f'  {tag} {sid} ERRORED: {bt.get("error", "?")}  ({bt["elapsed_s"]}s)', flush=True)
                continue
            try:
                persist(sid, bt)
            except Exception as e:
                print(f'  {tag} {sid} PERSIST_FAILED: {e}', flush=True)
                continue
            if bt.get('passed'):
                passed += 1
                mark = 'PASS'
            else:
                failed += 1
                mark = 'FAIL'
            print(f'  {tag} {sid} {mark} sharpe={bt.get("sharpe")} max_dd={bt.get("max_dd")} return={bt.get("total_return_pct")}% trades={bt.get("trade_count")}  ({bt["elapsed_s"]}s)', flush=True)

    dt = round(time.time() - start, 1)
    print(f'[batch] done in {dt}s  passed={passed} fail={failed} errored={errored}  total={total}')


if __name__ == '__main__':
    main()
