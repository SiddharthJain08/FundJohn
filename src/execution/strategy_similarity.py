#!/usr/bin/env python3
"""Per-regime strategy x strategy similarity + clustering for orthogonalization.

The transpose of correlation_matrix.py (which is ticker-keyed). Lead signal =
holdings co-firing Jaccard over (ISO-week, ticker, direction) emissions; blended
with return-correlation under a data-adaptive weight that rises from 0 as joint
history accrues. Reuses correlation_matrix's clip/sparse conventions.

Spec: docs/archive/superpowers/specs/2026-05-29-strategy-orthogonalization-design.md
"""
from __future__ import annotations

import os
import re
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
LW_GAMMA_MIN_ROWS = 60    # task P1: min overlapping return obs to fit lw_gamma at rebuild
# task P1 (shadow-first) storage choice: the per-regime LW gamma estimate rides
# ALONGSIDE the existing strategy_similarity_matrix row's `trigger` TEXT column
# as a parseable suffix, rather than as an extra key inside the `matrix` JSONB
# blob. `trigger` already carries suffix-encoded provenance (see the
# `+src=backtest` tag below) and nothing reads it with strict equality, so
# this is the least invasive option: the `matrix` JSONB stays byte-identical
# to today for every existing consumer (including the dashboard's
# /api/strategy-similarity route, which serves `matrix` verbatim) — a
# sentinel key mixed into `matrix` would have appeared there as a bogus
# pseudo-strategy row. See task-P1-report.md for the full rationale.
# Matches up to the next '+' (another suffix) or end-of-string — NOT anchored
# to end-of-string alone, so a hypothetical future appender that tacks on yet
# another '+something' suffix after ours still parses correctly.
_LW_GAMMA_TRIGGER_RE = re.compile(r'\+lw_gamma=([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)(?:\+|$)')

_DOTENV_LOADED = False


def _db():
    global _DOTENV_LOADED
    import psycopg2
    if not _DOTENV_LOADED:
        from dotenv import load_dotenv
        try:
            load_dotenv()
        except OSError:
            # .env is root-owned; under the claudebot-run systemd units
            # (weekend-maintenance-sat, sunday-research-code) it is unreadable
            # — but those units already inject it via EnvironmentFile=, so the
            # vars are in os.environ. The 2026-08-09 weekend-sat similarity
            # rebuild crashed here instead of connecting; never fatal again.
            pass
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
    """{regime_state: {strategy_id: {date_str: daily_return}}} from strategy_daily_returns.

    source='live' filter is load-bearing: the table carries a `source` column, and
    any future writer of backtest-derived rows must NOT silently blend into the
    live leg (2026-08-07 guard, pre-dating any such writer)."""
    sql = """
        SELECT regime_state, strategy_id, ret_date::text, daily_return_pct::float
          FROM strategy_daily_returns
         WHERE ret_date >= CURRENT_DATE - (%s::int * INTERVAL '1 day')
           AND source = 'live'
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


def _returns_by_regime_backtest() -> dict[str, dict[str, dict[str, float]]]:
    """{regime_state: {strategy_id: {date_str: mean_pnl_pct}}} from BACKTEST trades.

    Source: strategy_backtest_trades t JOIN strategy_backtest_runs r
            ON r.run_id = t.run_id AND r.primary_window
    A trade's pnl_pct is bucketed onto its EXIT date (realisation), grouped by
    t.entry_regime (spec 2026-08-05 §3.2). Per-day value = AVG(pnl_pct) across
    that day's exits — Pearson is invariant to per-strategy affine scaling, so
    mean-vs-sum only differs through day-mix weighting; mean is used because it
    is bounded regardless of trade count. entry_regime is the ENTRY stamp, not
    the holding period — the --shadow report measures how often exits cross
    regimes (§3.4(2)) so the mis-bucketing is quantified, not assumed away."""
    sql = """
        SELECT t.entry_regime, t.strategy_id, t.exit_date::text, AVG(t.pnl_pct)::float
          FROM strategy_backtest_trades t
          JOIN strategy_backtest_runs r ON r.run_id = t.run_id AND r.primary_window
         WHERE t.exit_date IS NOT NULL AND t.entry_regime IS NOT NULL
         GROUP BY 1, 2, 3
    """
    out: dict[str, dict[str, dict[str, float]]] = {r: {} for r in REGIME_STATES}
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            for regime, sid, d, ret in cur.fetchall():
                if regime in out:
                    out[regime].setdefault(sid, {})[d] = float(ret)
    finally:
        conn.close()
    return out


def _cofiring_sets_by_regime_backtest() -> tuple[dict[str, dict[str, set]],
                                                 dict[str, dict[str, set]]]:
    """BACKTEST co-firing tuples, same shape as the live sibling, plus each
    strategy's traded-ticker set (its de-facto simulated universe).

    Returns ({regime: {sid: {(iso_week, ticker, direction_int)}}},
             {regime: {sid: {ticker}}}).

    The traded-ticker sets exist for §3.4(1): post-shrink universes differ per
    strategy, so raw backtest co-firing fakes orthogonality between strategies
    simulated on different symbol sets. Overlap must be computed on the
    INTERSECTION of the two strategies' traded universes
    (overlap_similarity_restricted below)."""
    sql = """
        SELECT DISTINCT t.entry_regime, t.strategy_id,
               to_char(t.entry_date, 'IYYY"W"IW'), t.ticker, t.direction
          FROM strategy_backtest_trades t
          JOIN strategy_backtest_runs r ON r.run_id = t.run_id AND r.primary_window
         WHERE t.entry_regime IS NOT NULL AND t.ticker IS NOT NULL
    """
    sets: dict[str, dict[str, set]] = {r: {} for r in REGIME_STATES}
    universes: dict[str, dict[str, set]] = {r: {} for r in REGIME_STATES}
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            for regime, sid, wk, ticker, direction in cur.fetchall():
                if regime not in sets:
                    continue
                d = _dir_to_int(direction)
                if d == 0:
                    continue
                sets[regime].setdefault(sid, set()).add((wk, ticker, d))
                universes[regime].setdefault(sid, set()).add(ticker)
    finally:
        conn.close()
    return sets, universes


def overlap_similarity_restricted(sets_by_strat: dict[str, set],
                                  tickers_by_strat: dict[str, set]
                                  ) -> tuple[dict[str, dict[str, float]],
                                             dict[tuple, int]]:
    """Pairwise Jaccard restricted, per pair, to the intersection of the two
    strategies' traded-ticker universes (§3.4(1) mitigation). Returns
    (matrix, intersection_size_per_pair). Empty intersection → 0.0 similarity
    with intersection size 0 recorded (the caller can distinguish 'orthogonal'
    from 'never comparable')."""
    strats = sorted(sets_by_strat.keys())
    # ticker → tuple-bucket index per strategy, so a pair only touches buckets
    # in its universe intersection instead of filtering full sets (16k+ pairs).
    buckets: dict[str, dict[str, set]] = {}
    for s in strats:
        by_ticker: dict[str, set] = {}
        for tup in sets_by_strat[s]:
            by_ticker.setdefault(tup[1], set()).add(tup)
        buckets[s] = by_ticker
    out: dict[str, dict[str, float]] = {s: {} for s in strats}
    inter_size: dict[tuple, int] = {}
    for i, a in enumerate(strats):
        out[a][a] = 1.0
        for b in strats[i + 1:]:
            common = tickers_by_strat.get(a, set()) & tickers_by_strat.get(b, set())
            inter_size[(a, b)] = inter_size[(b, a)] = len(common)
            if not common:
                out[a][b] = out[b][a] = 0.0
                continue
            n_int = n_uni = 0
            ba, bb = buckets[a], buckets[b]
            for t in common:
                sa, sb = ba.get(t, ()), bb.get(t, ())
                sa, sb = set(sa), set(sb)
                n_int += len(sa & sb)
                n_uni += len(sa | sb)
            j = (n_int / n_uni) if n_uni else 0.0
            out[a][b] = out[b][a] = j
    return out, inter_size


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


def similarity_for_regime_backtest(regime_state: str,
                                   window_days: int = DEFAULT_WINDOW_DAYS,
                                   live_cofiring=None, bt_cofiring=None,
                                   bt_universes=None, bt_returns=None
                                   ) -> dict[str, dict[str, float]]:
    """Backtest-sourced similarity (spec 2026-08-05 §3.1): replace ONE leg, not both.

    * PnL-correlation leg → BACKTEST (strategy_backtest_trades, primary runs,
      10y / all 4 regimes) — the leg that was starved on live data (HIGH_VOL had
      13 strategies, CRISIS zero).
    * Co-firing overlap leg → LIVE where it exists (same ticker, same ISO week,
      same direction in production is genuinely OBSERVED behaviour); where a pair
      lacks live observation on either side, fall back to BACKTEST overlap
      computed on the intersection of the two strategies' traded universes
      (§3.4(1)) rather than to SPARSE_DEFAULT.

    The adaptive alpha rides the BACKTEST returns' joint-observation counts —
    with ~10y of history alpha sits at its 0.6 ceiling for almost every pair, so
    the blend is ≈ 0.4·overlap + 0.6·ret_corr."""
    live_cofiring = (live_cofiring if live_cofiring is not None
                     else _cofiring_sets_by_regime(window_days).get(regime_state, {}))
    if bt_cofiring is None or bt_universes is None:
        _sets, _unis = _cofiring_sets_by_regime_backtest()
        bt_cofiring = _sets.get(regime_state, {})
        bt_universes = _unis.get(regime_state, {})
    bt_returns = (bt_returns if bt_returns is not None
                  else _returns_by_regime_backtest().get(regime_state, {}))

    overlap_live = overlap_similarity(live_cofiring) if live_cofiring else {}
    overlap_bt, _inter = (overlap_similarity_restricted(bt_cofiring, bt_universes)
                          if bt_cofiring else ({}, {}))
    retcorr, n_obs = (return_correlation(bt_returns) if bt_returns else ({}, {}))
    strats = sorted(set(overlap_live) | set(overlap_bt) | set(retcorr))
    if not strats:
        return {}

    def _pair_overlap(a: str, b: str) -> float:
        if a == b:
            return 1.0
        # live observation preferred: both sides must actually have live emissions
        if live_cofiring.get(a) and live_cofiring.get(b):
            return overlap_live.get(a, {}).get(b, 0.0)
        return overlap_bt.get(a, {}).get(b, 0.0)

    return blend_similarity(
        {a: {b: _pair_overlap(a, b) for b in strats} for a in strats},
        {a: {b: retcorr.get(a, {}).get(b, 1.0 if a == b else SPARSE_DEFAULT) for b in strats} for a in strats},
        n_obs)


def resolve_source(source: Optional[str] = None) -> str:
    """'live' | 'backtest' — explicit arg wins, else OPENCLAW_SIMILARITY_SOURCE,
    else 'live'. Read at call time (not import) so the weekly cron picks up an
    .env cutover without code changes."""
    src = source or os.environ.get('OPENCLAW_SIMILARITY_SOURCE') or 'live'
    if src not in ('live', 'backtest'):
        raise ValueError(f'OPENCLAW_SIMILARITY_SOURCE={src!r}: expected live|backtest')
    return src


def representatives(fold_groups: dict[int, list[str]],
                    eff_sharpe: dict[str, float]) -> dict[int, str]:
    """group_id -> max-effective_sharpe member (ties broken by strategy_id for determinism)."""
    out: dict[int, str] = {}
    for gid, members in fold_groups.items():
        out[gid] = max(sorted(members), key=lambda s: eff_sharpe.get(s, float('-inf')))
    return out


def _dense_return_panel(returns_by_strat: dict[str, dict[str, float]],
                        min_rows: int = LW_GAMMA_MIN_ROWS):
    """{strategy_id: {date: return}} -> a dates x strategies DataFrame
    restricted to dates where EVERY included strategy has an observation (a
    true dense panel — no NaN-zeroing inside the shrinkage estimator). None
    if fewer than 3 strategies or fewer than `min_rows` such common dates
    survive (task P1: 'if the panel is too thin, store nothing').

    Strategies with fewer than `min_rows` OWN observations are dropped BEFORE
    the N-way date intersection (mirrors asset_correlation._dense_panel_from_
    returns) — a sparsely-observed strategy can never itself reach min_rows
    of overlap and would otherwise poison the intersection for every
    well-observed strategy too. NOTE (verified against live
    strategy_daily_returns, 2026-08-24): per-regime distinct-date counts over
    a 90-day window currently run well under 60 for every regime (HIGH_VOL≈3,
    TRANSITIONING≈21, LOW_VOL≈54) — under today's return-recording density
    this floor is rarely if ever clearable, so `store nothing` is the
    EXPECTED outcome for most rebuilds right now, not a bug in this filter.
    See task-P1-report.md."""
    if not returns_by_strat or len(returns_by_strat) < 3:
        return None
    import pandas as pd
    df = pd.DataFrame(returns_by_strat)
    well_covered = [c for c in df.columns if df[c].notna().sum() >= min_rows]
    if len(well_covered) < 3:
        return None
    dense = df[well_covered].dropna(axis=0, how='any')
    if dense.shape[0] < min_rows or dense.shape[1] < 3:
        return None
    return dense


def _regime_lw_gamma(regime: str, src: str, rets, bt_rets) -> Optional[float]:
    """Best-effort LW shrinkage intensity for one regime's strategy returns
    (task P1). Never raises — any failure (thin panel, import error, etc.)
    just means nothing gets stored for that regime this rebuild."""
    try:
        returns_by_strat = ((rets or {}).get(regime, {}) if src == 'live'
                            else (bt_rets or {}).get(regime, {}))
        panel = _dense_return_panel(returns_by_strat)
        if panel is None:
            return None
        from execution.shrinkage import lw_gamma as _lw_gamma
        return _lw_gamma(panel)
    except Exception:
        return None


def _parse_lw_gamma_trigger(trigger) -> Optional[float]:
    """Extract the lw_gamma value a `+lw_gamma=<x>` suffix on a
    strategy_similarity_matrix.trigger value, or None if absent/unparseable."""
    if not trigger:
        return None
    m = _LW_GAMMA_TRIGGER_RE.search(str(trigger))
    return float(m.group(1)) if m else None


def _build_matrix_trigger(trigger: str, lw_g: Optional[float]) -> str:
    """Pure string op: append the `+lw_gamma=<x.xxxxxx>` suffix (task P1) that
    carries a regime's fitted LW shrinkage intensity alongside the plain
    `trigger` value stored on this rebuild's strategy_similarity_matrix row;
    returns `trigger` unchanged when lw_g wasn't estimated (thin panel).
    Round-trips with _parse_lw_gamma_trigger. `trigger` may already carry a
    prior `+`-suffix (e.g. `+src=backtest`) — this appends after it, and the
    parser is not end-anchored so either order still parses."""
    return trigger if lw_g is None else f'{trigger}+lw_gamma={lw_g:.6f}'


def rebuild(trigger: str = 'manual', window_days: int = DEFAULT_WINDOW_DAYS,
            verbose: bool = False, source: Optional[str] = None) -> dict:
    """Build per-regime similarity + clusters; persist matrix, fold-groups, factor-blocks, audit.

    source: 'live' (historical behaviour) or 'backtest' (spec 2026-08-05 —
    PnL leg from backtest trades, co-firing live-where-observed). Defaults to
    OPENCLAW_SIMILARITY_SOURCE so the cutover is an .env flip."""
    import json
    src = resolve_source(source)
    cof = _cofiring_sets_by_regime(window_days)
    rets = _returns_by_regime(window_days) if src == 'live' else None
    bt_sets = bt_unis = bt_rets = None
    if src == 'backtest':
        bt_sets, bt_unis = _cofiring_sets_by_regime_backtest()
        bt_rets = _returns_by_regime_backtest()
    trigger = trigger if src == 'live' else f'{trigger}+src=backtest'
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

                if src == 'live':
                    sim = similarity_for_regime(regime, window_days,
                                                cofiring=cof.get(regime, {}),
                                                returns=rets.get(regime, {}))
                else:
                    sim = similarity_for_regime_backtest(
                        regime, window_days,
                        live_cofiring=cof.get(regime, {}),
                        bt_cofiring=bt_sets.get(regime, {}),
                        bt_universes=bt_unis.get(regime, {}),
                        bt_returns=bt_rets.get(regime, {}))
                if not sim:
                    summary[regime] = {'strategies': 0}
                    continue
                strats = sorted(sim.keys())
                fold, blocks = cluster_two_cuts(sim, strats)
                eff = _eff_sharpe_by_strat(regime)
                reps = representatives(fold, eff)

                # task P1 (shadow-first): best-effort LW gamma estimate for
                # this regime's strategy returns, stored alongside the matrix
                # as a `+lw_gamma=<x>` suffix on THIS row's trigger only (see
                # LW_GAMMA_MIN_ROWS comment above) — never blocks the rebuild.
                lw_g = _regime_lw_gamma(regime, src, rets, bt_rets)
                # WARNING for future maintainers: matrix_trigger may carry an
                # optional '+lw_gamma=<float>' suffix appended by
                # _build_matrix_trigger — any code that matches `trigger` by
                # strict equality (`==`) will silently stop matching rows
                # written here; use _parse_lw_gamma_trigger or a substring
                # check instead.
                matrix_trigger = _build_matrix_trigger(trigger, lw_g)

                # Insert new similarity matrix row
                cur.execute(
                    "INSERT INTO strategy_similarity_matrix (regime_state, matrix, trigger) VALUES (%s, %s, %s)",
                    (regime, json.dumps(sim), matrix_trigger))

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
    """Live read for the sizer: {fold_map, rep_map, block_map, matrix, lw_gamma}.
    fold_map: strategy_id -> group_id (multi-member groups only).
    rep_map:  group_id -> representative strategy_id.
    block_map: strategy_id -> block_id (multi-member blocks only).
    matrix:   {strategy_id: {strategy_id: rho}} (current row, or {}) —
              byte-identical to the pre-task-P1 shape; lw_gamma is carried
              out-of-band on the row's `trigger` column, never inside `matrix`.
    lw_gamma: task P1 — the LW shrinkage intensity estimated at the matrix's
              last rebuild, or None if it wasn't estimated (thin panel) or
              the row predates this feature. Feed to
              orthogonalization.resolve_tangency_gamma as `artifact_gamma`."""
    out = {'fold_map': {}, 'rep_map': {}, 'block_map': {}, 'matrix': {}, 'lw_gamma': None}
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

            # Load similarity matrix (+ trigger, to recover any lw_gamma suffix)
            cur.execute(
                "SELECT matrix, trigger FROM strategy_similarity_matrix "
                "WHERE regime_state=%s AND is_current ORDER BY id DESC LIMIT 1",
                (regime_state,))
            row = cur.fetchone()
            if row and isinstance(row[0], dict):
                out['matrix'] = row[0]
                out['lw_gamma'] = _parse_lw_gamma_trigger(row[1])
    finally:
        conn.close()
    return out


def _current_weight_rows(regime_state: str) -> dict[str, dict]:
    """{strategy_id: {daily_weight, bt_n, bt_sharpe}} for is_current rows."""
    out: dict[str, dict] = {}
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT strategy_id, daily_weight::float, COALESCE(bt_n, 0),
                          COALESCE(bt_sharpe, 0)::float
                     FROM strategy_weights_by_regime
                    WHERE is_current AND regime_state=%s""", (regime_state,))
            for sid, dw, bt_n, bt_s in cur.fetchall():
                out[sid] = {'daily_weight': float(dw or 0.0), 'bt_n': int(bt_n),
                            'bt_sharpe': float(bt_s)}
    finally:
        conn.close()
    return out


def _gate_weight_map(regime_state: str) -> dict[str, float]:
    """Replicates the sizer's gate leg: daily_weight × trade_weight_factor(bt_n)
    when OPENCLAW_TRADE_WEIGHT_FACTOR=1, else daily_weight."""
    # Duplicated one-line check rather than importing trade_weight_factor_enabled
    # from regime_blended_sizer at module level: this module already imports
    # regime_blended_sizer lazily (function-local, _keep_under_gate above) and
    # regime_blended_sizer imports this module lazily too (load_groups, in
    # _sharpe_cadence_path) — adding a module-level cross-import here would risk
    # turning that mutually-lazy pair into a real load-time cycle.
    if os.environ.get('OPENCLAW_TRADE_WEIGHT_FACTOR') != '1':
        return {sid: r['daily_weight'] for sid, r in _current_weight_rows(regime_state).items()}
    from execution.orthogonalization import trade_weight_factor
    anchor = 1000.0
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM pipeline_config WHERE key='strategy_trade_factor_anchor'")
            row = cur.fetchone()
            if row and row[0]:
                anchor = float(row[0])
    except Exception:
        pass
    finally:
        conn.close()
    rows = _current_weight_rows(regime_state)
    return {sid: r['daily_weight'] * trade_weight_factor(r['bt_n'], anchor)
            for sid, r in rows.items()}


def _recent_contribs(regime_state: str, days: int) -> dict[str, list[tuple]]:
    """{ticker: [(sid, dir_int), ...]} from recent execution_signals restricted to
    strategies carrying current weights — an approximation of the sizer's
    active-window ticker_meta for the shadow S_adj recompute (latest signal per
    (strategy, ticker) wins, matching the sizer's aggregation)."""
    weights = _current_weight_rows(regime_state)
    latest: dict[tuple, tuple] = {}
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT strategy_id, ticker, direction, signal_date
                     FROM execution_signals
                    WHERE signal_date >= CURRENT_DATE - (%s::int * INTERVAL '1 day')
                      AND strategy_id IS NOT NULL AND ticker IS NOT NULL""",
                (days,))
            for sid, ticker, direction, sdate in cur.fetchall():
                if sid not in weights:
                    continue
                d = _dir_to_int(direction)
                if d == 0:
                    continue
                key = (sid, ticker)
                if key not in latest or sdate > latest[key][0]:
                    latest[key] = (sdate, d)
    finally:
        conn.close()
    contribs: dict[str, list[tuple]] = {}
    for (sid, ticker), (_d, dsign) in latest.items():
        contribs.setdefault(ticker, []).append((sid, dsign))
    return contribs


def _live_min_acting(regime_state: str) -> int:
    """Per-regime conviction gate = minimum distinct acting strategies in the
    net direction (regime_sizer_params.min_acting_strategies, 2026-08-22;
    replaced the S_adj floor min_corr_cum_sharpe, which is no longer read)."""
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT min_acting_strategies FROM regime_sizer_params WHERE regime_state=%s",
                        (regime_state,))
            row = cur.fetchone()
            return max(1, min(10, int(row[0]))) if row and row[0] is not None else 1
    finally:
        conn.close()


def _keep_under_gate(sadj: dict, contribs: dict, min_acting: int) -> set:
    """Tickers the sizer would TAKE under the acting-strategy gate, given the
    S_adj map (net direction) and the (sid, dir) contributors. Mirrors
    regime_blended_sizer: count distinct same-direction strategies, keep when
    >= min_acting."""
    from execution.regime_blended_sizer import _acting_counts, _net_signs
    meta = {t: {'strategies': [sid for sid, _ in rows], 'directions': [d for _, d in rows]}
            for t, rows in contribs.items()}
    counts = _acting_counts(meta, _net_signs(sadj, {}))
    return {t for t, n in counts.items() if t in sadj and n >= min_acting}


def _sadj_under(matrix: dict, contribs: dict, gate_w: dict) -> dict[str, float]:
    from execution.orthogonalization import tangency_net_sharpe
    if not contribs:
        return {}
    adj, _backstop = tangency_net_sharpe(contribs, matrix or {}, gate_w)
    return adj


def _pair_iter(strats: list[str]):
    for i, a in enumerate(strats):
        for b in strats[i + 1:]:
            yield a, b


def _pct_list(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    vs = sorted(vals)
    i = max(0, min(len(vs) - 1, int(round(p * (len(vs) - 1)))))
    return vs[i]


def shadow_report(window_days: int = DEFAULT_WINDOW_DAYS, sadj_days: int = 7) -> dict:
    """Spec 2026-08-05 §3.3 — compare the LIVE current matrix (what the sizer
    uses today) against the backtest-sourced build, per regime. WRITES NOTHING.
    The comparison is the deliverable; §3.5 gates the cutover on it."""
    live_cof = _cofiring_sets_by_regime(window_days)
    bt_sets, bt_unis = _cofiring_sets_by_regime_backtest()
    bt_rets = _returns_by_regime_backtest()
    report: dict[str, dict] = {}
    for regime in REGIME_STATES:
        live_matrix = load_groups(regime).get('matrix') or {}
        bt_matrix = similarity_for_regime_backtest(
            regime, window_days,
            live_cofiring=live_cof.get(regime, {}),
            bt_cofiring=bt_sets.get(regime, {}),
            bt_universes=bt_unis.get(regime, {}),
            bt_returns=bt_rets.get(regime, {}))
        weights = _current_weight_rows(regime)
        weighted = sorted(set(weights))

        # (2)+(3) pair-level diffs on pairs that currently carry weight.
        deltas, live_pair_deltas, sparse_gained = [], [], 0
        inter_sizes = []
        _, inter_map = (overlap_similarity_restricted(bt_sets.get(regime, {}),
                                                      bt_unis.get(regime, {}))
                        if bt_sets.get(regime) else ({}, {}))
        big_moves = 0
        for a, b in _pair_iter(weighted):
            in_live = (a in live_matrix and b in (live_matrix.get(a) or {}))
            rho_live = (live_matrix[a][b] if in_live else SPARSE_DEFAULT)
            in_bt = (a in bt_matrix and b in (bt_matrix.get(a) or {}))
            rho_bt = (bt_matrix[a][b] if in_bt else SPARSE_DEFAULT)
            d = rho_bt - rho_live
            deltas.append(d)
            if abs(d) > 0.2:
                big_moves += 1
            if in_live:
                live_pair_deltas.append(d)
            elif in_bt:
                sparse_gained += 1
            if (a, b) in inter_map:
                inter_sizes.append(inter_map[(a, b)])

        # (4) S_adj recompute over the recent signal window under both matrices.
        gate_w = _gate_weight_map(regime)
        contribs = _recent_contribs(regime, sadj_days)
        min_acting = _live_min_acting(regime)
        sadj_live = _sadj_under(live_matrix, contribs, gate_w)
        sadj_bt = _sadj_under(bt_matrix, contribs, gate_w)
        keep_live = _keep_under_gate(sadj_live, contribs, min_acting)
        keep_bt = _keep_under_gate(sadj_bt, contribs, min_acting)
        mags_bt = sorted(abs(v) for v in sadj_bt.values())

        # §3.4(3) look-ahead sentinels: extreme backtest pairs.
        hot_pairs = [(a, b, round(bt_matrix[a][b], 3))
                     for a, b in _pair_iter(sorted(bt_matrix))
                     if abs(bt_matrix.get(a, {}).get(b, 0.0)) > 0.9]

        report[regime] = {
            'n_strategies': {'live': len(live_matrix), 'backtest': len(bt_matrix)},
            'weighted_strategies': len(weighted),
            'pair_delta_rho': {
                'n_pairs': len(deltas),
                'median': round(_pct_list(deltas, 0.5), 4),
                'p25': round(_pct_list(deltas, 0.25), 4),
                'p75': round(_pct_list(deltas, 0.75), 4),
                'abs_gt_0.2': big_moves,
            },
            'signed_mean_delta_on_live_pairs': (
                round(sum(live_pair_deltas) / len(live_pair_deltas), 4)
                if live_pair_deltas else None),
            'n_live_observed_pairs': len(live_pair_deltas),
            'sparse_default_pairs_gaining_real_rho': sparse_gained,
            'universe_intersection': {
                'median': _pct_list([float(x) for x in inter_sizes], 0.5),
                'p25': _pct_list([float(x) for x in inter_sizes], 0.25),
                'zero_intersection_pairs': sum(1 for x in inter_sizes if x == 0),
            },
            'sadj': {
                'min_acting_strategies': min_acting,
                'tickers': len(sadj_live),
                'keep_live_matrix': len(keep_live),
                'keep_backtest_matrix': len(keep_bt),
                'kept_set_changed': sorted(keep_live ^ keep_bt)[:20],
                'bt_dist': {'min': (mags_bt[0] if mags_bt else 0.0),
                            'p50': _pct_list(mags_bt, 0.5),
                            'p90': _pct_list(mags_bt, 0.9),
                            'max': (mags_bt[-1] if mags_bt else 0.0)},
                # gate calibration: kept-ticker count at each candidate
                # min_acting_strategies (1..10) under the BACKTEST matrix — the
                # operator sets the Conviction Gate sliders from this curve.
                'keep_curve_backtest_matrix': {
                    str(n): len(_keep_under_gate(sadj_bt, contribs, n))
                    for n in range(1, 11)},
            },
            'hot_pairs_gt_0.9': hot_pairs[:15],
        }
    # §3.4(2) — regime mis-bucketing: share of trades whose exit falls in a
    # different regime than entry (needs the historical regime calendar).
    try:
        report['_cross_regime_exit_share'] = _cross_regime_exit_share()
    except Exception as e:                                    # non-fatal diagnostic
        report['_cross_regime_exit_share'] = f'unavailable: {e}'
    return report


def _cross_regime_exit_share() -> dict[str, float]:
    """Per entry_regime: fraction of primary-run trades whose exit_date's regime
    (historical_regimes.parquet) differs from entry_regime."""
    import pandas as pd
    hist = pd.read_parquet('data/master/historical_regimes.parquet',
                           columns=['date', 'regime'])
    by_date = {str(d.date() if hasattr(d, 'date') else d): r
               for d, r in zip(hist['date'], hist['regime'])}
    sql = """
        SELECT t.entry_regime, t.exit_date::text, count(*)
          FROM strategy_backtest_trades t
          JOIN strategy_backtest_runs r ON r.run_id=t.run_id AND r.primary_window
         WHERE t.exit_date IS NOT NULL AND t.entry_regime IS NOT NULL
         GROUP BY 1, 2
    """
    tot: dict[str, int] = {}
    crossed: dict[str, int] = {}
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            for regime, exit_d, n in cur.fetchall():
                tot[regime] = tot.get(regime, 0) + n
                exit_regime = by_date.get(exit_d)
                if exit_regime is not None and exit_regime != regime:
                    crossed[regime] = crossed.get(regime, 0) + n
    finally:
        conn.close()
    return {r: round(crossed.get(r, 0) / tot[r], 4) for r in sorted(tot)}


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--rebuild', action='store_true')
    p.add_argument('--shadow', action='store_true',
                   help='spec 2026-08-05 §3.3: live-vs-backtest diff report; writes NOTHING')
    p.add_argument('--source', choices=('live', 'backtest'), default=None,
                   help='similarity source for --rebuild (default: OPENCLAW_SIMILARITY_SOURCE or live)')
    p.add_argument('--trigger', default='manual')
    p.add_argument('--window-days', type=int, default=DEFAULT_WINDOW_DAYS)
    p.add_argument('--sadj-days', type=int, default=7)
    p.add_argument('--verbose', action='store_true')
    args = p.parse_args()
    if args.shadow:
        import json
        print(json.dumps(shadow_report(window_days=args.window_days,
                                       sadj_days=args.sadj_days), indent=2))
        return 0
    if args.rebuild:
        s = rebuild(trigger=args.trigger, window_days=args.window_days,
                    verbose=args.verbose, source=args.source)
        print('similarity rebuild:', s)
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
