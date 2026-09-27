#!/usr/bin/env python3
"""src/execution/activation_apply.py — daily-cycle `activation` step.

Makes the dashboard's Strategy Activation min-TRADES slider, and a fresh
benchmark-sleeve backtest, take effect at the NEXT DAILY CYCLE instead of
the next weekly refresh (operator directive 2026-08-22; bench-relative
eligibility added 2026-09-25, spec docs/specs/2026-09-25-activation-
bench-relative-spec.md §2/§3). Runs FIRST in every compute chain (before
`sentiment` / `signals`), resolved by resolve_script.js /
pipeline_orchestrator.py as a plain src/execution step (300s budget; ~10s
in practice).

What it does
  1. Gate: OPENCLAW_ACTIVATION_ASSIGNER must be '1' (same switch the weekly
     Mon 00:00 ET weekly_live_sharpe.js run honours). Otherwise: log + rc 0.
  2. Pending check (pipeline_config, server-clock timestamps), pending :=
     ANY of:
        - marker row strategy_activation_last_applied missing
        - the min-TRADES slider row (strategy_activation_min_trades) has
          updated_at newer than the marker (the min-Sharpe slider row is
          no longer read here at all -- it no longer affects eligibility,
          Task 2)
        - a primary backtest run for any approved strategy landed after
          the marker (fresh re-backtests invalidate eligibility, 2026-09-08)
        - the benchmark sleeve's (registry `benchmark_sleeve=true`,
          S_beta_spy today -- same id resolution activation_assigner.
          resolve_bench_sleeve_id uses, reused here not re-derived) own
          latest primary_window run_id differs from the marker's
          `bench_sharpe`/`bench_run_id` (spec §2: "the re-apply trigger
          becomes 'sleeve primary run newer than last applied'"). A marker
          with no `bench_run_id` at all (pre-Task-1 marker, or a sleeve
          lookup that failed at stamp time) counts as pending too -- fail
          toward re-applying, not toward silently trusting a marker that
          never recorded what it was benchmarked against.
     Not pending → log the last-applied state, rc 0, nothing touched.
  3. Apply (only when pending, or --force):
        nice -n 19 python3 -m backtest.activation_assigner --all --notify --trigger=daily_cycle
        nice -n 19 python3 -m execution.strategy_weights --rebuild --trigger=activation_bench --verbose
     The assigner re-derives strategy_regime_params.eligible (what the
     engine's is_eligible() and the sizer's calendar-edge clause read); the
     weights rebuild is REQUIRED after it — the sizer sizes from
     strategy_weights_by_regime.is_current, and a regime that just became
     eligible has no weight row until a rebuild (weight 0 ⇒ never sized).
     The rebuild here is WEIGHTS-ONLY: OPENCLAW_AUTO_DEMOTE is forced to '0'
     for this invocation so the registry auto-demote chain keeps its weekly
     cadence (an activation nudge must never demote a strategy mid-week).

Exit codes
  0  nothing to do / applied cleanly / --dry-run
  1  assigner or weights rebuild failed (marker NOT advanced ⇒ retried next
     cycle). daily_cycle_node.js treats `activation` like `sentiment`: rc≠0
     posts the failure alert + persists stderr but NEVER aborts the chain —
     a stale activation must not cost the day's COMPUTED set. Under
     OPENCLAW_STRICT_EXIT_CODES=1 (live) every other step's rc=1 aborts, so
     this exemption is load-bearing.

--dry-run (also appended by PIPELINE_DRY_RUN=1) performs the pending check
and prints what WOULD run; no subprocesses, no writes.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import psycopg2

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

ENV_GATE = 'OPENCLAW_ACTIVATION_ASSIGNER'
# strategy_activation_min_sharpe (the min-Sharpe slider) REMOVED here
# (Task 2, spec 2026-09-25-activation-bench-relative §3): eligibility no
# longer reads it at all, so a slider row moving is no longer a pending
# reason. The row itself is left in pipeline_config, unread (append-only).
# The min-TRADES slider is UNCHANGED. strategy_activation_excess_sharpe (the
# EXCESS slider, spec 2026-09-25-activation-bench-relative §8 / Amendment 1,
# Task 4) is a NEW re-apply trigger: a newer excess row than the marker
# means eligibility was derived at a different threshold than what's now
# saved, so the daily activation step must re-derive it -- this reuses the
# existing generic "any SLIDER_KEYS row newer than the marker" loop below
# unchanged; only the key tuple grew.
SLIDER_KEYS = ('strategy_activation_min_trades', 'strategy_activation_excess_sharpe')
MARKER_KEY = 'strategy_activation_last_applied'
STEP = 'activation'

ASSIGNER_ARGV = ['nice', '-n', '19', sys.executable, '-m', 'backtest.activation_assigner',
                 '--all', '--notify', '--trigger=daily_cycle']
WEIGHTS_ARGV = ['nice', '-n', '19', sys.executable, '-m', 'execution.strategy_weights',
                '--rebuild', '--trigger=activation_bench', '--verbose']
SUBPROCESS_TIMEOUT_SEC = 120


def _log(msg: str) -> None:
    print(f'[{STEP}] {msg}', flush=True)


def _bench_sleeve_run_id(conn) -> Optional[str]:
    """Latest primary_window run_id for the registry's benchmark sleeve, as
    a string (matches backtest.activation_assigner.load_bench_sharpe's own
    `str(run_id)` cast, so the comparison in pending_state is string-to-
    string regardless of the underlying driver/column type). Sleeve id is
    resolved via activation_assigner.resolve_bench_sleeve_id -- REUSED, not
    re-derived, so a registry with multiple benchmark_sleeve=true rows
    ties-break identically here and in the assigner itself. Local import
    (mirrors activation_assigner.main()'s own `from execution.
    benchmark_sleeve import ...` pattern): keeps this fast, frequently-run
    step's module-load path lean when OPENCLAW_ACTIVATION_ASSIGNER is off
    (main() returns before this is ever called in that case) and avoids a
    backtest<->execution import cycle at module-import time.

    Fail-safe: any lookup failure returns None, which pending_state()
    treats as "can't tell" (no reason added on this check alone) -- NOT
    treated as pending, since the general newest-run-approved-strategies
    check above already covers a broken runs-table read; a broken
    registry/sleeve-id read must not force a re-apply every single cycle
    forever.

    ORDER BY run_at DESC, run_id DESC (F3, fix round 1): two sleeve primary
    runs stamped at the identical run_at (a backfill, or a fast rerun in
    the same second) must resolve deterministically to the same row every
    time this query runs, matching backtest.activation_assigner.
    load_bench_sharpe's own identical tie-break on the same query shape --
    an ORDER BY on run_at alone would leave Postgres free to return either
    tied row, which could disagree between this check and the assigner's
    own resolution of "the sleeve's latest run"."""
    try:
        from backtest.activation_assigner import resolve_bench_sleeve_id
        sleeve_id, _source = resolve_bench_sleeve_id(conn)
        cur = conn.cursor()
        cur.execute("""SELECT run_id FROM strategy_backtest_runs
                        WHERE strategy_id = %s AND primary_window = TRUE
                        ORDER BY run_at DESC, run_id DESC LIMIT 1""", (sleeve_id,))
        row = cur.fetchone()
        cur.close()
        return str(row[0]) if row and row[0] is not None else None
    except Exception as e:
        _log(f'benchmark sleeve run_id probe failed ({e}) — bench re-apply check skipped this cycle')
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def pending_state(conn) -> dict:
    """Return {'pending': bool, 'reasons': [...], 'marker': dict|None,
    'marker_updated_at': ts|None, 'sliders': {key: {'value','updated_at'}}}.

    Comparison is on pipeline_config.updated_at (server clock on both sides:
    the dashboard PUT writes NOW(), the assigner's stamp writes NOW()), so
    client clocks / ISO formatting never enter into it. Fail-safe direction:
    any read error ⇒ pending=True (an idempotent re-apply is the cheap
    mistake; a silently skipped slider is the expensive one).
    """
    out = {'pending': False, 'reasons': [], 'marker': None,
           'marker_updated_at': None, 'sliders': {}}
    try:
        cur = conn.cursor()
        cur.execute(
            'SELECT key, value, updated_at FROM pipeline_config WHERE key = ANY(%s)',
            (list(SLIDER_KEYS) + [MARKER_KEY],))
        rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        cur.close()
    except Exception as e:
        out['pending'] = True
        out['reasons'].append(f'pipeline_config read failed ({e}) — fail-safe re-apply')
        try:
            conn.rollback()
        except Exception:
            pass
        return out

    marker = rows.get(MARKER_KEY)
    if marker is not None:
        try:
            out['marker'] = json.loads(marker[0]) if marker[0] else {}
        except (TypeError, ValueError):
            out['marker'] = {'raw': marker[0]}
        out['marker_updated_at'] = marker[1]

    for k in SLIDER_KEYS:
        r = rows.get(k)
        if r is None:
            continue
        out['sliders'][k] = {'value': r[0], 'updated_at': r[1]}

    if marker is None:
        out['pending'] = True
        out['reasons'].append(f'{MARKER_KEY} missing — eligibility never stamped as applied')
        return out

    m_ts = marker[1]
    for k, s in out['sliders'].items():
        ts = s['updated_at']
        if ts is not None and m_ts is not None and ts > m_ts:
            out['pending'] = True
            out['reasons'].append(
                f'{k}={s["value"]} set {ts.isoformat()} > last applied {m_ts.isoformat()}')

    # Fresh re-backtests also invalidate eligibility (2026-09-08): the fleet
    # epoch re-backtests strategies NIGHTLY, but the assigner only ran on
    # slider changes + the Mon 00:00 ET weekly cron — so a Sunday 09:12 run
    # that turned S_ma_tsmom_crossover negative in every regime sat ELIGIBLE
    # behind a 04:00 marker for two trading days (39 stale-eligible cells
    # measured, worst sleeve −6.9). A primary run landing after the marker
    # now makes the daily activation step re-derive. Fail-safe: a read error
    # here does NOT force pending (the sliders above already cover their own
    # failure); it just logs — a broken runs-table read must not re-apply
    # eligibility every cycle forever.
    try:
        cur = conn.cursor()
        cur.execute("""SELECT MAX(r.run_at) FROM strategy_backtest_runs r
                        JOIN strategy_registry sr ON sr.id = r.strategy_id
                       WHERE r.primary_window AND sr.status = 'approved'""")
        row = cur.fetchone()
        cur.close()
        newest_run = row[0] if row else None
        out['newest_primary_run_at'] = newest_run
        if newest_run is not None and m_ts is not None and newest_run > m_ts:
            out['pending'] = True
            out['reasons'].append(
                f'primary backtest run landed {newest_run.isoformat()} > last applied '
                f'{m_ts.isoformat()} — eligibility derived from superseded sleeves')
    except Exception as e:
        _log(f'newest-run staleness probe failed ({e}) — slider-only pending check')
        try:
            conn.rollback()
        except Exception:
            pass

    # Benchmark-sleeve re-apply trigger (spec §2, Task 2): "the re-apply
    # trigger 'slider row newer than last applied' becomes 'sleeve primary
    # run newer than last applied'". The general staleness probe above only
    # looks at APPROVED strategies' runs -- the benchmark sleeve itself may
    # not be `status='approved'` (it's a sizing/comparator sleeve, not a
    # tradeable alpha strategy), so its own fresh backtest needs its own
    # check. Compared by run_id (not run_at): the marker stores the exact
    # run the bench vector was derived from (`bench_run_id`, spec §2), so
    # comparing ids is exact where a timestamp comparison could be fooled by
    # clock skew or a same-timestamp re-run. Fail-safe direction (F4, fix
    # round 1 -- corrected to match the code at :246 and _bench_sleeve_
    # run_id's own docstring above): a failed lookup is NOT pending (None) --
    # it adds no reason here and defers to the general staleness probe
    # above, so a broken registry/runs-table read doesn't force a re-apply
    # every cycle forever. What DOES count as pending: once the lookup
    # resolves a real sleeve run_id, a marker with no `bench_run_id` at all
    # (pre-Task-1 marker, or a sleeve lookup that failed at STAMP time) is
    # pending -- an eligibility run can't tell whether it was actually
    # benchmarked against something real without this field.
    bench_run_id = _bench_sleeve_run_id(conn)
    out['bench_run_id'] = bench_run_id
    if bench_run_id is not None:
        marker_bench_run_id = out['marker'].get('bench_run_id') if isinstance(out['marker'], dict) else None
        if marker_bench_run_id != bench_run_id:
            out['pending'] = True
            out['reasons'].append(
                f'benchmark sleeve primary run {bench_run_id!r} != last-applied '
                f'bench_run_id {marker_bench_run_id!r} — eligibility derived from a '
                f'stale or absent bench comparator')
    return out


def _run(argv: list[str], env: dict, label: str, runner=subprocess.run) -> int:
    _log(f'{label}: {" ".join(argv)}')
    try:
        res = runner(argv, cwd=str(ROOT), env=env, timeout=SUBPROCESS_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        _log(f'{label}: TIMEOUT after {SUBPROCESS_TIMEOUT_SEC}s')
        return 124
    except Exception as e:
        _log(f'{label}: spawn failed: {e}')
        return 2
    rc = int(getattr(res, 'returncode', 0) or 0)
    _log(f'{label}: rc={rc}')
    return rc


def apply(env: Optional[dict] = None, runner=subprocess.run) -> int:
    """Run assigner then weights rebuild (weights-only). Returns 0 on success,
    1 on any failure. The weights rebuild is skipped when the assigner fails
    — weights derived on half-updated eligibility would be worse than stale."""
    base = dict(os.environ if env is None else env)
    pp = [str(ROOT), str(ROOT / 'src')]
    if base.get('PYTHONPATH'):
        pp.append(base['PYTHONPATH'])
    base['PYTHONPATH'] = os.pathsep.join(pp)

    rc = _run(ASSIGNER_ARGV, base, 'activation_assigner', runner=runner)
    if rc != 0:
        _log('assigner failed — weights rebuild SKIPPED; marker not advanced, will retry next cycle')
        return 1

    w_env = dict(base)
    w_env['OPENCLAW_AUTO_DEMOTE'] = '0'   # weights-only; demote chain stays weekly
    rc = _run(WEIGHTS_ARGV, w_env, 'strategy_weights --rebuild', runner=runner)
    if rc != 0:
        _log('weights rebuild failed — eligibility IS applied but weights are stale until the next '
             'successful rebuild (weekly Mon 00:00 ET or a manual --rebuild)')
        return 1
    return 0


def main(argv: Optional[list[str]] = None, env: Optional[dict] = None,
         connect=psycopg2.connect, runner=subprocess.run) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n', 1)[0])
    ap.add_argument('--date', default=None, help='run date (accepted for step-runner parity; unused)')
    ap.add_argument('--dry-run', action='store_true', help='pending check only; no subprocesses, no writes')
    ap.add_argument('--force', action='store_true', help='apply even when nothing is pending')
    args = ap.parse_args(argv)
    env = dict(os.environ if env is None else env)

    if env.get(ENV_GATE) != '1':
        _log(f'SKIP: {ENV_GATE}!=1 (activation is operator-gated off; the min-trades slider row and '
             f'the benchmark sleeve\'s own backtests are stored but not applied)')
        return 0

    uri = env.get('POSTGRES_URI') or env.get('DATABASE_URL')
    if not uri:
        _log('POSTGRES_URI not set — cannot read pipeline_config')
        return 1

    try:
        conn = connect(uri)
    except Exception as e:
        _log(f'DB connect failed: {e}')
        return 1
    try:
        st = pending_state(conn)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    for k, s in st['sliders'].items():
        ts = s['updated_at'].isoformat() if s['updated_at'] is not None else 'n/a'
        _log(f'slider {k}={s["value"]} (set {ts})')
    if st['marker_updated_at'] is not None:
        _log(f'last applied {st["marker_updated_at"].isoformat()} {json.dumps(st["marker"], sort_keys=True)}')

    if not st['pending'] and not args.force:
        _log('nothing pending since last apply (min-trades slider, backtests, bench sleeve) '
             '— nothing to do (eligibility + weights unchanged)')
        return 0

    why = '; '.join(st['reasons']) if st['reasons'] else '--force'
    _log(f'PENDING: {why}')
    if args.dry_run:
        _log('dry-run: would run ' + ' && '.join(' '.join(a) for a in (ASSIGNER_ARGV, WEIGHTS_ARGV)))
        return 0

    rc = apply(env=env, runner=runner)
    _log('applied: eligibility re-derived + weights rebuilt — takes effect in THIS cycle'
         if rc == 0 else 'apply FAILED (see above)')
    return rc


if __name__ == '__main__':
    sys.exit(main())
