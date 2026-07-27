#!/usr/bin/env python3
"""src/backtest/activation_assigner.py — Strategy Activation Slider backend.

Derives per-(strategy, regime) SIZER eligibility (`strategy_regime_params.
eligible` — the column `strategy_weights._load_active_strategies` reads)
from each strategy's latest `primary_window=TRUE` unified_backtest run.

Design: docs/archive/superpowers/specs/2026-07-05-strategy-activation-slider-design.md
Operator directive (2026-07-13 v2, supersedes 2026-07-05):
  eligible[r] = QUALIFIES[r] AND sharpe[r] >= threshold
  where QUALIFIES[r] is the shared per-regime promotion/activation rule
  (backtest.regime_qualification / promotion_service.js judgeRegimeSleeve):
      sharpe[r] STRICTLY > 0
      AND max_dd_pct[r] <= class ceiling (equity/etp 20, option 30, crypto 70)
      AND trade_count[r] >= 100
  threshold = pipeline_config.strategy_activation_min_sharpe (dashboard-
              controlled; default 0.5; read fresh every run; fail-safe to
              0.5 on any read error / missing key / malformed value).
  Slider at 0 therefore activates a strategy in exactly its QUALIFYING
  regimes; a higher slider only narrows within that set. Instrument class
  comes from the manifest (default equity).

Unlike the legacy manifest-writing `eligibility_assigner` (which REFUSES to
wipe `eligible_regimes` to empty), this deriver is authoritative for the
LIVE sizer store and DOES set all 4 canonical regimes to eligible=FALSE
when zero regimes clear the bar (auto-dormancy, operator-approved). Manifest
/ registry status is left completely untouched — a strategy can sit
`state=live` with all 4 regimes dormant simultaneously until a later
re-backtest lifts a regime back over threshold.

Only strategies with a latest `primary_window=TRUE` run that ALSO has
>=1 `strategy_backtest_regimes` child row are touched. A strategy with a
`primary_window` row but zero regime rows (malformed/partial backtest
write) is treated the same as "no run at all": SKIPPED, not wiped. A
strategy with no `primary_window` run whatsoever is likewise skipped.
Skipped strategies' `strategy_regime_params` rows are left byte-identical.

Upsert pattern matched verbatim from `src/strategies/eligibility_manager.
py` (~line 158, `set_params`) and `src/channels/api/server.js` (~line
1595, the picker's `strategy_regime_params` sync): same 4-column INSERT +
`ON CONFLICT (strategy_id, regime_state) DO UPDATE SET eligible, set_at,
set_by`, paired with an audit row in `strategy_regime_param_changes`. The
no-op write guard mirrors server.js's `if (before && before.eligible ===
isElig) continue;` EXACTLY: a write is skipped only when a prior row
EXISTS and already matches — a strategy/regime with no prior row always
gets an explicit write (even eligible=False) so eligibility is fully
determined for all 4 canonical regimes, never left implicit-by-omission.

CLI:
  python3 -m backtest.activation_assigner --strategy-id S_xxx [--dry-run] [--min-sharpe X] [--notify]
  python3 -m backtest.activation_assigner --all [--dry-run] [--min-sharpe X] [--notify]

--dry-run computes the full prior->new diff and prints it but performs NO
database writes whatsoever (the write helper is never called).
--notify (default OFF, so tests/dry-runs stay silent) posts the newly-
dormant + net-change summary to #botjohn-log via the existing webhook
lookup pattern (`agent_registry.webhook_urls->>'botjohn-log'`), mirroring
`regime_blended_sizer._post_corr_cumsharpe_log` / `fold_report.py`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

from backtest.regime_qualification import class_thresholds, dd_leg_passes  # noqa: E402

CANONICAL_REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')

CONFIG_KEY = 'strategy_activation_min_sharpe'
DEFAULT_MIN_SHARPE = 0.5
# min_trades resolution order (most specific wins):
#   explicit min_trades= / --min-trades  >  pipeline_config  >  class gate (100)
# The pipeline_config key makes the sample-size floor dashboard-adjustable, like
# its min-Sharpe sibling (operator directive 2026-07-16): it was previously
# reachable only from the shared per-regime gate (regime_qualification
# class_thresholds → 100) or a CLI flag, so tuning it needed a code change.
# The old fixed MIN_TRADES=20 is retired.
CONFIG_KEY_MIN_TRADES = 'strategy_activation_min_trades'

ACTOR = 'activation_assigner'


def _load_instrument_classes() -> dict:
    """sid → instrument_class from the manifest (default equity). Read once
    per process; a missing/unreadable manifest degrades to all-equity, which
    only loosens nothing (equity has the tightest DD ceiling)."""
    try:
        m = json.loads((ROOT / 'src' / 'strategies' / 'manifest.json').read_text())
        return {sid: (rec.get('instrument_class') or 'equity')
                for sid, rec in (m.get('strategies') or {}).items()}
    except Exception:
        return {}


def _log(msg: str) -> None:
    print(f'[activation_assigner] {msg}', flush=True)


# ── Config accessor ─────────────────────────────────────────────────────────
# Co-located with the deriver (mirrors strategy_weights._get_bt_sharpe_cap's
# placement next to its sole consumer). A future dashboard slider reads the
# same pipeline_config key directly — no separate module needed for that.
def get_activation_threshold(cur) -> float:
    """Read strategy_activation_min_sharpe from pipeline_config; default 0.5.
    Fail-safe: missing row / malformed value / query error all fall back to
    0.5 (mirrors strategy_weights._get_bt_sharpe_cap's fail-safe pattern).
    Callers MUST NOT rely on the connection's transaction state after a
    failure here without rolling back first — see main()'s explicit
    conn.rollback() immediately after calling this, which guarantees a
    clean transaction before the write loop regardless of outcome (2026-
    05-16 tier-1 lesson: an uncaught exception on a SELECT like this left
    the connection aborted for every subsequent statement in
    strategy_weights.py until an explicit rollback was added)."""
    try:
        cur.execute("SELECT value FROM pipeline_config WHERE key=%s", (CONFIG_KEY,))
        row = cur.fetchone()
        if row and row[0] is not None:
            return float(row[0])
    except Exception:
        pass
    return DEFAULT_MIN_SHARPE


def get_activation_min_trades(cur) -> Optional[int]:
    """Read strategy_activation_min_trades from pipeline_config.

    Returns None when unset/unreadable, which makes compute_eligible fall back to
    the per-class gate (regime_qualification.class_thresholds → 100). Fail-SAFE
    direction matters: a malformed value must defer to the class floor, never
    loosen to 0 — a 0 floor would activate regimes on a 1-trade sample.

    0 IS honoured when explicitly configured (a deliberate "no sample floor"),
    which is why this returns Optional[int] rather than using 0 as the sentinel.
    Negative values are rejected (they would activate everything).

    Same transaction caveat as get_activation_threshold: callers must rollback
    after a failure before further statements.
    """
    try:
        cur.execute("SELECT value FROM pipeline_config WHERE key=%s", (CONFIG_KEY_MIN_TRADES,))
        row = cur.fetchone()
        if row and row[0] is not None:
            n = int(row[0])
            if n >= 0:
                return n
    except Exception:
        pass
    return None


# ── Eligibility computation ─────────────────────────────────────────────────
def compute_eligible(conn, strategy_id: str, threshold: float,
                     min_trades: Optional[int] = None,
                     instrument_class: str = 'equity') -> tuple[Optional[dict], dict]:
    """Return (eligible_by_regime, diag) for strategy_id's latest
    primary_window=TRUE run.

    eligible_by_regime: dict of ALL 4 CANONICAL_REGIMES -> bool, fully
    determined (a regime absent from strategy_backtest_regimes counts as
    not-eligible — no observed sharpe/trades). None if the strategy has no
    primary_window run, OR the run has zero regime rows — caller MUST skip
    (not touch strategy_regime_params) in either case.

    diag: per-regime {sharpe, trade_count, eligible} for every regime row
    actually present in strategy_backtest_regimes (used for the prior->new
    diff report and the audit-row bt_sharpe_after/bt_n_trades columns).
    """
    gate = class_thresholds(instrument_class)
    eff_min_trades = gate['min_trades'] if min_trades is None else min_trades
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("""
        SELECT run_id FROM strategy_backtest_runs
        WHERE strategy_id = %s AND primary_window = TRUE
        ORDER BY run_at DESC LIMIT 1
    """, (strategy_id,))
    row = cur.fetchone()
    if not row:
        return None, {}
    run_id = row['run_id']
    # Universe ladder campaign W3 (2026-07-21): when the strategy's live
    # universe is a ladder tier chosen by the shrink pass, judge eligibility
    # on THAT tier's sleeves (universe_shrink_metrics chosen rows) — the
    # full-universe strategy_backtest_regimes sleeves describe a universe the
    # strategy no longer trades. Falls back to the full-run sleeves when no
    # chosen rows exist (no shrink yet / non-ladder predicate).
    cur.execute("""
        SELECT regime_state, sharpe, trade_count, max_dd_pct, calmar
        FROM universe_shrink_metrics
        WHERE run_id = %s AND chosen AND regime_state <> 'TOTAL'
    """, (run_id,))
    rows = cur.fetchall()
    if rows:
        _log(f'{strategy_id}: eligibility from chosen shrink sleeves '
             f'({len(rows)} regimes)')
    else:
        cur.execute("""
            SELECT regime_state, sharpe, trade_count, max_dd_pct, calmar
            FROM strategy_backtest_regimes
            WHERE run_id = %s
        """, (run_id,))
        rows = cur.fetchall()
    diag: dict[str, dict] = {}
    for r in rows:
        s = r['sharpe']
        n = r['trade_count'] if r['trade_count'] is not None else 0
        dd = r['max_dd_pct']
        # .get: tolerate legacy fixture rows without a calmar key — missing
        # calmar only forfeits the DD escape hatch (dd_leg_passes contract).
        cal = r.get('calmar') if hasattr(r, 'get') else r['calmar']
        # QUALIFIES (shared per-regime gate: >0 sharpe, class DD leg — flat
        # ceiling OR Calmar escape hatch under the hard cap (2026-07-27) —
        # trade floor) AND the slider's sharpe dial on top.
        passes = (s is not None and dd is not None
                  and s > gate['min_sharpe']
                  and dd_leg_passes(dd, cal, gate)
                  and n >= eff_min_trades
                  and s >= threshold)
        diag[r['regime_state']] = {'sharpe': s, 'trade_count': n,
                                   'max_dd_pct': dd, 'calmar': cal,
                                   'eligible': passes}
    if not diag:
        # primary_window run exists but has zero strategy_backtest_regimes
        # rows (malformed/partial write) -- treat identically to "no run":
        # skip, don't wipe.
        return None, {}
    eligible_by_regime = {r: diag.get(r, {}).get('eligible', False) for r in CANONICAL_REGIMES}
    return eligible_by_regime, diag


def _load_prior(conn, strategy_id: str) -> dict:
    """Return {regime_state: {eligible, size_scalar, stop_pct, target_pct,
    max_hold_days}} for every strategy_regime_params row that currently
    exists for this strategy. A regime with NO row is simply absent from
    the dict (distinguishes 'never set' from 'set False')."""
    cur = conn.cursor()
    cur.execute("""
        SELECT regime_state, eligible, size_scalar, stop_pct, target_pct, max_hold_days
        FROM strategy_regime_params
        WHERE strategy_id = %s
    """, (strategy_id,))
    out: dict[str, dict] = {}
    for regime, eligible, size_scalar, stop_pct, target_pct, max_hold_days in cur.fetchall():
        out[regime] = {
            'eligible': eligible,
            'size_scalar': float(size_scalar) if size_scalar is not None else None,
            'stop_pct': float(stop_pct) if stop_pct is not None else None,
            'target_pct': float(target_pct) if target_pct is not None else None,
            'max_hold_days': max_hold_days,
        }
    return out


def _classify_action(before_eligible: Optional[bool], new_eligible: bool) -> str:
    """Classify one (before, after) eligible transition.

    'unchanged' ONLY when a prior row exists and its value already matches
    (mirrors the DB-write no-op skip below exactly). A regime with no
    prior row is 'initialized' when the computed value is False (still
    gets an explicit write so eligibility is fully determined) or
    'activated' when the computed value is True."""
    if before_eligible is not None and before_eligible == new_eligible:
        return 'unchanged'
    if before_eligible is True and new_eligible is False:
        return 'deactivated'
    if before_eligible in (False, None) and new_eligible is True:
        return 'activated'
    return 'initialized'


def _apply_regime(cur, strategy_id: str, regime_state: str, new_eligible: bool,
                  prior: dict, sharpe: Optional[float], trade_count: Optional[int],
                  threshold: float, min_trades: int,
                  max_dd_pct: Optional[float] = None,
                  instrument_class: str = 'equity') -> str:
    """Write one (strategy, regime) row IFF it actually changes (or has no
    prior row yet). Returns the action taken/would-be-taken. Exact upsert
    pattern matched from eligibility_manager.py / server.js: 4-column
    INSERT + ON CONFLICT (strategy_id, regime_state) DO UPDATE SET
    eligible/set_at/set_by, preserving size_scalar/stop_pct/target_pct/
    max_hold_days verbatim (this deriver owns ONLY the eligible flag)."""
    before = prior.get(regime_state)
    before_eligible = before['eligible'] if before is not None else None
    action = _classify_action(before_eligible, new_eligible)
    if action == 'unchanged':
        return action

    def _row_json(eligible_val):
        return json.dumps({
            'strategy_id': strategy_id, 'regime_state': regime_state,
            'eligible': eligible_val,
            'size_scalar': before['size_scalar'] if before else None,
            'stop_pct': before['stop_pct'] if before else None,
            'target_pct': before['target_pct'] if before else None,
            'max_hold_days': before['max_hold_days'] if before else None,
        })

    before_json = _row_json(before_eligible) if before is not None else None
    after_json = _row_json(new_eligible)
    reason = (f'activation_assigner: sharpe={sharpe} n={trade_count} '
             f'dd={max_dd_pct} class={instrument_class} '
             f'threshold={threshold} min_trades={min_trades} '
             f'rule=qualifies(>0·classDD·trades)+slider')

    cur.execute("""
        INSERT INTO strategy_regime_param_changes
            (actor, strategy_id, regime_state, before_row, after_row,
             reason, source, bt_sharpe_after, bt_n_trades)
        VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s)
    """, (ACTOR, strategy_id, regime_state, before_json, after_json,
          reason, ACTOR, sharpe, trade_count))
    cur.execute("""
        INSERT INTO strategy_regime_params
            (strategy_id, regime_state, eligible, set_by)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (strategy_id, regime_state) DO UPDATE
           SET eligible = EXCLUDED.eligible,
               set_at   = NOW(),
               set_by   = EXCLUDED.set_by
    """, (strategy_id, regime_state, new_eligible, ACTOR))
    return action


def apply_one(conn, strategy_id: str, threshold: float,
             dry_run: bool = False, min_trades: Optional[int] = None,
             instrument_class: str = 'equity') -> dict:
    """Compute + (unless dry_run) write eligibility for one strategy.

    Returns: {status: 'ok'|'skipped_no_run', strategy_id, prior, new, diag,
    actions}. 'prior'/'new' are {regime_state: bool_or_None} (None = never
    set). 'actions' is {regime_state: action_str}. On 'skipped_no_run',
    prior/new/diag/actions are all {} and NOTHING is touched in the DB —
    not even a read of strategy_regime_params.
    """
    eligible_by_regime, diag = compute_eligible(conn, strategy_id, threshold, min_trades,
                                                instrument_class=instrument_class)
    if eligible_by_regime is None:
        return {'status': 'skipped_no_run', 'strategy_id': strategy_id,
                'prior': {}, 'new': {}, 'diag': {}, 'actions': {}}

    prior_rows = _load_prior(conn, strategy_id)
    prior_eligible = {r: (prior_rows[r]['eligible'] if r in prior_rows else None)
                      for r in CANONICAL_REGIMES}

    actions: dict[str, str] = {}
    if dry_run:
        for r in CANONICAL_REGIMES:
            actions[r] = _classify_action(prior_eligible[r], eligible_by_regime[r])
    else:
        cur = conn.cursor()
        try:
            eff_min_trades = class_thresholds(instrument_class)['min_trades'] if min_trades is None else min_trades
            for r in CANONICAL_REGIMES:
                d = diag.get(r, {})
                actions[r] = _apply_regime(
                    cur, strategy_id, r, eligible_by_regime[r], prior_rows,
                    sharpe=d.get('sharpe'), trade_count=d.get('trade_count'),
                    threshold=threshold, min_trades=eff_min_trades,
                    max_dd_pct=d.get('max_dd_pct'), instrument_class=instrument_class)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()

    return {'status': 'ok', 'strategy_id': strategy_id,
            'prior': prior_eligible, 'new': eligible_by_regime,
            'diag': diag, 'actions': actions}


# ── Discord notify (best-effort, never raises) ──────────────────────────────
def _notify_botjohn_log(summary: str, newly_dormant: list, dry_run: bool) -> None:
    """Best-effort post to #botjohn-log. Mirrors regime_blended_sizer.py's
    _post_corr_cumsharpe_log / fold_report.py's webhook pattern: look up
    agent_registry.webhook_urls->>'botjohn-log', POST via urllib with an
    explicit User-Agent (Discord's Cloudflare edge 403s the default
    python-urllib/* UA). NEVER raises — a Discord hiccup must not fail the
    weekly eligibility refresh or a manual CLI run."""
    try:
        url = None
        with psycopg2.connect(os.environ['POSTGRES_URI']) as c, c.cursor() as cur:
            cur.execute("SELECT webhook_urls->>'botjohn-log' FROM agent_registry "
                        "WHERE webhook_urls->>'botjohn-log' IS NOT NULL LIMIT 1")
            row = cur.fetchone()
            url = row[0] if row else None
        if not url:
            _log('notify: no botjohn-log webhook URL found; skipping')
            return
        import urllib.request as _ur
        prefix = '[DRY-RUN] ' if dry_run else ''
        lines = [prefix + summary]
        if newly_dormant:
            lines.append('Newly dormant: ' + ', '.join(newly_dormant))
        content = '\n'.join(lines)[:1900]
        req = _ur.Request(
            url, data=json.dumps({'content': content}).encode(), method='POST',
            headers={'Content-Type': 'application/json',
                     'User-Agent': 'OpenClaw-ActivationAssigner/1.0 (+botjohn)'})
        _ur.urlopen(req, timeout=10).read()
    except Exception as e:
        _log(f'notify: webhook post skipped ({e})')


# ── CLI ──────────────────────────────────────────────────────────────────────
def _fmt_diff(result: dict) -> str:
    parts = []
    for r in CANONICAL_REGIMES:
        action = result['actions'].get(r, 'unchanged')
        suffix = f' ({action})' if action != 'unchanged' else ''
        parts.append(f"{r}: {result['prior'].get(r)}->{result['new'].get(r)}{suffix}")
    return ', '.join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--strategy-id')
    g.add_argument('--all', action='store_true',
                   help='Run for every strategy with a primary_window backtest')
    ap.add_argument('--dry-run', action='store_true',
                    help='Compute + print prior->new diff only; NO writes')
    ap.add_argument('--min-sharpe', type=float, default=None,
                    help='Override the pipeline_config threshold for this run')
    ap.add_argument('--min-trades', type=int, default=None,
                    help='Override the per-class gate trade floor (default: '
                         'regime_qualification class_thresholds, 100)')
    ap.add_argument('--notify', action='store_true',
                    help="Post the newly-dormant + net-change summary to "
                         "#botjohn-log (default off so tests/dry-runs stay silent)")
    args = ap.parse_args()

    uri = os.environ.get('POSTGRES_URI') or os.environ.get('DATABASE_URL')
    if not uri:
        _log('POSTGRES_URI not set'); return 1
    conn = psycopg2.connect(uri)

    if args.min_sharpe is not None:
        threshold = args.min_sharpe
    else:
        cur0 = conn.cursor()
        threshold = get_activation_threshold(cur0)
        cur0.close()
        # Guarantee a clean transaction before the write loop regardless of
        # whether the config read above succeeded or raised.
        conn.rollback()

    # min_trades: explicit --min-trades > pipeline_config > per-class gate (None
    # here → compute_eligible applies class_thresholds). Resolved once per run so
    # the dashboard slider takes effect without a deploy, mirroring min-Sharpe.
    resolved_min_trades = args.min_trades
    if resolved_min_trades is None:
        cur1 = conn.cursor()
        resolved_min_trades = get_activation_min_trades(cur1)
        cur1.close()
        conn.rollback()   # same clean-transaction guarantee as above

    if args.strategy_id:
        sids = [args.strategy_id]
    else:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT strategy_id FROM strategy_backtest_runs WHERE primary_window = TRUE")
        sids = sorted(r[0] for r in cur.fetchall())
        cur.close()

    classes = _load_instrument_classes()
    # NOTE: header + summary line formats are PINNED by activation_preview.js
    # (HEADER_RE / SUMMARY_RE, consumed by the dashboard dry-run endpoint) —
    # keep `threshold=… min_trades=… dry_run=… strategies=…` byte-stable.
    # min_trades is uniform across classes (gate floor 100), so one number
    # remains printable even though the rule is class-aware.
    eff_min_trades = (resolved_min_trades if resolved_min_trades is not None
                      else class_thresholds('equity')['min_trades'])
    _log(f'threshold={threshold} min_trades={eff_min_trades} dry_run={args.dry_run} '
        f'strategies={len(sids)}')

    results = []
    n_errors = 0
    for sid in sids:
        try:
            r = apply_one(conn, sid, threshold, dry_run=args.dry_run,
                          min_trades=resolved_min_trades,
                          instrument_class=classes.get(sid, 'equity'))
        except Exception as e:
            _log(f'  ERROR {sid}: {e}')
            try:
                conn.rollback()
            except Exception:
                pass
            n_errors += 1
            continue
        results.append(r)
        if r['status'] == 'skipped_no_run':
            _log(f'  SKIP  {sid}: no primary_window backtest with regime rows')
            continue
        _log(f'  {sid}: {_fmt_diff(r)}')

    n_ok = sum(1 for r in results if r['status'] == 'ok')
    n_skipped = sum(1 for r in results if r['status'] == 'skipped_no_run')
    activated_cells = deactivated_cells = 0
    newly_dormant: list = []
    for r in results:
        if r['status'] != 'ok':
            continue
        for x in CANONICAL_REGIMES:
            if r['actions'].get(x) == 'activated':
                activated_cells += 1
            elif r['actions'].get(x) == 'deactivated':
                deactivated_cells += 1
        was_active = any(r['prior'].get(x) for x in CANONICAL_REGIMES)
        is_active = any(r['new'].get(x) for x in CANONICAL_REGIMES)
        if was_active and not is_active:
            newly_dormant.append(r['strategy_id'])

    summary = (
        f'activation_assigner summary: {n_ok} strategies evaluated, '
        f'{n_skipped} skipped (no corrected backtest), '
        f'{activated_cells} cell(s) activated, {deactivated_cells} cell(s) deactivated, '
        f'{len(newly_dormant)} newly-dormant strateg{"y" if len(newly_dormant) == 1 else "ies"}'
        + (f' ({", ".join(newly_dormant)})' if newly_dormant else '')
        + f', threshold={threshold}, min_trades={eff_min_trades}, dry_run={args.dry_run}, errors={n_errors}'
    )
    _log(summary)

    if args.notify:
        _notify_botjohn_log(summary, newly_dormant, args.dry_run)

    conn.close()
    return 1 if n_errors else 0


if __name__ == '__main__':
    sys.exit(main())
