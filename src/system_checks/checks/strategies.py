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


_IMPL_DIR = ROOT / 'src' / 'strategies' / 'implementations'


@check(name='manifest_canonical_file_consistency', tags=['strategies'], requires=['fs'])
def _manifest_canonical_file_consistency():
    """Every manifest strategy's `metadata.canonical_file` must point at an
    actual .py file in src/strategies/implementations/. Drift causes the
    orchestrator's `_resolveImplPath` to return a missing path → validator
    emits `Contract validation failed — File not found` → strategy gets
    quarantined as `validation_failed` despite working code.

    Sat 2026-05-29 incident: strategycoder.md template used uppercase `S_XX...`
    for the manifest key but lowercase `s_xx....py` for canonical_file. The LLM
    made inconsistent choices file-by-file; 9 strategies ended up stuck this way
    before the prompt was tightened + `_resolveImplPath` got a case-toggle
    fallback (commit 7708d1b).

    This check reports any future drift. Repair with:
        python3 scripts/repair_manifest_canonical_files.py
    """
    if not MANIFEST.exists():
        return Status.SKIP, 'no manifest'
    m = json.loads(MANIFEST.read_text())
    case_drift = []   # file exists but with toggled-case basename
    truly_missing = []
    checked = 0
    for sid, rec in (m.get('strategies') or {}).items():
        cf = (rec.get('metadata') or {}).get('canonical_file')
        if not cf:
            continue
        checked += 1
        if (_IMPL_DIR / cf).is_file():
            continue
        # File doesn't exist at recorded path — try case-toggled
        toggled = cf[0].swapcase() + cf[1:] if cf else cf
        if (_IMPL_DIR / toggled).is_file():
            case_drift.append(f'{sid}: {cf!r} → {toggled!r}')
        else:
            truly_missing.append(f'{sid}: {cf!r}')
    if case_drift or truly_missing:
        msgs = []
        if case_drift:
            msgs.append(f'{len(case_drift)} case-drift (repairable): ' + '; '.join(case_drift[:3]))
        if truly_missing:
            msgs.append(f'{len(truly_missing)} truly-missing: ' + '; '.join(truly_missing[:3]))
        return Status.FAIL, ' | '.join(msgs)
    return Status.PASS, f'all {checked} canonical_file entries resolve cleanly'


@check(name='live_strategies_have_weights', tags=['strategies'], requires=['db', 'fs'])
def _live_strategies_have_weights():
    """Every manifest live/monitoring EQUITY-CLASS strategy must have at least
    one strategy_weights_by_regime row. The dashboard's candidate→live
    transition fire-and-forgets a `strategy_weights --rebuild`; if the spawn
    fails, the rebuild errors out, or per-regime backtest data never landed,
    the strategy joins the active stack but the sizer's `load_current(regime)`
    returns no row → strategy gets zero weight → no orders fire and nobody
    notices until they audit P&L.

    Crypto strategies (instrument_class='crypto') are sized by
    `execution.crypto_redeploy_sizer` against `crypto_regime_states`, NOT by
    `strategy_weights_by_regime`. Excluded here so this check stays a
    real-failure signal."""
    if not MANIFEST.exists():
        return Status.SKIP, 'no manifest'
    m = json.loads(MANIFEST.read_text())
    active_equity_class = {
        sid for sid, e in (m.get('strategies') or {}).items()
        if e.get('state') in ('live', 'monitoring')
        and e.get('instrument_class', 'equity') != 'crypto'
    }
    if not active_equity_class:
        return Status.SKIP, 'no live/monitoring equity-class strategies'
    with _pg() as conn, conn.cursor() as cur:
        cur.execute('SELECT DISTINCT strategy_id FROM strategy_weights_by_regime WHERE strategy_id = ANY(%s) AND is_current = TRUE',
                    (list(active_equity_class),))
        have = {r[0] for r in cur.fetchall()}
    missing = sorted(active_equity_class - have)
    if missing:
        return Status.FAIL, f'{len(missing)}/{len(active_equity_class)} live equity strategies missing from weights table — likely missing per-regime backtest data; rerun_backtest fixes it. Affected: {missing[:5]}'
    return Status.PASS, f'all {len(active_equity_class)} live/monitoring equity strategies have ≥1 weights row (crypto excluded)'


@check(name='live_strategies_have_cadence', tags=['strategies'], requires=['db', 'fs'])
def _live_strategies_have_cadence():
    """Every live strategy_weights_by_regime row must carry cadence_days > 0.
    A cadence of 0 means the sizer's `daily_weight = w / sqrt(cadence)` blows
    up and the strategy effectively sizes to zero (or NaN). Backstop for the
    cadence-bootstrap fix added 2026-05-29: if neither live_avg nor backtest
    avg_holding_days resolves, static_cad must still be ≥1."""
    with _pg() as conn, conn.cursor() as cur:
        cur.execute('''SELECT strategy_id, regime_state, cadence_days
                         FROM strategy_weights_by_regime
                        WHERE is_current = TRUE
                          AND (cadence_days IS NULL OR cadence_days < 1)''')
        bad = cur.fetchall()
    if bad:
        sample = [f'{sid}/{rgm}={cad}' for sid, rgm, cad in bad[:5]]
        return Status.FAIL, f'{len(bad)} rows with cadence_days < 1 or NULL: ' + '; '.join(sample)
    return Status.PASS, 'all weights rows have cadence_days ≥ 1'


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
