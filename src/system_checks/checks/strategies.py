"""Strategy checks — manifest loadability, signal hygiene."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import psycopg2

from ..registry import check
from ..types import Status

ROOT = Path('/root/openclaw')
MANIFEST = ROOT / 'src' / 'strategies' / 'manifest.json'


def _pg():
    return psycopg2.connect(os.environ['POSTGRES_URI'])


_ACTIVE_STATES = ('live', 'staging')


@check(name='all_active_strategies_importable', tags=['strategies'], requires=['fs'])
def _all_active_strategies_importable():
    """Every live/staging strategy in manifest.json has an entry in
    strategies.registry._IMPL_MAP and its module/class load cleanly."""
    sys.path.insert(0, str(ROOT / 'src'))
    if not MANIFEST.exists():
        return Status.FAIL, f'manifest not found at {MANIFEST}'
    manifest = json.loads(MANIFEST.read_text())
    try:
        from strategies.registry import _IMPL_MAP, load_strategy_class
    except Exception as e:
        return Status.FAIL, f'registry import failed: {e}'

    failures = []
    not_in_map = []
    n = 0
    for sid, rec in (manifest.get('strategies') or {}).items():
        if rec.get('state') not in _ACTIVE_STATES:
            continue
        n += 1
        if sid not in _IMPL_MAP:
            not_in_map.append(sid)
            continue
        cls = load_strategy_class(sid)
        if cls is None:
            failures.append(sid)
    if failures or not_in_map:
        msgs = []
        if failures:
            msgs.append(f'{len(failures)} failed to load: {failures[:3]}')
        if not_in_map:
            msgs.append(f'{len(not_in_map)} not in _IMPL_MAP: {not_in_map[:3]}')
        return Status.FAIL, '; '.join(msgs)
    return Status.PASS, f'all {n} live/staging strategies importable via registry'


@check(name='no_new_nan_signals_after_fix', tags=['strategies'], requires=['db'])
def _no_new_nan_signals_after_fix():
    """Engine-level NaN write-guard shipped 2026-05-21 (drops NaN/Inf signals at write_signals).
    Upstream price guards also added to S_pairs_trading_jump_diffusion_intraday and S_ptree_panel_tangency.
    Any NaN-priced signals dated AFTER 2026-05-21 indicate the guard regressed.
    Legacy rows (signal_date <= 2026-05-21) are accepted: they predate the guard,
    are immutable per master-DB rule, and are inert (handoff filters them)."""
    with _pg() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM execution_signals
            WHERE signal_date > DATE '2026-05-21'
              AND (entry_price = 'NaN'::numeric
                OR stop_loss   = 'NaN'::numeric
                OR target_1    = 'NaN'::numeric)
        """)
        n_new_nan = cur.fetchone()[0]
        cur.execute("""
            SELECT COUNT(*) FROM execution_signals
            WHERE entry_price = 'NaN'::numeric
               OR stop_loss   = 'NaN'::numeric
               OR target_1    = 'NaN'::numeric
        """)
        n_total_nan = cur.fetchone()[0]
    if n_new_nan == 0:
        return Status.PASS, f'0 new NaN signals since 2026-05-21 guard ({n_total_nan} legacy rows pre-guard)'
    return Status.FAIL, f'{n_new_nan} NaN signals dated after 2026-05-21 — guard regressed'


_ARCHIVED_STATES = {'decommissioned', 'deprecated', 'archived'}


@check(name='manifest_in_registry_impl_map', tags=['strategies'], requires=['fs'])
def _manifest_in_registry_impl_map():
    """Every live/staging manifest entry is registered in
    strategies.registry._IMPL_MAP. If a strategy is in the manifest as `live`
    but missing from the impl map, engine.py will silently skip it. Candidates
    and archived states are excluded."""
    if not MANIFEST.exists():
        return Status.SKIP, 'no manifest'
    manifest = json.loads(MANIFEST.read_text())
    sys.path.insert(0, str(ROOT / 'src'))
    try:
        from strategies.registry import _IMPL_MAP
    except Exception as e:
        return Status.FAIL, f'registry import failed: {e}'
    missing = []
    candidates_skipped = 0
    archived_skipped = 0
    for sid, rec in (manifest.get('strategies') or {}).items():
        state = rec.get('state', 'unknown')
        if state == 'candidate':
            candidates_skipped += 1
            continue
        if state in _ARCHIVED_STATES:
            archived_skipped += 1
            continue
        if sid not in _IMPL_MAP:
            missing.append(f'{sid} ({state})')
    if missing:
        return Status.FAIL, f'{len(missing)} live/staging not in _IMPL_MAP: {missing[:5]}'
    return Status.PASS, f'all live/staging entries registered ({candidates_skipped} candidates + {archived_skipped} archived skipped)'


@check(name='manifest_no_active_under_decommissioned', tags=['strategies'], requires=['fs'])
def _manifest_no_active_under_decommissioned():
    """An entry under m['decommissioned'] must NOT carry state='live',
    'candidate', or 'staging'. Sat 2026-05-16: 6 freshly-coded Tier-A
    strategies ended up under decommissioned with state='candidate',
    invisible to eligibility_assigner (which only reads m['strategies'])
    and breaking the unified-backtest <-> weights handshake silently."""
    if not MANIFEST.exists():
        return Status.SKIP, 'no manifest'
    m = json.loads(MANIFEST.read_text())
    decom = m.get('decommissioned') or {}
    bad = []
    for sid, rec in decom.items():
        state = (rec or {}).get('state', 'decommissioned')
        if state not in _ARCHIVED_STATES and state != 'decommissioned':
            bad.append(f'{sid}={state}')
    if bad:
        return Status.FAIL, f'{len(bad)} active strategies under decommissioned: ' + '; '.join(bad[:10])
    return Status.PASS, f'{len(decom)} decommissioned entries, all archived-state'
