#!/usr/bin/env python3
"""Universe ladder shrink driver — campaign W3 (2026-07-21).

Re-selects every strategy's universe by SHRINKING its stored full-universe
primary-run trades down the tier ladder (backtest.universe_shrink) — no
re-runs — then feeds the prefer-largest select_tier (W1) and persists:

  * universe_shrink_metrics rows (5 tiers × TOTAL+4 regimes, upserted)
  * a strategy_universe_recommendations row (candidate_set_id
    'shrink-1-<artifact run id>'; skipped if one already exists — resume-safe)
  * with --adopt: 'change' verdicts are adopted immediately
    (lifecycle_universe_adoption, actor auto:universe-shrink)
  * chosen flags: the tier the strategy ACTUALLY trades after this pass
    (adopted choice, else its current predicate when that is a ladder tier)
    — consumed by the activation assigner + dashboard.

Scope: manifest states live+candidate, instrument_class != crypto, with a
primary_window run. After adopting anything, re-run the activation assigner
(python3 -m backtest.activation_assigner --all) so per-regime eligibility is
re-derived from the CHOSEN tier's sleeves — the driver prints the reminder
(or does it inline with --reassign).

Usage:
  python3 scripts/run_universe_shrink.py [--strategy SID[,SID…]]
      [--adopt] [--reassign] [--dry-run] [--artifact PATH]
      [--states live,candidate] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest.universe_ladder_selection import LADDER_TIERS  # noqa: E402
from backtest import universe_ladder_recs as recs  # noqa: E402
from backtest.universe_shrink import (  # noqa: E402
    FULL_TIER, load_membership, mean_tier_sizes, shrink_and_select, db_rows,
)

MANIFEST = ROOT / 'src' / 'strategies' / 'manifest.json'
REGIMES_PARQUET = ROOT / 'data' / 'master' / 'historical_regimes.parquet'
GRID_KEYS = ('sharpe', 'max_dd_pct', 'win_rate', 'trades_n', 'sortino',
             'calmar', 'mean_holding_days', 'mean_universe_size')


def _pg():
    return psycopg2.connect(os.environ['POSTGRES_URI'])


def _current_predicate(entry: dict) -> str:
    ref = (entry.get('metadata') or {}).get('universe_filter_ref')
    return ref.rsplit(':', 1)[-1] if ref else 'sp500'


def ensure_artifact(args) -> tuple[Path, str]:
    if args.artifact:
        p = Path(args.artifact)
        return p, p.stem.replace('universe_tier_membership_', '')
    existing = sorted((ROOT / 'data').glob(
        'universe_tier_membership_shrink-*.parquet'))
    if existing:
        p = existing[-1]
        tag = p.stem.replace('universe_tier_membership_', '')
        # Reuse while fresh: an artifact whose build date is >35 days old
        # can't bucket the newest trades (its last month-end predates them),
        # so rebuild instead (research-pipeline W5 path runs unattended).
        try:
            built = date.fromisoformat(
                f'{tag[-8:-4]}-{tag[-4:-2]}-{tag[-2:]}')
            fresh = (date.today() - built).days <= 35
        except ValueError:
            fresh = True
        if fresh:
            return p, tag
    run_id = f'shrink-{date.today().strftime("%Y%m%d")}'
    print(f'[shrink] building membership artifact {run_id} '
          '(2016-03-01 → today; ~2 min)…')
    rc = subprocess.run(
        ['python3', 'scripts/build_tier_membership.py', '--run-id', run_id,
         '--start', '2016-03-01', '--end', date.today().isoformat()],
        cwd=str(ROOT)).returncode
    if rc != 0:
        raise SystemExit(f'membership build failed rc={rc}')
    return ROOT / 'data' / f'universe_tier_membership_{run_id}.parquet', run_id


def grid_summary(result: dict, window, verdict_name: str) -> dict:
    grid = []
    for t in (*LADDER_TIERS, FULL_TIER):
        m = result['metrics_by_tier'].get(t) or {}
        grid.append({'name': t, **{k: m.get(k) for k in GRID_KEYS}})
    return {'grid': grid, 'window': list(window), 'verdict': verdict_name,
            'candidate_set': list(LADDER_TIERS), 'mode': 'shrink',
            'maintained_regimes': result['verdict'].get('maintained_regimes', [])}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--strategy', help='comma-separated strategy ids')
    ap.add_argument('--adopt', action='store_true',
                    help="adopt 'change' verdicts immediately")
    ap.add_argument('--reassign', action='store_true',
                    help='re-run the activation assigner after adoptions')
    ap.add_argument('--dry-run', action='store_true',
                    help='compute + print only; no DB writes')
    ap.add_argument('--artifact')
    ap.add_argument('--states', default='live,candidate')
    ap.add_argument('--limit', type=int)
    args = ap.parse_args()

    import pandas as pd
    from backtest.unified_backtest import load_regimes
    from backtest.regime_blended_backtest import regime_day_frequency

    artifact, artifact_run_id = ensure_artifact(args)
    print(f'[shrink] artifact={artifact.name}')
    snaps, members = load_membership(artifact)
    regimes = load_regimes()
    day_freq = regime_day_frequency(REGIMES_PARQUET)
    candidate_set_id = f'shrink-1-{artifact_run_id}'

    manifest = json.loads(MANIFEST.read_text())['strategies']
    states = {s.strip() for s in args.states.split(',')}
    only = ({s.strip() for s in args.strategy.split(',')}
            if args.strategy else None)

    pg = _pg()
    with pg.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (strategy_id) strategy_id, run_id::text,
                   start_date, end_date
              FROM strategy_backtest_runs WHERE primary_window
             ORDER BY strategy_id, run_at DESC""")
        primaries = {r[0]: {'run_id': r[1], 'start': r[2], 'end': r[3]}
                     for r in cur.fetchall()}

    sids = []
    for sid, entry in sorted(manifest.items()):
        if only is not None and sid not in only:
            continue
        if only is None and entry.get('state') not in states:
            continue
        if entry.get('instrument_class') == 'crypto':
            continue
        if sid not in primaries:
            continue
        sids.append(sid)
    if args.limit:
        sids = sids[:args.limit]
    print(f'[shrink] strategies={len(sids)} candidate_set={candidate_set_id} '
          f'adopt={args.adopt} dry_run={args.dry_run}')

    changes, no_changes, adopted_ids, skipped = [], [], [], 0
    for sid in sids:
        run = primaries[sid]
        with pg.cursor() as cur:
            cur.execute("""SELECT 1 FROM strategy_universe_recommendations
                            WHERE strategy_id=%s AND candidate_set_id=%s""",
                        (sid, candidate_set_id))
            if cur.fetchone() and not args.dry_run:
                skipped += 1
                continue
            cur.execute("""
                SELECT ticker, entry_date, pnl_pct, holding_days, entry_regime
                  FROM strategy_backtest_trades WHERE run_id=%s""",
                        (run['run_id'],))
            trades = [dict(zip(('ticker', 'entry_date', 'pnl_pct',
                                'holding_days', 'entry_regime'), r))
                      for r in cur.fetchall()]

        w = (str(run['start']), str(run['end']))
        entry = manifest[sid]
        ic = entry.get('instrument_class') or 'equity'
        result = shrink_and_select(
            trades, snaps, members,
            regimes.loc[pd.Timestamp(w[0]):pd.Timestamp(w[1])], day_freq,
            instrument_class=ic,
            sizes=mean_tier_sizes(snaps, members, w[0], w[1]))
        verdict = result['verdict']
        current = _current_predicate(entry)
        if verdict['verdict'] == 'no_signal':
            choice, verdict_name = current, 'no_signal'
        else:
            choice = verdict['choice']
            verdict_name = 'no_change' if choice == current else 'change'
        rationale = ('shrink ' +
                     recs.build_rationale(verdict, window=w))

        line = (f"{sid}: {current} -> {choice} [{verdict_name}] "
                f"trades={len(trades)} "
                f"tier_n={ {t: (result['metrics_by_tier'][t] or {}).get('trades_n') for t in LADDER_TIERS} }")
        print('  ' + line)
        if args.dry_run:
            (changes if verdict_name == 'change' else no_changes).append(line)
            continue

        rows = db_rows(sid, run['run_id'], result,
                       candidate_set_id=candidate_set_id)
        with pg.cursor() as cur:
            execute_values(cur, """
                INSERT INTO universe_shrink_metrics
                  (strategy_id, run_id, tier, regime_state, sharpe, max_dd_pct,
                   trade_count, win_rate, sortino, calmar, mean_holding_days,
                   return_pct, candidate_set_id)
                VALUES %s
                ON CONFLICT (run_id, tier, regime_state) DO UPDATE SET
                  sharpe=EXCLUDED.sharpe, max_dd_pct=EXCLUDED.max_dd_pct,
                  trade_count=EXCLUDED.trade_count, win_rate=EXCLUDED.win_rate,
                  sortino=EXCLUDED.sortino, calmar=EXCLUDED.calmar,
                  mean_holding_days=EXCLUDED.mean_holding_days,
                  return_pct=EXCLUDED.return_pct,
                  candidate_set_id=EXCLUDED.candidate_set_id,
                  computed_at=NOW()""", rows, page_size=100)
        pg.commit()

        rec_id = recs.insert_recommendation(
            pg, strategy_id=sid, current_predicate=current,
            candidate_predicate=choice, candidate_set_id=candidate_set_id,
            backtest_summary=grid_summary(result, w, verdict_name),
            rationale=rationale)

        adopted = False
        if args.adopt and verdict_name == 'change':
            try:
                from src.strategies.lifecycle_universe_adoption import (
                    adopt_universe_recommendation)
                adopt_universe_recommendation(rec_id,
                                              actor='auto:universe-shrink')
                adopted = True
                adopted_ids.append(sid)
            except Exception as e:
                print(f'  [shrink] ADOPT FAILED {sid} rec={rec_id}: {e}')

        live_pred = choice if adopted else current
        with pg.cursor() as cur:
            cur.execute("""UPDATE universe_shrink_metrics
                              SET chosen=(tier=%s), rec_id=%s
                            WHERE run_id=%s""",
                        (live_pred if live_pred in LADDER_TIERS else None,
                         rec_id, run['run_id']))
        pg.commit()
        (changes if verdict_name == 'change' else no_changes).append(
            f'{sid}: {current} -> {choice}'
            + (' ADOPTED' if adopted else
               (' (pending rec %d)' % rec_id if verdict_name == 'change' else '')))

    print(f'[shrink] DONE changes={len(changes)} no_change={len(no_changes)} '
          f'adopted={len(adopted_ids)} skipped_existing={skipped}')

    if not args.dry_run and (changes or no_changes):
        head = (f'**Universe Shrink — {candidate_set_id}** '
                f'({len(changes)} changes, {len(adopted_ids)} adopted, '
                f'{len(no_changes)} unchanged)')
        lines = [head] + [f'- {c}' for c in changes]
        buf = ''
        for ln in lines:
            if len(buf) + len(ln) + 1 > 1800:
                recs.post_discord(pg, buf)
                buf = ''
            buf += ln + '\n'
        if buf:
            recs.post_discord(pg, buf)

    if adopted_ids:
        if args.reassign:
            scope = (['--strategy-id', adopted_ids[0]]
                     if len(adopted_ids) == 1 else ['--all'])
            print(f'[shrink] re-running activation assigner {scope}…')
            rc = subprocess.run(
                ['python3', '-m', 'backtest.activation_assigner', *scope,
                 '--notify'],
                cwd=str(ROOT), env={**os.environ, 'PYTHONPATH': 'src'},
            ).returncode
            print(f'[shrink] assigner rc={rc}')
        else:
            print('[shrink] OWED: python3 -m backtest.activation_assigner '
                  '--all  (re-derive eligibility from chosen sleeves)')
    pg.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
