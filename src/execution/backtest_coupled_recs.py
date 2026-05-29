"""Saturday backtest-coupling step.

For each fresh stop/TP recommendation, backtest the change and apply it to
strategy_regime_params (all eligible regimes) IFF it raises Sharpe by >= MIN_DELTA
on >= MIN_TRADES trades. Gated by OPENCLAW_BACKTEST_COUPLED_RECS — refuses to
apply when the gate is OFF.

Accept: candidate_sharpe - baseline_sharpe >= 0.10 AND candidate_n_trades >= 30.
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
CANONICAL_REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')


def gate_on() -> bool:
    return os.environ.get('OPENCLAW_BACKTEST_COUPLED_RECS') == '1'


def has_actionable_delta(rec: dict) -> bool:
    for k in ('stop_delta_pct', 'target_delta_pct'):
        v = rec.get(k)
        if v is not None and abs(float(v)) >= NOISE:
            return True
    return False


def candidate_pct(base: Optional[float], delta: Optional[float],
                  default: float) -> Optional[float]:
    if delta is None or abs(float(delta)) < NOISE:
        return None
    b = float(base) if base is not None else default
    return max(CLAMP_LO, min(CLAMP_HI, b * (1 + float(delta))))


def qualifies(*, baseline_sharpe: float, candidate_sharpe: float,
              candidate_n_trades: int) -> bool:
    return (candidate_sharpe - baseline_sharpe) >= MIN_DELTA and candidate_n_trades >= MIN_TRADES


def _eligible_regimes(strategy_id) -> list:
    from execution import regime_param_resolver as rpr
    elig = [r for r in CANONICAL_REGIMES if rpr.is_eligible(strategy_id, r)]
    return elig or list(CANONICAL_REGIMES)


def _run_metrics(strategy_id, param_override) -> dict:
    """Ephemeral backtest → metrics dict. commit=False (own-conn) so probe runs
    roll back — never persist nor rebuild the dashboard panel."""
    from backtest import unified_backtest as ub
    _run_id, metrics = ub.run_backtest(strategy_id, commit=False,
                                       param_override=param_override,
                                       return_metrics=True)
    return metrics


def _load_recs(rec_date=None) -> list:
    import psycopg2
    sql = """SELECT id, strategy_id, stop_delta_pct, target_delta_pct, reasoning
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
        if cand_stop is None and cand_tgt is None:
            continue
        regimes = _eligible_regimes(sid)
        cand_map = {r: {k: v for k, v in (('stop_pct', cand_stop), ('target_pct', cand_tgt)) if v is not None}
                    for r in regimes}
        cand = _run_metrics(sid, cand_map)
        cand_sharpe = float(cand.get('sharpe') or 0.0)
        cand_n = int(cand.get('total_trades') or 0)
        ok = qualifies(baseline_sharpe=base_sharpe, candidate_sharpe=cand_sharpe, candidate_n_trades=cand_n)
        note = f'dSharpe {cand_sharpe - base_sharpe:+.3f} ({base_sharpe:.2f}->{cand_sharpe:.2f}), n={cand_n}'
        log(f'[coupling] {sid}: stop={cand_stop} target={cand_tgt} {note} -> {"APPLY" if ok else "reject"}')
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
                          actor='saturday_coupling',
                          reason='backtest-coupled stop/TP: ' + note,
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
