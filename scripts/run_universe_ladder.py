#!/usr/bin/env python3
"""SP-7 Phase B — ladder queue driver.

  seed  — create/extend a run: insert (strategy × 4 tiers) queued cells.
          --strategy SID limits to one strategy (dashboard recompute);
          --arm touches data/.sp7_ladder_armed; builds the membership
          artifact if absent (delegates to scripts/build_tier_membership.py).
  drain — sequentially run queued cells until the queue is empty or TERM.
          Prints '[ladder] DONE' when no queued cells remain.

Resumability: terminal cell writes are atomic per cell; cells found in
status='running' at drain start are reset to 'queued' (a prior window's
SIGTERM landed mid-cell).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))


def _on_term(signum, frame):
    # Raise so a blocked subprocess.run aborts and reaps its child
    # (timeout --signal=TERM at 13:00 UTC must not orphan a running cell
    # into the EDGAR/premarket band on the 2-core box).
    raise SystemExit(143)

LADDER_TIERS = ('sp500', 'tier_r1000', 'tier_r3000', 'tier_liquid')
# Documentation constant (test-locked): the drain query's SQL CASE mirrors
# this ordering — keep the two in sync (extremes first for degenerate-skip).
TIER_PRIORITY = {'sp500': 0, 'tier_liquid': 1, 'tier_r1000': 2, 'tier_r3000': 3}
SLOW_BUDGETS = {
    'S_tr_03_bocpd_change_point': 21600,            # ~3.5h on 591 names
    'S_pairs_trading_jump_diffusion_intraday': 21600,
}
DEFAULT_BUDGET = 7200
RAM_FLOOR_MB = 1800
SENTINEL = ROOT / 'data' / '.sp7_ladder_armed'
GRID_KEYS = ('sharpe', 'max_dd_pct', 'win_rate', 'trades_n', 'sortino',
             'calmar', 'mean_holding_days', 'mean_universe_size')


def budget_for(strategy_id: str) -> int:
    return SLOW_BUDGETS.get(strategy_id, DEFAULT_BUDGET)


def is_degenerate(extremes: dict) -> bool:
    a, b = extremes.get('sp500'), extremes.get('tier_liquid')
    return bool(a and b and a.get('status') == 'done'
                and b.get('status') == 'done'
                and a.get('trade_sha') and a['trade_sha'] == b.get('trade_sha'))


def should_fail_strategy(recent_statuses: list[str]) -> bool:
    return len(recent_statuses) >= 3 and all(
        s == 'error' for s in recent_statuses[-3:])


def grid_row(tier: str, metrics: dict | None) -> dict:
    row = {'name': tier}
    for k in GRID_KEYS:
        row[k] = None if metrics is None else metrics.get(k)
    return row


def mem_available_mb() -> int:
    with open('/proc/meminfo') as f:
        for line in f:
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) // 1024
    return 0


def _pg():
    import psycopg2
    return psycopg2.connect(os.environ['POSTGRES_URI'])


# ── seed ────────────────────────────────────────────────────────────────
def cmd_seed(args) -> int:
    pg = _pg()
    run_id = args.run_id or f'ladder-{date.today().strftime("%Y%m%d")}'
    window_start, window_end = args.start, args.end or date.today().isoformat()
    artifact = (ROOT / 'data' /
                f'universe_tier_membership_{run_id}.parquet')
    if not artifact.exists():
        # Amendment 2: build the membership artifact with --start = first day of
        # the month BEFORE window_start, so bars from window day one resolve from
        # the prior month-end snapshot instead of pre-window-empty.
        ws = date.fromisoformat(window_start)
        if ws.month == 1:
            art_start = date(ws.year - 1, 12, 1)
        else:
            art_start = date(ws.year, ws.month - 1, 1)
        rc = subprocess.run(
            ['python3', 'scripts/build_tier_membership.py',
             '--run-id', run_id, '--start', art_start.isoformat(),
             '--end', window_end], cwd=str(ROOT)).returncode
        if rc != 0:
            print(f'[ladder-seed] membership build failed rc={rc}')
            return 1
    with pg.cursor() as cur:
        if args.strategy:
            sids = [args.strategy]
        else:
            cur.execute("SELECT id FROM strategy_registry "
                        "WHERE status='approved' ORDER BY id")
            sids = [r[0] for r in cur.fetchall()]
        n = 0
        for sid in sids:
            for tier in LADDER_TIERS:
                cur.execute("""
                    INSERT INTO universe_ladder_runs
                      (run_id, strategy_id, tier, status, window_start,
                       window_end, artifact_path)
                    VALUES (%s, %s, %s, 'queued', %s, %s, %s)
                    ON CONFLICT (run_id, strategy_id, tier) DO NOTHING""",
                    (run_id, sid, tier, window_start, window_end,
                     str(artifact)))
                n += cur.rowcount
    pg.commit()
    print(f'[ladder-seed] run={run_id} strategies={len(sids)} new_cells={n}')
    if not args.strategy:
        _redis_set('sp7:ladder:full_run_id', run_id)
    if args.arm:
        SENTINEL.touch()
        print(f'[ladder-seed] armed {SENTINEL}')
    pg.close()
    return 0


# ── drain ───────────────────────────────────────────────────────────────
def run_cell(cell: dict) -> dict:
    """Spawn universe_grid_cli for one cell; return terminal-state fields."""
    t0 = time.time()
    cmd = ['python3', '-m', 'backtest.universe_grid_cli',
           '--strategy', cell['strategy_id'],
           '--start', str(cell['window_start']),
           '--end', str(cell['window_end']),
           '--membership-artifact', cell['artifact_path'],
           '--tier', cell['tier']]
    try:
        # Amendment 1: backtest.universe_grid_cli is a module under src/, so it
        # requires PYTHONPATH=src (matches the :3000 server's spawn convention).
        res = subprocess.run(
            cmd, cwd=str(ROOT),
            env={**os.environ, 'PYTHONPATH': 'src'},
            capture_output=True, text=True,
            timeout=budget_for(cell['strategy_id']))
        dur = round(time.time() - t0, 1)
        if res.returncode == 0:
            metrics = json.loads(res.stdout.strip().splitlines()[-1])
            return {'status': 'done', 'metrics': metrics,
                    'trade_sha': metrics.get('trade_sha'),
                    'duration_s': dur, 'stderr_tail': None}
        return {'status': 'error', 'metrics': None, 'trade_sha': None,
                'duration_s': dur,
                'stderr_tail': (res.stderr or '')[-800:]}
    except subprocess.TimeoutExpired:
        return {'status': 'timeout', 'metrics': None, 'trade_sha': None,
                'duration_s': round(time.time() - t0, 1),
                'stderr_tail': f'timeout after {budget_for(cell["strategy_id"])}s'}
    except Exception as e:
        return {'status': 'error', 'metrics': None, 'trade_sha': None,
                'duration_s': round(time.time() - t0, 1),
                'stderr_tail': str(e)[:800]}


def cmd_drain(args) -> int:
    signal.signal(signal.SIGTERM, _on_term)
    pg = _pg()
    with pg.cursor() as cur:  # mid-kill recovery
        cur.execute("UPDATE universe_ladder_runs SET status='queued' "
                    "WHERE status='running'")
        if cur.rowcount:
            print(f'[ladder] reset {cur.rowcount} stuck running cells')
    pg.commit()

    # Liveness sweep: a prior crash may have left strategies with all cells
    # terminal but no rec row (died between cell-commit and finalize).
    # _maybe_finalize_strategy dedups via candidate_set_id, so this is idempotent.
    with pg.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT run_id, strategy_id FROM universe_ladder_runs r
             WHERE NOT EXISTS (SELECT 1 FROM universe_ladder_runs q
                               WHERE q.run_id=r.run_id AND q.strategy_id=r.strategy_id
                                 AND q.status IN ('queued','running'))""")
        stranded = cur.fetchall()
    for _run_id, _sid in stranded:
        _maybe_finalize_strategy(pg, _run_id, _sid)

    while True:
        if mem_available_mb() < RAM_FLOOR_MB:
            print(f'[ladder] RAM below floor ({mem_available_mb()}MB) — wait 300s')
            time.sleep(300)
            continue
        with pg.cursor() as cur:
            cur.execute("""
                SELECT id, run_id, strategy_id, tier, window_start,
                       window_end, artifact_path
                  FROM universe_ladder_runs WHERE status='queued'
                 ORDER BY queued_at, strategy_id,
                          -- tier-major within each strategy so all 4 cells finish
                          -- together; queued_at is constant (single-txn NOW() seed)
                          CASE tier WHEN 'sp500' THEN 0
                                    WHEN 'tier_liquid' THEN 1
                                    WHEN 'tier_r1000' THEN 2
                                    ELSE 3 END, id
                 LIMIT 1""")
            row = cur.fetchone()
        if row is None:
            _maybe_record_full_run(pg)
            print('[ladder] DONE')
            break
        cell = dict(zip(('id', 'run_id', 'strategy_id', 'tier',
                         'window_start', 'window_end', 'artifact_path'), row))
        # degenerate short-circuit + error policy BEFORE spending compute
        if _pre_skip(pg, cell):
            continue
        with pg.cursor() as cur:
            cur.execute("UPDATE universe_ladder_runs SET status='running', "
                        "started_at=NOW() WHERE id=%s", (cell['id'],))
        pg.commit()
        print(f"[ladder] cell {cell['strategy_id']}/{cell['tier']} "
              f"budget={budget_for(cell['strategy_id'])}s")
        result = run_cell(cell)
        with pg.cursor() as cur:
            cur.execute("""
                UPDATE universe_ladder_runs
                   SET status=%s, metrics=%s::jsonb, trade_sha=%s,
                       duration_s=%s, stderr_tail=%s, finished_at=NOW()
                 WHERE id=%s""",
                (result['status'],
                 json.dumps(result['metrics']) if result['metrics'] else None,
                 result['trade_sha'], result['duration_s'],
                 result['stderr_tail'], cell['id']))
        pg.commit()
        print(f"[ladder] cell {cell['strategy_id']}/{cell['tier']} "
              f"→ {result['status']} ({result['duration_s']}s)")
        _maybe_finalize_strategy(pg, cell['run_id'], cell['strategy_id'])
    pg.close()
    return 0


def _pre_skip(pg, cell) -> bool:
    """Apply degenerate-skip + 3-error policy; True if cell was skipped."""
    with pg.cursor() as cur:
        cur.execute("""SELECT tier, status, trade_sha
                         FROM universe_ladder_runs
                        WHERE run_id=%s AND strategy_id=%s""",
                    (cell['run_id'], cell['strategy_id']))
        cells = {r[0]: {'status': r[1], 'trade_sha': r[2]}
                 for r in cur.fetchall()}
        if cell['tier'] in ('tier_r1000', 'tier_r3000') and is_degenerate(cells):
            cur.execute("""UPDATE universe_ladder_runs
                              SET status='skipped_degenerate', finished_at=NOW()
                            WHERE id=%s""", (cell['id'],))
            pg.commit()
            print(f"[ladder] {cell['strategy_id']}/{cell['tier']} "
                  "skipped_degenerate (extremes identical)")
            _maybe_finalize_strategy(pg, cell['run_id'], cell['strategy_id'])
            return True
        # 3-consecutive-error policy: trailing terminal statuses by finish time
        cur.execute("""SELECT status FROM universe_ladder_runs
                        WHERE run_id=%s AND strategy_id=%s
                          AND status IN ('done','error','timeout')
                        ORDER BY finished_at""",
                    (cell['run_id'], cell['strategy_id']))
        trailing = [r[0] for r in cur.fetchall()]
        if should_fail_strategy(trailing):
            cur.execute("""UPDATE universe_ladder_runs
                              SET status='error',
                                  stderr_tail='strategy failed: 3 consecutive errors',
                                  finished_at=NOW()
                            WHERE id=%s""", (cell['id'],))
            pg.commit()
            _maybe_finalize_strategy(pg, cell['run_id'], cell['strategy_id'])
            return True
    return False


def finalize_payload(cells: dict, current: str) -> dict:
    """PURE verdict→rec-row mapping (unit-tested; the DB glue below stays
    thin). cells: tier -> {'status', 'metrics', 'w': (start, end)}."""
    from backtest.universe_ladder_selection import select_tier
    from backtest import universe_ladder_recs as recs

    window = next(iter(cells.values()))['w']
    metrics_by_tier = {t: (c['metrics'] if c['status'] == 'done' else None)
                       for t, c in cells.items()}
    degenerate = (
        cells.get('sp500', {}).get('status') == 'done'
        and cells.get('tier_liquid', {}).get('status') == 'done'
        and all(cells.get(t, {}).get('status') == 'skipped_degenerate'
                for t in ('tier_r1000', 'tier_r3000')))
    if degenerate:
        choice, verdict_name = current, 'universe-independent'
        rationale = 'sp7b ladder: extremes trade-identical → universe-independent'
    else:
        verdict = select_tier(metrics_by_tier)
        if verdict['verdict'] == 'no_signal':
            choice, verdict_name = current, 'no_signal'
        else:
            choice = verdict['choice']
            verdict_name = 'no_change' if choice == current else 'change'
        rationale = recs.build_rationale(verdict, window=window)
    summary = {'grid': [grid_row(t, metrics_by_tier.get(t))
                        for t in LADDER_TIERS],
               'window': list(window), 'verdict': verdict_name,
               'cell_statuses': {t: c['status'] for t, c in cells.items()},
               'candidate_set': list(LADDER_TIERS)}
    return {'choice': choice, 'verdict_name': verdict_name,
            'rationale': rationale, 'summary': summary}


def _maybe_finalize_strategy(pg, run_id: str, strategy_id: str) -> None:
    """When all 4 cells are terminal → finalize_payload → persist rec, post."""
    from backtest import universe_ladder_recs as recs

    with pg.cursor() as cur:
        cur.execute("""SELECT tier, status, metrics, window_start, window_end
                         FROM universe_ladder_runs
                        WHERE run_id=%s AND strategy_id=%s""",
                    (run_id, strategy_id))
        rows = cur.fetchall()
        cur.execute("""SELECT 1 FROM strategy_universe_recommendations
                        WHERE strategy_id=%s AND candidate_set_id=%s""",
                    (strategy_id, f'sp7b-1-{run_id}'))
        if cur.fetchone():
            return  # already finalized
    cells = {r[0]: {'status': r[1], 'metrics': r[2],
                    'w': (str(r[3]), str(r[4]))} for r in rows}
    if len(cells) < 4 or any(
            c['status'] in ('queued', 'running') for c in cells.values()):
        return
    current = _current_predicate(strategy_id)
    p = finalize_payload(cells, current)
    rec_id = recs.insert_recommendation(
        pg, strategy_id=strategy_id, current_predicate=current,
        candidate_predicate=p['choice'],
        candidate_set_id=f'sp7b-1-{run_id}',
        backtest_summary=p['summary'], rationale=p['rationale'])
    if p['verdict_name'] == 'change':
        msg = recs.format_change_message(strategy_id, current, p['choice'],
                                         p['rationale'], p['summary']['grid'],
                                         rec_id=rec_id)
        recs.post_discord(pg, msg)
    else:
        _queue_summary_line(pg, strategy_id, p['verdict_name'])
    print(f"[ladder] finalized {strategy_id}: {p['verdict_name']} → rec {rec_id}")


def _current_predicate(strategy_id: str) -> str:
    try:
        manifest = json.loads(
            (ROOT / 'src' / 'strategies' / 'manifest.json').read_text())
        ref = (manifest.get('strategies', {}).get(strategy_id, {})
               .get('metadata', {}).get('universe_filter_ref'))
        return ref.rsplit(':', 1)[-1] if ref else 'sp500'
    except Exception:
        return 'sp500'


def _queue_summary_line(pg, sid: str, verdict: str) -> None:
    """Batch non-change verdicts into one Discord summary at drain end —
    accumulate in redis list, flushed by cmd_drain's DONE path."""
    r = _redis()
    if r:
        r.rpush('sp7:ladder:summary_queue', f'{sid}:{verdict}')


def _maybe_record_full_run(pg) -> None:
    r = _redis()
    if not r:
        return
    # flush non-change summary
    items = []
    while True:
        v = r.lpop('sp7:ladder:summary_queue')
        if v is None:
            break
        sid, verdict = v.split(':', 1)
        items.append((sid, verdict))
    if items:
        from backtest import universe_ladder_recs as recs
        recs.post_discord(pg, recs.format_summary_message(items))
    full_run = r.get('sp7:ladder:full_run_id')
    if full_run:
        with pg.cursor() as cur:
            cur.execute("""SELECT count(*) FROM universe_ladder_runs
                           WHERE run_id=%s AND status IN ('queued','running')""",
                        (full_run,))
            if cur.fetchone()[0] == 0:
                r.set('sp7:ladder:last_full_run', date.today().isoformat())
                r.delete('sp7:ladder:full_run_id')
                print(f'[ladder] full run {full_run} complete — '
                      'sp7:ladder:last_full_run updated')


def _redis():
    try:
        import redis
        r = redis.from_url(os.environ.get('REDIS_URL',
                                          'redis://localhost:6379'),
                           socket_connect_timeout=3, decode_responses=True)
        r.ping()
        return r
    except Exception:
        return None


def _redis_set(k, v):
    r = _redis()
    if r:
        r.set(k, v)


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('seed')
    s.add_argument('--run-id')
    s.add_argument('--strategy')
    s.add_argument('--start', default='2021-07-01')
    s.add_argument('--end')
    s.add_argument('--arm', action='store_true')
    sub.add_parser('drain')
    args = ap.parse_args()
    return cmd_seed(args) if args.cmd == 'seed' else cmd_drain(args)


if __name__ == '__main__':
    sys.exit(main())
