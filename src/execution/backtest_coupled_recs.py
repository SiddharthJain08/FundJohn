"""Saturday backtest-coupling step.

For each fresh stop/TP/max-hold recommendation, backtest the change and apply it
to strategy_regime_params (all eligible regimes) IFF it raises Sharpe by
>= MIN_DELTA on >= MIN_TRADES trades. Gated by OPENCLAW_BACKTEST_COUPLED_RECS —
refuses to apply when the gate is OFF.

Accept: candidate_sharpe - baseline_sharpe >= 0.10 AND candidate_n_trades >= 30.

Scope (2026-05-31, operator): ALL per-strategy config — stop_pct, target_pct,
AND max_hold_days — is decided here via backtest, never in the manual proposals
queue. Stop/target are injected per-regime via param_override (re-anchored in
the simulate loop); max_hold_days is a single top-level run_backtest arg, so it
is backtested as one candidate value applied across the run and then persisted
to every eligible regime via set_params(max_hold_days=...).
"""
from __future__ import annotations
import os
from typing import Optional

MIN_DELTA = 0.10
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
    return (candidate_sharpe - baseline_sharpe) >= MIN_DELTA and candidate_n_trades >= MIN_TRADES


def _eligible_regimes(strategy_id) -> list:
    from execution import regime_param_resolver as rpr
    elig = [r for r in CANONICAL_REGIMES if rpr.is_eligible(strategy_id, r)]
    return elig or list(CANONICAL_REGIMES)


def _run_metrics(strategy_id, param_override, max_hold_days=None) -> dict:
    """Ephemeral backtest → metrics dict. commit=False (own-conn) so probe runs
    roll back — never persist nor rebuild the dashboard panel.

    ``max_hold_days`` (when not None) overrides the backtest's top-level hold
    horizon so a candidate max_hold can be evaluated against baseline. Stop/target
    candidates still ride ``param_override`` (per-regime). When max_hold_days is
    None, run_backtest uses its own DEFAULT_MAX_HOLD_DAYS — byte-identical to the
    pre-max-hold-coupling behaviour."""
    from backtest import unified_backtest as ub
    kwargs = dict(commit=False, param_override=param_override, return_metrics=True)
    if max_hold_days is not None:
        kwargs['max_hold_days'] = int(max_hold_days)
    _run_id, metrics = ub.run_backtest(strategy_id, **kwargs)
    return metrics


def _load_recs(rec_date=None) -> list:
    import psycopg2
    sql = """SELECT id, strategy_id, stop_delta_pct, target_delta_pct,
                    hold_days_delta, reasoning
               FROM strategy_sizing_recommendations
              WHERE action_taken = 'pending'"""
    params = []
    if rec_date:
        sql += " AND rec_date = %s"; params.append(rec_date)
    else:
        # No explicit date → scope to the LATEST pending batch only. Otherwise we
        # would re-process the entire pending backlog (e.g. 36 superseded rows from
        # prior weeks), each triggering a multi-hour backtest and applying off stale
        # recommendations. MAX over zero pending rows → NULL → returns empty (same
        # as today's no-pending case).
        sql += (" AND rec_date = (SELECT MAX(rec_date) FROM strategy_sizing_recommendations"
                " WHERE action_taken = 'pending')")
    with psycopg2.connect(os.environ['POSTGRES_URI']) as c, c.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _mark(rec_id, status, note):
    import psycopg2
    with psycopg2.connect(os.environ['POSTGRES_URI']) as c, c.cursor() as cur:
        cur.execute("""UPDATE strategy_sizing_recommendations
                          SET action_taken=%s, reasoning=COALESCE(reasoning,'')|| %s
                        WHERE id=%s""", (status, ' | ' + note, rec_id))
        c.commit()


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
        base = _run_metrics(sid, None)
        base_sharpe = float(base.get('sharpe') or 0.0)
        cand_stop = candidate_pct(base.get('median_stop_pct'), rec.get('stop_delta_pct'), DEFAULT_STOP_PCT)
        cand_tgt = candidate_pct(base.get('median_target_pct'), rec.get('target_delta_pct'), DEFAULT_TARGET_PCT)
        # max_hold candidate is anchored to DEFAULT_MAX_HOLD so it compares against
        # the same horizon the baseline backtest used (_run_metrics(None) → default).
        cand_hold = candidate_max_hold(DEFAULT_MAX_HOLD, rec.get('hold_days_delta'))
        if cand_stop is None and cand_tgt is None and cand_hold is None:
            continue
        regimes = _eligible_regimes(sid)
        # Stop/target ride the per-regime param_override; max_hold is a single
        # top-level run_backtest arg. One candidate backtest evaluates the whole
        # config change together (keeps it to 1 extra backtest per rec).
        cand_map = {r: {k: v for k, v in (('stop_pct', cand_stop), ('target_pct', cand_tgt)) if v is not None}
                    for r in regimes}
        cand = _run_metrics(sid, cand_map, max_hold_days=cand_hold)
        cand_sharpe = float(cand.get('sharpe') or 0.0)
        cand_n = int(cand.get('total_trades') or 0)
        ok = qualifies(baseline_sharpe=base_sharpe, candidate_sharpe=cand_sharpe, candidate_n_trades=cand_n)
        note = (f'dSharpe {cand_sharpe - base_sharpe:+.3f} ({base_sharpe:.2f}->{cand_sharpe:.2f}), '
                f'n={cand_n}')
        log(f'[coupling] {sid}: stop={cand_stop} target={cand_tgt} max_hold={cand_hold} '
            f'{note} -> {"APPLY" if ok else "reject"}')
        if not ok:
            if not dry_run:
                _mark(rec['id'], 'ignored', 'coupling reject ' + note)
            rejected += 1
            continue
        if dry_run:
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
        _mark(rec['id'], 'applied', 'coupling apply ' + note)
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
