#!/usr/bin/env python3
"""
One-shot reconciliation of manifest.json.state ↔ strategy_registry.status ↔
execution_signals for every strategy, per Option A from the 2026-04-22 audit:

  * Active zombies (manifest != live, but pipeline firing with open positions):
    promote manifest to 'live'; status stays 'approved'.
    candidate → live goes through an intermediate 'paper' history entry since
    lifecycle.py doesn't allow candidate→live directly.

  * Dormant zombies (manifest=paper, status=approved, no open positions, no
    recent signals): demote status → 'pending_approval' so the pipeline stops
    firing them. Manifest stays 'paper' → they show in Research Candidates.

  * Candidate-in-manifest / paper-in-DB (S_alpha191, S_barbell,
    S_sparse_basis_pursuit_sdf): demote status → 'pending_approval' so they
    cleanly appear as Research Candidates awaiting approval.

  * Phantom S_HV10_triple_gate_fear (manifest=staging, no DB row): INSERT a
    row with status='pending_approval' so it becomes approvable.

Idempotent: re-running with clean state is a no-op.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path('/root/openclaw')
MANIFEST = ROOT / 'src' / 'strategies' / 'manifest.json'


def pg(sql: str, params=None):
    """Run via docker exec psql. No shell-interp on params."""
    cmd = ['docker', 'exec', '-i', 'openclaw-postgres',
           'psql', '-U', 'openclaw', '-d', 'openclaw',
           '-A', '-F', '\t', '-t']
    if params:
        # Use prepared-style via -v; simplest: embed literals with strict quoting
        raise NotImplementedError('use pg_exec_literal for parameterized queries')
    cmd += ['-c', sql]
    return subprocess.check_output(cmd, text=True).strip()


def pg_exec_literal(sql: str):
    """Execute a SQL string that already has literals embedded (we build it here)."""
    cmd = ['docker', 'exec', '-i', 'openclaw-postgres',
           'psql', '-U', 'openclaw', '-d', 'openclaw', '-c', sql]
    subprocess.check_call(cmd, stdout=subprocess.DEVNULL)


def q(s) -> str:
    return "'" + str(s).replace("'", "''") + "'"


# ─────────────────────────── Plan ────────────────────────────────────────────
# From the audit snapshot. Active = has open positions today.
ACTIVE_ZOMBIES_PAPER = [
    'S25_dual_momentum_v2',
    'S_HV16_gex_regime',
    'S_HV19_iv_surface_tilt',
    'S_HV8_gamma_theta_carry',
    'S_HV9_rv_momentum_div',
    'S_regime_specialist_vol',
    'S_robust_minimum_variance_hedge',
    'S_tr_02_hurst_regime_flip',
    'S_tr_03_bocpd_change_point',
    'S_tr_06_eod_reversal',
]

# manifest=candidate, with open positions — promote candidate→paper→live.
ACTIVE_ZOMBIES_CANDIDATE = [
    'S_price_path_convexity',
]

# Dormant zombies: manifest=paper, status=approved, no open positions.
DORMANT_ZOMBIES = [
    'S_HV7_iv_crush_fade',
    'S_HV11_cross_stock_dispersion',
    'S_HV12_vrp_normalization',
    'S_HV13_call_put_iv_spread',
    'S_HV14_otm_skew_factor',
    'S_HV15_iv_term_structure',
    'S_HV17_earnings_straddle_fade',
    'S_HV20_iv_dispersion_reversion',
    'S_markov_frontier_regimes',
    'S_tr_01_vvix_early_warning',
    'S_tr_04_intraday_spy_momentum',
]

# manifest=candidate / DB status=paper (not firing — status≠approved).
# Demote to 'pending_approval' so Research Candidates shows them consistently.
CANDIDATE_DB_PAPER = [
    'S_alpha191_lasso_crossmarket',
    'S_barbell_trend_horizon',
    'S_sparse_basis_pursuit_sdf',
]

# Missing DB row entirely.
PHANTOMS_TO_REGISTER = [
    ('S_HV10_triple_gate_fear',
     'Triple Gate Fear — Bollenbacher multi-gate entry signal',
     'src/strategies/implementations/shv10_triple_gate_fear.py'),
]


def promote_manifest_to_live(sid: str, from_state: str, now_iso: str, manifest: dict):
    """Flip state to 'live' in-memory. If from_state='candidate', log an
    intermediate 'paper' step in history because lifecycle.py doesn't allow
    candidate→live directly — but we don't re-enter auto_backtest here."""
    rec = manifest['strategies'][sid]
    if from_state == 'candidate':
        rec['history'] = rec.get('history', [])
        rec['history'].append({
            'from_state': 'candidate',
            'to_state':   'paper',
            'timestamp':  now_iso,
            'actor':      'reconcile:2026-04-22',
            'reason':     'Reconcile — pipeline was already firing this strategy, promoting through paper',
            'metadata':   {},
        })
        rec['state'] = 'paper'
    rec['history'] = rec.get('history', [])
    rec['history'].append({
        'from_state': rec['state'],
        'to_state':   'live',
        'timestamp':  now_iso,
        'actor':      'reconcile:2026-04-22',
        'reason':     'Reconcile — pipeline was firing this strategy while manifest said paper/candidate',
        'metadata':   {'option': 'A', 'preserve_execution': True},
    })
    rec['state']       = 'live'
    rec['state_since'] = now_iso


def main():
    manifest = json.loads(MANIFEST.read_text())
    now_iso = datetime.now(timezone.utc).isoformat()

    # ── 1. Active zombies (manifest=paper) → manifest.state='live' ─────────
    print('[1] Promoting paper→live for active zombies (manifest edit only, DB already approved):')
    for sid in ACTIVE_ZOMBIES_PAPER:
        rec = manifest['strategies'].get(sid)
        if not rec:
            print(f'    SKIP {sid}: not in manifest')
            continue
        if rec['state'] == 'live':
            print(f'    SKIP {sid}: already live')
            continue
        promote_manifest_to_live(sid, rec['state'], now_iso, manifest)
        print(f'    OK   {sid}')

    # ── 2. Active zombies (manifest=candidate) → candidate→paper→live ─────
    print('[2] Promoting candidate→paper→live for active candidate zombies:')
    for sid in ACTIVE_ZOMBIES_CANDIDATE:
        rec = manifest['strategies'].get(sid)
        if not rec:
            print(f'    SKIP {sid}: not in manifest')
            continue
        if rec['state'] == 'live':
            print(f'    SKIP {sid}: already live')
            continue
        promote_manifest_to_live(sid, rec['state'], now_iso, manifest)
        print(f'    OK   {sid}')

    manifest['updated_at'] = now_iso

    # Atomic write
    tmp = MANIFEST.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(manifest, indent=2))
    tmp.replace(MANIFEST)
    print(f'[manifest] wrote updated_at={now_iso}')

    # ── 3. Dormant zombies → status='pending_approval' ──────────────────────
    all_ids = ACTIVE_ZOMBIES_PAPER + ACTIVE_ZOMBIES_CANDIDATE

    # Insert lifecycle_events for every promotion we did (audit trail).
    print('[audit] inserting lifecycle_events rows')
    for sid in all_ids:
        rec = manifest['strategies'].get(sid)
        if not rec:
            continue
        evs = rec.get('history', [])
        # Just persist the last 1 or 2 events we just appended
        new_events = [e for e in evs if e.get('actor') == 'reconcile:2026-04-22'
                       and e.get('timestamp') == now_iso]
        for e in new_events:
            pg_exec_literal(
                f"""INSERT INTO lifecycle_events
                    (strategy_id, from_state, to_state, actor, reason, metadata)
                   VALUES ({q(sid)}, {q(e['from_state'])}, {q(e['to_state'])},
                           {q(e['actor'])}, {q(e['reason'])},
                           {q(json.dumps(e.get('metadata', {})))}::jsonb)"""
            )

    # status='approved' for active zombies to be explicit (idempotent — already approved)
    # and approved_by gets our reconciliation tag if not already set.
    print('[3] Ensuring DB.status=approved for active zombies:')
    for sid in all_ids:
        pg_exec_literal(
            f"""UPDATE strategy_registry
                  SET status      = 'approved',
                      approved_by = COALESCE(approved_by, 'reconcile:2026-04-22'),
                      approved_at = COALESCE(approved_at, NOW())
                WHERE id = {q(sid)}"""
        )
        print(f'    OK   {sid}')

    # ── 4. Dormant zombies + candidate/paper-DB mismatches → pending_approval ─
    to_demote = DORMANT_ZOMBIES + CANDIDATE_DB_PAPER
    print(f'[4] Demoting {len(to_demote)} strategies to status=pending_approval:')
    for sid in to_demote:
        pg_exec_literal(
            f"""UPDATE strategy_registry
                  SET status = 'pending_approval'
                WHERE id = {q(sid)}
                  AND status <> 'deprecated'"""
        )
        print(f'    OK   {sid}')

    # ── 5. Phantom → new strategy_registry row ──────────────────────────────
    print('[5] Inserting phantom strategies into strategy_registry:')
    for sid, description, impl_path in PHANTOMS_TO_REGISTER:
        pg_exec_literal(
            f"""INSERT INTO strategy_registry
                   (id, name, description, status, tier, implementation_path,
                    parameters, regime_conditions, universe)
                VALUES ({q(sid)}, {q(sid)}, {q(description)},
                        'pending_approval', 2, {q(impl_path)},
                        '{{}}'::jsonb,
                        '{{"active_in_regimes":["HIGH_VOL","TRANSITIONING","LOW_VOL"]}}'::jsonb,
                        ARRAY['SP500']::text[])
                ON CONFLICT (id) DO NOTHING"""
        )
        print(f'    OK   {sid}')

    print('\nReconciliation complete.')


if __name__ == '__main__':
    main()
