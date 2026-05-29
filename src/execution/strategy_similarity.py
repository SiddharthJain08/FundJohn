#!/usr/bin/env python3
"""Per-regime strategy x strategy similarity + clustering for orthogonalization.

The transpose of correlation_matrix.py (which is ticker-keyed). Lead signal =
holdings co-firing Jaccard over (ISO-week, ticker, direction) emissions; blended
with return-correlation under a data-adaptive weight that rises from 0 as joint
history accrues. Reuses correlation_matrix's clip/sparse conventions.

Spec: docs/superpowers/specs/2026-05-29-strategy-orthogonalization-design.md
"""
from __future__ import annotations

import os
from typing import Optional

from execution.orthogonalization import _dir_to_int

REGIME_STATES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')
DEFAULT_WINDOW_DAYS = 90
FOLD_THRESHOLD  = float(os.environ.get('OPENCLAW_FOLD_THRESHOLD', '0.85'))
BLOCK_THRESHOLD = float(os.environ.get('OPENCLAW_BLOCK_THRESHOLD', '0.40'))
RETURN_CORR_ALPHA_CEIL = 0.6     # max weight return-corr ever takes in the blend
ALPHA_FULL_OBS = 60              # overlapping observations at which alpha reaches the ceiling
MAX_OFF_DIAGONAL = 0.95
SPARSE_DEFAULT = 0.05

_DOTENV_LOADED = False


def _db():
    global _DOTENV_LOADED
    import psycopg2
    if not _DOTENV_LOADED:
        from dotenv import load_dotenv
        load_dotenv()
        _DOTENV_LOADED = True
    return psycopg2.connect(os.environ.get('DATABASE_URL')
                            or os.environ.get('POSTGRES_URI')
                            or 'postgresql://openclaw:password@localhost:5432/openclaw')


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    u = len(a | b)
    return (len(a & b) / u) if u else 0.0


def overlap_similarity(sets_by_strat: dict[str, set]) -> dict[str, dict[str, float]]:
    """Pairwise Jaccard over co-firing emission sets. Diagonal 1.0; symmetric."""
    strats = sorted(sets_by_strat.keys())
    out: dict[str, dict[str, float]] = {s: {} for s in strats}
    for i, a in enumerate(strats):
        out[a][a] = 1.0
        for b in strats[i + 1:]:
            j = jaccard(sets_by_strat[a], sets_by_strat[b])
            out[a][b] = j
            out[b][a] = j
    return out


def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    import math
    n = len(xs)
    if n != len(ys) or n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def return_correlation(returns_by_strat: dict[str, dict[str, float]]
                       ) -> tuple[dict[str, dict[str, float]], dict[tuple, int]]:
    """Pearson on per-strategy {date: daily_return}. Returns (matrix, n_obs_per_pair).
    Sparse / zero-variance pairs default to SPARSE_DEFAULT; off-diagonals clipped +/-0.95."""
    strats = sorted(returns_by_strat.keys())
    out: dict[str, dict[str, float]] = {s: {} for s in strats}
    n_obs: dict[tuple, int] = {}
    for i, a in enumerate(strats):
        out[a][a] = 1.0
        for b in strats[i + 1:]:
            da, db = returns_by_strat[a], returns_by_strat[b]
            paired = sorted(set(da) & set(db))
            n_obs[(a, b)] = n_obs[(b, a)] = len(paired)
            if len(paired) < 2:
                rho = SPARSE_DEFAULT
            else:
                r = _pearson([da[d] for d in paired], [db[d] for d in paired])
                rho = SPARSE_DEFAULT if r is None else max(-MAX_OFF_DIAGONAL, min(MAX_OFF_DIAGONAL, r))
            out[a][b] = out[b][a] = rho
    return out, n_obs


def adaptive_alpha(n_obs: int) -> float:
    """Weight on return-correlation: 0 at no joint history, rising linearly to the
    ceiling at ALPHA_FULL_OBS overlapping observations, then capped."""
    if n_obs <= 0:
        return 0.0
    return min(RETURN_CORR_ALPHA_CEIL, RETURN_CORR_ALPHA_CEIL * n_obs / ALPHA_FULL_OBS)


def blend_similarity(overlap: dict[str, dict[str, float]],
                     return_corr: dict[str, dict[str, float]],
                     n_obs_per_pair: dict[tuple, int]) -> dict[str, dict[str, float]]:
    """Per-pair convex blend: (1-alpha)*overlap + alpha*return_corr, alpha=adaptive_alpha(n_obs).
    Overlap LEADS; return-corr enters only as joint history accrues. Diagonal 1.0."""
    strats = sorted(overlap.keys())
    out: dict[str, dict[str, float]] = {s: {} for s in strats}
    for a in strats:
        for b in strats:
            if a == b:
                out[a][b] = 1.0
                continue
            o = overlap.get(a, {}).get(b, 0.0)
            r = return_corr.get(a, {}).get(b, SPARSE_DEFAULT)
            al = adaptive_alpha(n_obs_per_pair.get((a, b), 0))
            out[a][b] = (1.0 - al) * o + al * r
    return out


def cluster_two_cuts(sim: dict[str, dict[str, float]], strategies: list[str],
                     fold_thr: float = FOLD_THRESHOLD, block_thr: float = BLOCK_THRESHOLD
                     ) -> tuple[dict[int, list[str]], dict[int, list[str]]]:
    """Agglomerative average-linkage clustering on distance = 1 - similarity.
    Cut at two heights -> (fold_groups, factor_blocks). Each maps group_id -> [strategy_id].
    <2 strategies -> each its own singleton group."""
    strategies = sorted(strategies)
    n = len(strategies)
    if n < 2:
        return ({i: [s] for i, s in enumerate(strategies)},
                {i: [s] for i, s in enumerate(strategies)})

    import numpy as np
    from scipy.cluster.hierarchy import linkage, fcluster
    from scipy.spatial.distance import squareform

    dist = np.zeros((n, n))
    for i, a in enumerate(strategies):
        for k, b in enumerate(strategies):
            if i < k:
                s = max(0.0, min(1.0, sim.get(a, {}).get(b, 0.0)))
                dist[i][k] = dist[k][i] = 1.0 - s
    Z = linkage(squareform(dist, checks=False), method='average')

    def _cut(thr: float) -> dict[int, list[str]]:
        labels = fcluster(Z, t=1.0 - thr, criterion='distance')  # distance cut = 1 - similarity
        groups: dict[int, list[str]] = {}
        for idx, lab in enumerate(labels):
            groups.setdefault(int(lab), []).append(strategies[idx])
        return groups

    return _cut(fold_thr), _cut(block_thr)


def _iso_week(d) -> str:
    iso = d.isocalendar()
    return f"{iso[0]}W{iso[1]:02d}"


def _cofiring_sets_by_regime(window_days: int) -> dict[str, dict[str, set]]:
    """{regime_state: {strategy_id: {(iso_week, ticker, direction_int), ...}}} from execution_signals."""
    sql = """
        SELECT regime_state, strategy_id, signal_date, ticker, direction
          FROM execution_signals
         WHERE signal_date >= CURRENT_DATE - (%s::int * INTERVAL '1 day')
           AND strategy_id IS NOT NULL AND ticker IS NOT NULL
    """
    out: dict[str, dict[str, set]] = {r: {} for r in REGIME_STATES}
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (window_days,))
            for regime, sid, sdate, ticker, direction in cur.fetchall():
                if regime not in out:
                    out.setdefault(regime, {})
                d = _dir_to_int(direction)
                if d == 0:
                    continue                     # FLAT / unknown: not a directional co-fire
                out[regime].setdefault(sid, set()).add((_iso_week(sdate), ticker, d))
    finally:
        conn.close()
    return out


def _returns_by_regime(window_days: int) -> dict[str, dict[str, dict[str, float]]]:
    """{regime_state: {strategy_id: {date_str: daily_return}}} from strategy_daily_returns."""
    sql = """
        SELECT regime_state, strategy_id, ret_date::text, daily_return_pct::float
          FROM strategy_daily_returns
         WHERE ret_date >= CURRENT_DATE - (%s::int * INTERVAL '1 day')
    """
    out: dict[str, dict[str, dict[str, float]]] = {r: {} for r in REGIME_STATES}
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (window_days,))
            for regime, sid, d, ret in cur.fetchall():
                if regime in out:
                    out[regime].setdefault(sid, {})[d] = float(ret)
    finally:
        conn.close()
    return out


def _eff_sharpe_by_strat(regime_state: str) -> dict[str, float]:
    from execution import strategy_weights as sw
    return {r['strategy_id']: float(r['effective_sharpe']) for r in sw.load_current(regime_state)}


def similarity_for_regime(regime_state: str, window_days: int = DEFAULT_WINDOW_DAYS,
                          cofiring=None, returns=None) -> dict[str, dict[str, float]]:
    """Blended similarity matrix for one regime's strategy set (union of co-firing + returns keys)."""
    cofiring = cofiring if cofiring is not None else _cofiring_sets_by_regime(window_days).get(regime_state, {})
    returns = returns if returns is not None else _returns_by_regime(window_days).get(regime_state, {})
    overlap = overlap_similarity(cofiring) if cofiring else {}
    retcorr, n_obs = (return_correlation(returns) if returns else ({}, {}))
    strats = sorted(set(overlap) | set(retcorr))
    if not strats:
        return {}
    return blend_similarity(
        {a: {b: overlap.get(a, {}).get(b, 1.0 if a == b else 0.0) for b in strats} for a in strats},
        {a: {b: retcorr.get(a, {}).get(b, 1.0 if a == b else SPARSE_DEFAULT) for b in strats} for a in strats},
        n_obs)


def representatives(fold_groups: dict[int, list[str]],
                    eff_sharpe: dict[str, float]) -> dict[int, str]:
    """group_id -> max-effective_sharpe member (ties broken by strategy_id for determinism)."""
    out: dict[int, str] = {}
    for gid, members in fold_groups.items():
        out[gid] = max(sorted(members), key=lambda s: eff_sharpe.get(s, float('-inf')))
    return out


def rebuild(trigger: str = 'manual', window_days: int = DEFAULT_WINDOW_DAYS, verbose: bool = False) -> dict:
    """Build per-regime similarity + clusters; persist matrix, fold-groups, factor-blocks, audit."""
    import json
    cof = _cofiring_sets_by_regime(window_days)
    rets = _returns_by_regime(window_days)
    summary: dict[str, dict] = {}
    conn = _db()
    try:
        with conn.cursor() as cur:
            for regime in REGIME_STATES:
                # Flip is_current=FALSE for existing current rows BEFORE any early-continue
                # so a regime with no new data doesn't keep stale rows alive.
                cur.execute(
                    "UPDATE strategy_similarity_matrix SET is_current=FALSE WHERE regime_state=%s AND is_current",
                    (regime,))
                cur.execute(
                    "UPDATE strategy_fold_groups SET is_current=FALSE WHERE regime_state=%s AND is_current",
                    (regime,))
                cur.execute(
                    "UPDATE strategy_factor_blocks SET is_current=FALSE WHERE regime_state=%s AND is_current",
                    (regime,))

                sim = similarity_for_regime(regime, window_days,
                                            cofiring=cof.get(regime, {}),
                                            returns=rets.get(regime, {}))
                if not sim:
                    summary[regime] = {'strategies': 0}
                    continue
                strats = sorted(sim.keys())
                fold, blocks = cluster_two_cuts(sim, strats)
                eff = _eff_sharpe_by_strat(regime)
                reps = representatives(fold, eff)

                # Insert new similarity matrix row
                cur.execute(
                    "INSERT INTO strategy_similarity_matrix (regime_state, matrix, trigger) VALUES (%s, %s, %s)",
                    (regime, json.dumps(sim), trigger))

                # Insert fold groups + audit
                for gid, members in fold.items():
                    for s in members:
                        cur.execute(
                            """INSERT INTO strategy_fold_groups
                               (regime_state, group_id, strategy_id, is_representative, effective_sharpe, trigger)
                               VALUES (%s, %s, %s, %s, %s, %s)""",
                            (regime, gid, s, s == reps[gid], eff.get(s), trigger))
                    if len(members) >= 2:
                        cur.execute(
                            """INSERT INTO strategy_fold_audit
                               (regime_state, group_id, strategy_ids, representative, member_sharpes)
                               VALUES (%s, %s, %s, %s, %s)""",
                            (regime, gid, members,
                             reps[gid],
                             json.dumps({s: eff.get(s) for s in members})))

                # Insert factor blocks
                for bid, members in blocks.items():
                    for s in members:
                        cur.execute(
                            """INSERT INTO strategy_factor_blocks
                               (regime_state, block_id, strategy_id, trigger)
                               VALUES (%s, %s, %s, %s)""",
                            (regime, bid, s, trigger))

                summary[regime] = {
                    'strategies': len(strats),
                    'fold_groups': sum(1 for m in fold.values() if len(m) >= 2),
                    'factor_blocks': sum(1 for m in blocks.values() if len(m) >= 2),
                }
                if verbose:
                    print(f"[{regime}] {summary[regime]}")
        conn.commit()
    finally:
        conn.close()
    return summary


def load_groups(regime_state: str) -> dict:
    """Live read for the sizer: {fold_map, rep_map, block_map, matrix}.
    fold_map: strategy_id -> group_id (multi-member groups only).
    rep_map:  group_id -> representative strategy_id.
    block_map: strategy_id -> block_id (multi-member blocks only).
    matrix:   {strategy_id: {strategy_id: rho}} (current row, or {})."""
    out = {'fold_map': {}, 'rep_map': {}, 'block_map': {}, 'matrix': {}}
    conn = _db()
    try:
        with conn.cursor() as cur:
            # Load fold groups
            cur.execute(
                "SELECT group_id, strategy_id, is_representative FROM strategy_fold_groups "
                "WHERE regime_state=%s AND is_current",
                (regime_state,))
            members: dict[int, list[str]] = {}
            for gid, sid, is_rep in cur.fetchall():
                members.setdefault(gid, []).append(sid)
                if is_rep:
                    out['rep_map'][gid] = sid
            for gid, ms in members.items():
                if len(ms) >= 2:
                    for s in ms:
                        out['fold_map'][s] = gid

            # Load factor blocks
            cur.execute(
                "SELECT block_id, strategy_id FROM strategy_factor_blocks "
                "WHERE regime_state=%s AND is_current",
                (regime_state,))
            bmembers: dict[int, list[str]] = {}
            for bid, sid in cur.fetchall():
                bmembers.setdefault(bid, []).append(sid)
            for bid, ms in bmembers.items():
                if len(ms) >= 2:
                    for s in ms:
                        out['block_map'][s] = bid

            # Load similarity matrix
            cur.execute(
                "SELECT matrix FROM strategy_similarity_matrix "
                "WHERE regime_state=%s AND is_current ORDER BY id DESC LIMIT 1",
                (regime_state,))
            row = cur.fetchone()
            if row and isinstance(row[0], dict):
                out['matrix'] = row[0]
    finally:
        conn.close()
    return out


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--rebuild', action='store_true')
    p.add_argument('--trigger', default='manual')
    p.add_argument('--window-days', type=int, default=DEFAULT_WINDOW_DAYS)
    p.add_argument('--verbose', action='store_true')
    args = p.parse_args()
    if args.rebuild:
        s = rebuild(trigger=args.trigger, window_days=args.window_days, verbose=args.verbose)
        print('similarity rebuild:', s)
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
