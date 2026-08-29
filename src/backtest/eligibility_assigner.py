#!/usr/bin/env python3
"""eligibility_assigner.py — auto-set strategy.metadata.eligible_regimes
from the latest unified_backtest output.

Rule (2026-07-13 v2 — aligned with the shared per-regime qualification gate
in regime_qualification.py / promotion_service.js; sharpe/trades env-tunable):
    eligible_regimes = { r ∈ CANONICAL_REGIMES
                         |  sharpe[r] > MIN_SHARPE          (default 0.0, STRICT)
                         AND trade_count[r] >= MIN_TRADES   (default 100)
                         AND max_dd_pct[r] <= class ceiling (equity/etp 20,
                             option 30, crypto 70 — from PROMOTION_THRESHOLDS) }

If the strategy fired no regime that clears the bar, the previous
eligible_regimes value is preserved (refuse to wipe to empty — that
would zero the sizer's weight contribution unsafely). Operator gets a
WARN line.

Writes:
  - Updates manifest.strategies[strategy_id].metadata.eligible_regimes
  - Logs the prior → new diff to stdout (and to lifecycle_events if conn
    is available, so the dashboard surfaces the change)

CLI:
  python3 -m backtest.eligibility_assigner --strategy-id S_xxx
  python3 -m backtest.eligibility_assigner --all
  python3 -m backtest.eligibility_assigner --dry-run  # report, don't write
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

CANONICAL_REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')

MIN_SHARPE_DEFAULT  = float(os.environ.get('ELIGIBILITY_MIN_SHARPE', '0.0'))
MIN_TRADES_DEFAULT  = int(os.environ.get('ELIGIBILITY_MIN_TRADES', '100'))

from backtest.regime_qualification import class_thresholds, dd_leg_passes  # noqa: E402


def _instrument_class_map() -> dict:
    """sid → instrument_class from the manifest (default equity)."""
    try:
        m = json.loads((ROOT / 'src' / 'strategies' / 'manifest.json').read_text())
        return {sid: (rec.get('instrument_class') or 'equity')
                for sid, rec in (m.get('strategies') or {}).items()}
    except Exception:
        return {}


def _log(msg: str) -> None:
    print(f'[eligibility_assigner] {msg}', flush=True)


def compute_eligible(conn, strategy_id: str,
                     min_sharpe: float = MIN_SHARPE_DEFAULT,
                     min_trades: int = MIN_TRADES_DEFAULT,
                     instrument_class: str = 'equity') -> tuple[list[str], dict]:
    """Return (eligible_regimes_list, per_regime_diagnostics_dict).

    Reads the latest primary_window=true run for the strategy. If no run
    exists, returns ([], {}).
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("""
        SELECT run_id FROM strategy_backtest_runs
        WHERE strategy_id = %s AND primary_window = TRUE
        ORDER BY run_at DESC LIMIT 1
    """, (strategy_id,))
    row = cur.fetchone()
    if not row:
        return [], {}
    run_id = row['run_id']
    cur.execute("""
        SELECT regime_state, sharpe, trade_count, max_dd_pct, calmar
        FROM strategy_backtest_regimes
        WHERE run_id = %s
    """, (run_id,))
    gate = class_thresholds(instrument_class)
    diag: dict[str, dict] = {}
    eligible: list[str] = []
    for r in cur:
        s     = r['sharpe']
        n     = r['trade_count'] or 0
        dd    = r.get('max_dd_pct') if hasattr(r, 'get') else r['max_dd_pct']
        cal   = r.get('calmar') if hasattr(r, 'get') else r['calmar']
        # Sharpe is a STRICT exceed (policy 2026-07-13 v2: "positive Sharpe");
        # the DD leg is the class ceiling OR the Calmar escape hatch under the
        # catastrophic hard cap (2026-07-27).
        legacy_pass = (s is not None and dd is not None
                      and s > min_sharpe and n >= min_trades
                      and dd_leg_passes(dd, cal, gate))
        passes = legacy_pass
        diag[r['regime_state']] = {
            'sharpe':      s,
            'trade_count': n,
            'max_dd_pct':  dd,
            'eligible':    passes,
        }
        if passes:
            eligible.append(r['regime_state'])
    # Stable canonical ordering
    eligible_sorted = [r for r in CANONICAL_REGIMES if r in eligible]
    return eligible_sorted, diag


def update_manifest(strategy_id: str, new_eligible: list[str],
                    dry_run: bool = False) -> Optional[list[str]]:
    """Patch manifest.json in place. Returns the prior eligible_regimes list,
    or None if the strategy isn't in the manifest yet."""
    manifest_path = ROOT / 'src' / 'strategies' / 'manifest.json'
    m = json.loads(manifest_path.read_text())
    entries = m.get('strategies') or {}
    if strategy_id not in entries:
        return None
    entry = entries[strategy_id]
    meta  = entry.setdefault('metadata', {}) if not dry_run else dict(entry.get('metadata') or {})
    prior = list(meta.get('eligible_regimes') or [])
    if dry_run:
        return prior
    meta['eligible_regimes'] = new_eligible
    from datetime import datetime, timezone
    m['updated_at'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    manifest_path.write_text(json.dumps(m, indent=2) + '\n')
    return prior


def apply_one(strategy_id: str, conn, dry_run: bool = False,
              min_sharpe: float = MIN_SHARPE_DEFAULT,
              min_trades: int = MIN_TRADES_DEFAULT,
              instrument_class: str = 'equity') -> dict:
    """Apply eligibility derivation to one strategy. Returns summary dict
    with keys: status ('ok'|'no_run'|'no_pass'|'no_manifest'), eligible,
    prior, diag.
    """
    eligible, diag = compute_eligible(conn, strategy_id, min_sharpe, min_trades,
                                      instrument_class=instrument_class)
    if not diag:
        return {'status': 'no_run', 'eligible': [], 'prior': None, 'diag': {}}
    if not eligible:
        # Refuse to wipe. Preserve prior; this is a soft-warn condition.
        prior = update_manifest(strategy_id, [], dry_run=True)
        return {'status': 'no_pass', 'eligible': [], 'prior': prior, 'diag': diag}
    prior = update_manifest(strategy_id, eligible, dry_run=dry_run)
    if prior is None:
        return {'status': 'no_manifest', 'eligible': eligible, 'prior': None, 'diag': diag}
    return {'status': 'ok', 'eligible': eligible, 'prior': prior, 'diag': diag}


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--strategy-id')
    g.add_argument('--all', action='store_true',
                   help='Run for every strategy with a primary_window backtest')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--min-sharpe', type=float, default=MIN_SHARPE_DEFAULT)
    ap.add_argument('--min-trades', type=int,   default=MIN_TRADES_DEFAULT)
    args = ap.parse_args()

    uri = os.environ.get('POSTGRES_URI')
    if not uri:
        _log('POSTGRES_URI not set'); return 1
    conn = psycopg2.connect(uri)

    classes = _instrument_class_map()

    if args.strategy_id:
        result = apply_one(args.strategy_id, conn, dry_run=args.dry_run,
                           min_sharpe=args.min_sharpe, min_trades=args.min_trades,
                           instrument_class=classes.get(args.strategy_id, 'equity'))
        diag_str = ', '.join(f'{r}: sharpe={d["sharpe"]} n={d["trade_count"]}{"✓" if d["eligible"] else ""}'
                             for r, d in result['diag'].items())
        _log(f'{args.strategy_id}: status={result["status"]} prior={result["prior"]} '
             f'new={result["eligible"]} [{diag_str}]')
        return 0

    if args.all:
        cur = conn.cursor()
        cur.execute("""SELECT DISTINCT strategy_id FROM strategy_backtest_runs
                       WHERE primary_window = TRUE""")
        sids = [r[0] for r in cur]
        _log(f'applying eligibility to {len(sids)} strategies (dry-run={args.dry_run})')
        n_changed = n_nopass = n_unchanged = 0
        for sid in sids:
            result = apply_one(sid, conn, dry_run=args.dry_run,
                               min_sharpe=args.min_sharpe, min_trades=args.min_trades,
                               instrument_class=classes.get(sid, 'equity'))
            if result['status'] == 'ok':
                if set(result['eligible']) != set(result['prior'] or []):
                    n_changed += 1
                    _log(f'  CHANGE {sid}: {result["prior"]} → {result["eligible"]}')
                else:
                    n_unchanged += 1
            elif result['status'] == 'no_pass':
                n_nopass += 1
                _log(f'  WARN  {sid}: no regime clears sharpe>={args.min_sharpe}+trades>={args.min_trades}; '
                     f'preserved prior={result["prior"]}')
        _log(f'done: changed={n_changed} unchanged={n_unchanged} no_pass={n_nopass}')
        return 0

    return 1


if __name__ == '__main__':
    sys.exit(main())
