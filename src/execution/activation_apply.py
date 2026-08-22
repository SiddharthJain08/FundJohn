#!/usr/bin/env python3
"""src/execution/activation_apply.py — daily-cycle `activation` step.

Makes the dashboard's Strategy Activation sliders take effect at the NEXT
DAILY CYCLE instead of the next weekly refresh (operator directive
2026-08-22). Runs FIRST in every compute chain (before `sentiment` /
`signals`), resolved by resolve_script.js / pipeline_orchestrator.py as a
plain src/execution step (300s budget; ~10s in practice).

What it does
  1. Gate: OPENCLAW_ACTIVATION_ASSIGNER must be '1' (same switch the weekly
     Mon 00:00 ET weekly_live_sharpe.js run honours). Otherwise: log + rc 0.
  2. Pending check (pipeline_config, server-clock timestamps):
        slider rows   strategy_activation_min_sharpe / strategy_activation_min_trades
        marker row    strategy_activation_last_applied (stamped by
                      activation_assigner after every clean non-dry-run --all)
     pending := marker missing OR any slider row's updated_at > marker.updated_at
     Not pending → log the last-applied state, rc 0, nothing touched.
  3. Apply (only when pending, or --force):
        nice -n 19 python3 -m backtest.activation_assigner --all --notify --trigger=daily_cycle
        nice -n 19 python3 -m execution.strategy_weights --rebuild --trigger=activation_slider --verbose
     The assigner re-derives strategy_regime_params.eligible (what the
     engine's is_eligible() and the sizer's calendar-edge clause read); the
     weights rebuild is REQUIRED after it — the sizer sizes from
     strategy_weights_by_regime.is_current, and a regime that just became
     eligible has no weight row until a rebuild (weight 0 ⇒ never sized).
     The rebuild here is WEIGHTS-ONLY: OPENCLAW_AUTO_DEMOTE is forced to '0'
     for this invocation so the registry auto-demote chain keeps its weekly
     cadence (a slider nudge must never demote a strategy mid-week).

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

ENV_GATE = 'OPENCLAW_ACTIVATION_ASSIGNER'
SLIDER_KEYS = ('strategy_activation_min_sharpe', 'strategy_activation_min_trades')
MARKER_KEY = 'strategy_activation_last_applied'
STEP = 'activation'

ASSIGNER_ARGV = ['nice', '-n', '19', sys.executable, '-m', 'backtest.activation_assigner',
                 '--all', '--notify', '--trigger=daily_cycle']
WEIGHTS_ARGV = ['nice', '-n', '19', sys.executable, '-m', 'execution.strategy_weights',
                '--rebuild', '--trigger=activation_slider', '--verbose']
SUBPROCESS_TIMEOUT_SEC = 120


def _log(msg: str) -> None:
    print(f'[{STEP}] {msg}', flush=True)


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
    ap.add_argument('--force', action='store_true', help='apply even when no slider changed')
    args = ap.parse_args(argv)
    env = dict(os.environ if env is None else env)

    if env.get(ENV_GATE) != '1':
        _log(f'SKIP: {ENV_GATE}!=1 (activation is operator-gated off; sliders are stored but not applied)')
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
        _log('no slider change since last apply — nothing to do (eligibility + weights unchanged)')
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
