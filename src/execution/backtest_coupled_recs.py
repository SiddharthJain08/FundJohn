"""Saturday backtest-coupling step.

For each fresh stop/TP/max-hold recommendation, backtest the change and apply it
to strategy_regime_params (all eligible regimes) IFF the candidate strictly
improves Sharpe on >= MIN_TRADES trades. Gated by
OPENCLAW_BACKTEST_COUPLED_RECS — refuses to apply when the gate is OFF.

Accept: candidate_sharpe > baseline_sharpe AND candidate_n_trades >= 30.
(2026-07-14 operator directive — full-auto Saturday adjustments: ANY measured
improvement applies; was >= +0.10.)

Scope (2026-05-31, operator): ALL per-strategy config — stop_pct, target_pct,
AND max_hold_days — is decided here via backtest, never in the manual proposals
queue. Stop/target are injected per-regime via param_override (re-anchored in
the simulate loop); max_hold_days is a single top-level run_backtest arg, so it
is backtested as one candidate value applied across the run and then persisted
to every eligible regime via set_params(max_hold_days=...).

2026-07-14 (full-auto Saturday): bracket deltas are backtested regardless of
memo confidence — rows with action_taken='noted' (low-confidence SIZE recs) are
included. The decision lands in the new `coupling_outcome` column; action_taken
(the Monday-handoff SIZE gate) is never touched here. On an APPLY with a stop
candidate, currently-open positions get their broker stop re-anchored to the
validated distance from the original entry (alpaca_replace_stop; still gated by
OPENCLAW_ALPACA_LIVE_REPLACE inside that module).

ONE backtest per rec + NO weekly refresh (2026-07-14 operator directives):
the baseline is READ from the canonical primary_window strategy_backtest_runs
row, with the median stop/target anchors recomputed in SQL from that run's
persisted trades. The single candidate backtest runs on a deferred-commit
connection: on APPLY it is COMMITTED and becomes the new canonical primary row
(run_backtest demotes prior primaries inside the same transaction) — the
strategy's stored metrics therefore always reflect the last adjustment that
was determined helpful, and the fleet-wide weekly re-backtest is retired. On
reject/dry-run the candidate rolls back untouched. A fresh baseline run (which
also persists as canonical, self-healing the store) happens only when no
canonical row exists (strategy never backtested through the unified engine).
max_hold: strategy-configured — run_backtest bakes strategy_regime_params
max_hold into every run, so baselines and candidates share the config horizon
and applied hold changes compound via the stored config_json anchor.

Selected-universe decisions (2026-07-27 operator directive): when the
strategy's chosen predicate is a ladder tier, the candidate backtest runs on
that tier (chosen-universe PROBE, always rolled back) and is judged against
the chosen tier's TOTAL Sharpe from the shrink store — the adjustment is
evaluated on the universe the strategy actually trades. On APPLY the applied
config is re-run on the FULL universe and committed as the new canonical
primary row (canonical must stay full-universe: it is the shrink baseline),
then the chosen-tier sleeves/eligibility are refreshed via a per-strategy
shrink pass. Rejects now cost one cheap tier-bounded run instead of a full
one. Escape hatch: OPENCLAW_COUPLING_CHOSEN_UNIVERSE=0 restores legacy
full-universe decisions.
"""
from __future__ import annotations
import os
from typing import Optional

MIN_DELTA = 0.0          # strict >: any measured Sharpe improvement applies
MIN_TRADES = 30
NOISE = 0.005
DEFAULT_STOP_PCT = 0.07
DEFAULT_TARGET_PCT = 0.08
CLAMP_LO, CLAMP_HI = 0.01, 0.30
# max_hold_days candidate bounds (trading days). A strategy holding 1 day or
# 250 days is the sane envelope; clamp keeps a malformed delta from proposing 0
# (instant exit) or a year-long hold.
MAX_HOLD_LO, MAX_HOLD_HI = 1, 250
DEFAULT_MAX_HOLD = 21  # mirrors unified_backtest.DEFAULT_MAX_HOLD_DAYS
CANONICAL_REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')


def gate_on() -> bool:
    return os.environ.get('OPENCLAW_BACKTEST_COUPLED_RECS') == '1'


def has_actionable_delta(rec: dict) -> bool:
    for k in ('stop_delta_pct', 'target_delta_pct'):
        v = rec.get(k)
        if v is not None and abs(float(v)) >= NOISE:
            return True
    # max_hold_days is an integer day-count delta (not a pct) — any non-zero
    # change is actionable (no NOISE floor; the smallest meaningful step is 1 day).
    hd = rec.get('hold_days_delta')
    if hd is not None and int(hd) != 0:
        return True
    return False


def candidate_pct(base: Optional[float], delta: Optional[float],
                  default: float) -> Optional[float]:
    if delta is None or abs(float(delta)) < NOISE:
        return None
    b = float(base) if base is not None else default
    return max(CLAMP_LO, min(CLAMP_HI, b * (1 + float(delta))))


def candidate_max_hold(base: Optional[int], delta: Optional[int],
                       default: int = DEFAULT_MAX_HOLD) -> Optional[int]:
    """Candidate max_hold_days from an ABSOLUTE integer day delta (hold_days_delta
    is +N/-N days, not a pct). Returns None when there's no change to test.
    Clamped to [MAX_HOLD_LO, MAX_HOLD_HI]; rounds to a whole day."""
    if delta is None or int(delta) == 0:
        return None
    b = int(base) if base is not None else int(default)
    return max(MAX_HOLD_LO, min(MAX_HOLD_HI, b + int(delta)))


def qualifies(*, baseline_sharpe: float, candidate_sharpe: float,
              candidate_n_trades: int) -> bool:
    return (candidate_sharpe - baseline_sharpe) > MIN_DELTA and candidate_n_trades >= MIN_TRADES


def _eligible_regimes(strategy_id) -> list:
    from execution import regime_param_resolver as rpr
    elig = [r for r in CANONICAL_REGIMES if rpr.is_eligible(strategy_id, r)]
    return elig or list(CANONICAL_REGIMES)


def _repo_root():
    from pathlib import Path
    return Path(__file__).resolve().parents[2]


def _chosen_resolver(strategy_id):
    """Selected-universe resolver for adjustment DECISIONS (operator directive
    2026-07-27): stop/TP/max-hold candidates are judged on the universe the
    strategy ACTUALLY trades, not the full backtest panel. Returns
    (resolver, tier); (None, None) = full-universe legacy behavior — the
    manifest predicate is not a ladder tier (incl. 'full', where the panel IS
    the selected universe), no membership artifact exists, or
    OPENCLAW_COUPLING_CHOSEN_UNIVERSE=0 (escape hatch)."""
    if os.environ.get('OPENCLAW_COUPLING_CHOSEN_UNIVERSE', '1') == '0':
        return None, None
    import json
    root = _repo_root()
    try:
        entry = (json.loads((root / 'src/strategies/manifest.json').read_text())
                 .get('strategies', {}).get(strategy_id) or {})
        ref = (entry.get('metadata') or {}).get('universe_filter_ref')
        tier = ref.rsplit(':', 1)[-1] if ref else None
    except Exception:
        return None, None
    from backtest.universe_ladder_selection import LADDER_TIERS
    if tier not in LADDER_TIERS:
        return None, None
    arts = (sorted((root / 'data').glob('universe_tier_membership_shrink-*.parquet'))
            or sorted((root / 'data').glob('universe_tier_membership_*.parquet')))
    if not arts:
        return None, None
    from backtest.precomputed_resolver import PrecomputedResolver
    return PrecomputedResolver(arts[-1], tier), tier


def _chosen_baseline_sharpe(strategy_id, tier):
    """Chosen-tier TOTAL Sharpe from the latest shrink pass — the
    selected-universe baseline a chosen-universe candidate probe is judged
    against. Shrink metrics filter the full-universe primary run's trades to
    tier membership (a documented approximation of a native tier run) and
    carry the same cost model as the probe. None when no shrink row exists
    (caller falls back to the full-universe canonical baseline, logged)."""
    import psycopg2
    try:
        with psycopg2.connect(os.environ['POSTGRES_URI']) as conn, conn.cursor() as cur:
            cur.execute("""SELECT sharpe FROM universe_shrink_metrics
                            WHERE strategy_id=%s AND tier=%s AND regime_state='TOTAL'
                            ORDER BY computed_at DESC LIMIT 1""",
                        (strategy_id, tier))
            row = cur.fetchone()
        return float(row[0]) if row and row[0] is not None else None
    except Exception:
        return None


def _run_metrics(strategy_id, param_override, max_hold_days=None, commit=False) -> dict:
    """Backtest → metrics dict. commit=False (own-conn) rolls back — a pure
    probe. commit=True persists the run as the new canonical primary row (used
    for the missing-baseline fallback, self-healing the store).

    ``max_hold_days`` (when not None) pins the backtest's top-level hold
    horizon; when None run_backtest resolves the strategy-configured max_hold
    from strategy_regime_params. Stop/target candidates ride ``param_override``
    (per-regime)."""
    from backtest import unified_backtest as ub
    kwargs = dict(commit=commit, param_override=param_override, return_metrics=True)
    if max_hold_days is not None:
        kwargs['max_hold_days'] = int(max_hold_days)
    _run_id, metrics = ub.run_backtest(strategy_id, **kwargs)
    return metrics


def _run_candidate(strategy_id, param_override, max_hold_days=None, resolver=None):
    """Candidate backtest on a DEFERRED-COMMIT connection. Returns
    (conn, run_id, metrics); the caller MUST conn.commit() (apply → the run
    becomes the canonical primary_window row; run_backtest's demotion UPDATE is
    part of the same transaction) or conn.rollback() (reject/dry-run), then
    close. On backtest failure the connection is cleaned up here and the error
    propagates. ``resolver`` (chosen-universe probes) bounds the per-bar
    universe; probe runs must ALWAYS be rolled back — canonical primary rows
    stay full-universe (they are the shrink baseline)."""
    import psycopg2
    from backtest import unified_backtest as ub
    conn = psycopg2.connect(os.environ['POSTGRES_URI'])
    try:
        kwargs = dict(conn=conn, commit=False, param_override=param_override,
                      return_metrics=True)
        if resolver is not None:
            kwargs['resolver'] = resolver
        if max_hold_days is not None:
            kwargs['max_hold_days'] = int(max_hold_days)
        run_id, metrics = ub.run_backtest(strategy_id, **kwargs)
    except Exception:
        try:
            conn.rollback()
        finally:
            conn.close()
        raise
    return conn, run_id, metrics


def _stored_baseline(strategy_id) -> Optional[dict]:
    """Baseline from the canonical primary_window backtest row instead of a
    fresh run. Under the no-refresh regime (2026-07-14) this row is either the
    last APPLIED candidate run (coupling commits it) or the strategy's last
    full backtest — an old row just means nothing changed since, not a fault.
    Median stop/target anchors are recomputed from that run's persisted trades
    with the same filters as run_backtest's in-memory version (truthy
    signal_stop/signal_target AND truthy entry_price; percentile_cont
    interpolates the even-count midpoint exactly like statistics.median).

    Returns None (→ caller runs+persists a fresh baseline) only when no
    primary row exists at all."""
    import json
    import psycopg2
    with psycopg2.connect(os.environ['POSTGRES_URI']) as c, c.cursor() as cur:
        cur.execute("""
            SELECT run_id, total_sharpe, end_date, config_json
              FROM strategy_backtest_runs
             WHERE strategy_id = %s AND primary_window = TRUE
             ORDER BY run_at DESC
             LIMIT 1
        """, (strategy_id,))
        row = cur.fetchone()
        if row is None:
            return None
        run_id, sharpe, end_date, config_json = row[0], row[1], row[2], row[3]
        cur.execute("""
            SELECT percentile_cont(0.5) WITHIN GROUP
                     (ORDER BY abs(entry_price - signal_stop) / entry_price)
                     FILTER (WHERE signal_stop IS NOT NULL AND signal_stop <> 0
                               AND entry_price IS NOT NULL AND entry_price <> 0),
                   percentile_cont(0.5) WITHIN GROUP
                     (ORDER BY abs(signal_target - entry_price) / entry_price)
                     FILTER (WHERE signal_target IS NOT NULL AND signal_target <> 0
                               AND entry_price IS NOT NULL AND entry_price <> 0)
              FROM strategy_backtest_trades
             WHERE run_id = %s
        """, (run_id,))
        med_stop, med_target = cur.fetchone()
    cfg = config_json if isinstance(config_json, dict) else json.loads(config_json or '{}')
    return {
        'sharpe': sharpe,
        'median_stop_pct': med_stop,
        'median_target_pct': med_target,
        'end_date': end_date,
        'max_hold_days': cfg.get('max_hold_days'),
    }


def _load_recs(rec_date=None) -> list:
    import psycopg2
    # 'noted' rows (low-confidence SIZE recs) are still bracket-coupled: the
    # backtest is the arbiter for stop/TP/hold, confidence only gates size.
    # coupling_outcome IS NULL keeps re-runs idempotent (each row is backtested
    # at most once).
    sql = """SELECT id, strategy_id, stop_delta_pct, target_delta_pct,
                    hold_days_delta, reasoning
               FROM strategy_sizing_recommendations
              WHERE action_taken IN ('pending', 'noted')
                AND coupling_outcome IS NULL"""
    params = []
    if rec_date:
        sql += " AND rec_date = %s"; params.append(rec_date)
    else:
        # No explicit date → scope to the LATEST batch only. Otherwise we
        # would re-process the entire backlog (e.g. 36 superseded rows from
        # prior weeks), each triggering a multi-hour backtest and applying off stale
        # recommendations. MAX over zero rows → NULL → returns empty (same
        # as today's no-pending case).
        sql += (" AND rec_date = (SELECT MAX(rec_date) FROM strategy_sizing_recommendations"
                " WHERE action_taken IN ('pending', 'noted'))")
    with psycopg2.connect(os.environ['POSTGRES_URI']) as c, c.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _mark_outcome(rec_id, outcome, note):
    """Record the bracket decision WITHOUT touching action_taken — that column
    is the Monday-handoff SIZE gate ('pending'/'noted'), decoupled 2026-07-14
    (previously a coupling reject silently dropped the size rec too)."""
    import psycopg2
    with psycopg2.connect(os.environ['POSTGRES_URI']) as c, c.cursor() as cur:
        cur.execute("""UPDATE strategy_sizing_recommendations
                          SET coupling_outcome=%s, reasoning=COALESCE(reasoning,'')|| %s
                        WHERE id=%s""", (outcome, ' | ' + note, rec_id))
        c.commit()


def _replace_stops_for_applied(strategy_id: str, stop_pct: float, log=print) -> dict:
    """Re-anchor the broker stop of each recently-filled open position of this
    strategy to the backtest-validated stop DISTANCE from its original entry:
    long → entry·(1−stop_pct); short → entry·(1+stop_pct).

    (The pre-2026-07-14 flow multiplied the stop PRICE by (1+δ) inside
    position_recommender BEFORE any backtest ran — which tightens a long stop
    for a positive "widen" δ. This path replaces it: distance semantics, and
    only after the coupling gate passed.)

    Live/dry behaviour is owned by alpaca_replace_stop (OPENCLAW_ALPACA_LIVE_REPLACE).
    Broker rejections (already-closed orders, market-side constraints) are logged
    and never fatal. Returns {'attempted': n, 'replaced': n, 'failed': n}.
    """
    import psycopg2
    from execution.alpaca_replace_stop import replace_stop_for_coid
    out = {'attempted': 0, 'replaced': 0, 'failed': 0}
    try:
        with psycopg2.connect(os.environ['POSTGRES_URI']) as c, c.cursor() as cur:
            cur.execute("""SELECT client_order_id, ticker, direction,
                                  entry_price, stop_price
                             FROM alpaca_submissions
                            WHERE strategy_id = %s
                              AND submitted_at >= NOW() - INTERVAL '14 days'
                              AND broker_status IN ('filled', 'partial')
                              AND alpaca_order_id IS NOT NULL
                              AND stop_price IS NOT NULL""", (strategy_id,))
            rows = cur.fetchall()
    except Exception as e:
        log(f'[coupling] {strategy_id}: stop-replace position query failed ({e})')
        return out
    for coid, ticker, direction, entry, _old_stop in rows:
        try:
            entry = float(entry) if entry is not None else None
        except (TypeError, ValueError):
            entry = None
        if not entry or entry <= 0:
            log(f'[coupling] {strategy_id}/{ticker}: no usable entry_price — skip stop replace')
            continue
        is_short = str(direction or 'long').lower() == 'short'
        if is_short:
            new_stop = entry * (1.0 + float(stop_pct))   # stop above entry
        else:
            new_stop = entry * (1.0 - float(stop_pct))   # stop below entry
        new_stop = max(0.01, new_stop)
        out['attempted'] += 1
        try:
            r = replace_stop_for_coid(coid, round(new_stop, 2))
            status = (r or {}).get('status')
            if status in ('replaced', 'skipped_dry_run'):
                out['replaced'] += 1
            else:
                out['failed'] += 1
            log(f'[coupling] {strategy_id}/{ticker}: stop → {new_stop:.2f} ({status})')
        except Exception as e:
            out['failed'] += 1
            log(f'[coupling] {strategy_id}/{ticker}: stop replace failed ({e})')
    return out


def run(rec_date=None, dry_run: bool = False, log=print) -> dict:
    if not gate_on():
        log('[coupling] OPENCLAW_BACKTEST_COUPLED_RECS off — skipping (no-op).')
        return {'skipped': True}
    from strategies import eligibility_manager as em
    recs = _load_recs(rec_date)
    applied, rejected = 0, 0
    for rec in recs:
        sid = rec['strategy_id']
        if not has_actionable_delta(rec):
            continue
        base = _stored_baseline(sid)
        if base is None:
            # Never backtested through the unified engine — run + PERSIST a
            # baseline (becomes the canonical primary row; self-healing).
            log(f'[coupling] {sid}: no canonical baseline — running one (persists)')
            base = _run_metrics(sid, None, commit=True)
        base_sharpe = float(base.get('sharpe') or 0.0)
        cand_stop = candidate_pct(base.get('median_stop_pct'), rec.get('stop_delta_pct'), DEFAULT_STOP_PCT)
        cand_tgt = candidate_pct(base.get('median_target_pct'), rec.get('target_delta_pct'), DEFAULT_TARGET_PCT)
        # max_hold candidate anchors to the horizon the baseline actually ran
        # with (stored config_json = the configured value at that time, so
        # applied hold changes compound; DEFAULT_MAX_HOLD when absent).
        cand_hold = candidate_max_hold(base.get('max_hold_days') or DEFAULT_MAX_HOLD,
                                       rec.get('hold_days_delta'))
        if cand_stop is None and cand_tgt is None and cand_hold is None:
            continue
        regimes = _eligible_regimes(sid)
        # Stop/target ride the per-regime param_override; max_hold is a single
        # top-level run_backtest arg. ONE candidate backtest evaluates the whole
        # config change together, full-window, on a deferred-commit connection.
        # Selected-universe decisions (2026-07-27): when the strategy's chosen
        # predicate is a ladder tier, the candidate runs as a chosen-universe
        # PROBE (always rolled back) and is judged against the chosen tier's
        # shrink-store baseline; APPLY then re-runs the applied config on the
        # FULL universe and commits THAT as the new canonical primary row, so
        # canonical stays the shrink baseline. Legacy full-universe strategies
        # keep the original flow: APPLY commits the candidate run itself.
        cand_map = {r: {k: v for k, v in (('stop_pct', cand_stop), ('target_pct', cand_tgt)) if v is not None}
                    for r in regimes}
        resolver, chosen_tier = _chosen_resolver(sid)
        if resolver is not None:
            cb = _chosen_baseline_sharpe(sid, chosen_tier)
            if cb is not None:
                base_sharpe = float(cb)
                log(f'[coupling] {sid}: baseline = chosen-tier {chosen_tier} '
                    f'TOTAL Sharpe {base_sharpe:.2f} (shrink store)')
            else:
                log(f'[coupling] {sid}: chosen tier {chosen_tier} has no shrink '
                    'baseline — judging against the full-universe canonical baseline')
        cconn, cand_run_id, cand = _run_candidate(sid, cand_map, max_hold_days=cand_hold,
                                                  resolver=resolver)
        canonical_committed = False
        try:
            cand_sharpe = float(cand.get('sharpe') or 0.0)
            cand_n = int(cand.get('total_trades') or 0)
            ok = qualifies(baseline_sharpe=base_sharpe, candidate_sharpe=cand_sharpe, candidate_n_trades=cand_n)
            note = (f'dSharpe {cand_sharpe - base_sharpe:+.3f} ({base_sharpe:.2f}->{cand_sharpe:.2f}) '
                    f'on {chosen_tier or "full"}, n={cand_n}')
            log(f'[coupling] {sid}: stop={cand_stop} target={cand_tgt} max_hold={cand_hold} '
                f'{note} -> {"APPLY" if ok else "reject"}')
            if not ok:
                cconn.rollback()
                if not dry_run:
                    _mark_outcome(rec['id'], 'rejected', 'coupling reject ' + note)
                rejected += 1
                continue
            if dry_run:
                cconn.rollback()
                applied += 1
                continue
            for r in regimes:
                em.set_params(strategy_id=sid, regime_state=r,
                              stop_pct=cand_stop, target_pct=cand_tgt,
                              max_hold_days=cand_hold,
                              actor='saturday_coupling',
                              reason='backtest-coupled stop/TP/max-hold: ' + note,
                              source='saturday_coupling',
                              bt_sharpe_before=base_sharpe, bt_sharpe_after=cand_sharpe,
                              bt_n_trades=cand_n)
            if resolver is not None:
                # The decision run was a chosen-universe probe — never canonical.
                cconn.rollback()
                cconn.close()
                log(f'[coupling] {sid}: refreshing full-universe canonical with the applied config')
                try:
                    cconn, cand_run_id, _full = _run_candidate(sid, cand_map, max_hold_days=cand_hold)
                except Exception as e:
                    log(f'[coupling] {sid}: full-universe canonical refresh FAILED ({e}) — '
                        'params are applied; canonical heals on the next fleet run')
                    cconn = None
            if cconn is not None:
                # Params are in force → commit the full-universe run as the new
                # canonical primary row (tagged so it's auditable as an apply).
                with cconn.cursor() as cur:
                    cur.execute("""UPDATE strategy_backtest_runs SET notes=%s
                                    WHERE run_id=%s""",
                                ('saturday_coupling apply: ' + note, cand_run_id))
                cconn.commit()
                canonical_committed = True
        finally:
            if cconn is not None:
                cconn.close()   # close without commit == rollback (reject/error paths)
        # Dashboard backtest panel rebuild — run_backtest skips it on
        # commit=False, so redo it here now that the run is committed.
        if canonical_committed:
            try:
                from backtest.backtest_panel import rebuild as _rebuild_panel
                _rebuild_panel(sid)
            except Exception as e:
                log(f'[coupling] {sid}: panel rebuild skipped: {e}')
        if resolver is not None and canonical_committed:
            # Refresh the chosen-tier sleeves + per-regime eligibility from the
            # just-committed canonical trades (fail-soft). No --adopt: universe
            # re-SELECTION belongs to the fleet/creation processes, not here.
            try:
                import subprocess
                import sys as _sys
                r = subprocess.run(
                    [_sys.executable, 'scripts/run_universe_shrink.py',
                     '--strategy', sid, '--reassign'],
                    cwd=str(_repo_root()), timeout=900, capture_output=True,
                    text=True, env={**os.environ, 'PYTHONPATH': 'src'})
                tail = (r.stdout or '').strip().split('\n')[-1] if (r.stdout or '').strip() else ''
                log(f'[coupling] {sid}: chosen-tier sleeve refresh rc={r.returncode}'
                    + (f' :: {tail[:120]}' if tail else ''))
            except Exception as e:
                log(f'[coupling] {sid}: sleeve refresh skipped ({e})')
        _mark_outcome(rec['id'], 'applied', 'coupling apply ' + note)
        # Re-anchor broker stops on currently-open positions to the validated
        # stop distance (post-gate only; live/dry owned by alpaca_replace_stop).
        if cand_stop is not None:
            rr = _replace_stops_for_applied(sid, cand_stop, log=log)
            if rr['attempted']:
                log(f'[coupling] {sid}: stop replacements '
                    f"{rr['replaced']}/{rr['attempted']} ok, {rr['failed']} failed")
        applied += 1
    log(f'[coupling] done: {applied} applied, {rejected} rejected, {len(recs)} recs scanned.')
    return {'applied': applied, 'rejected': rejected, 'scanned': len(recs)}


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--date')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    run(rec_date=a.date, dry_run=a.dry_run)
