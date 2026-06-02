"""Persist B1 shadow ledger rows + build/post the go/no-go report. Discord posting
uses requests (passes Cloudflare) with an explicit UA for safety."""
from __future__ import annotations
import os
import psycopg2
import psycopg2.extras
import requests


def _conn():
    return psycopg2.connect(os.environ['POSTGRES_URI'])


def persist_rows(mode, rows):
    """rows: list of dicts with the ledger fields. Writes to b1_shadow_exec_ledger."""
    if not rows:
        return 0
    with _conn() as c:
        cur = c.cursor()
        psycopg2.extras.execute_values(cur, """
            INSERT INTO b1_shadow_exec_ledger
              (mode, run_date, ticker, strategy_id, signal_id, regime_state,
               signed_qty, s_i, lam, actual_fill, close_t1, exec_ledger,
               naive_ledger, vwap_base_ledger, completion)
            VALUES %s""",
            [(mode, r.get('run_date'), r['ticker'], r.get('strategy_id'),
              r.get('signal_id'), r.get('regime_state'), r.get('signed_qty'),
              r.get('s_i'), r.get('lam'), r.get('actual_fill'), r.get('close_t1'),
              r.get('exec_ledger'), r.get('naive_ledger'), r.get('vwap_base_ledger'),
              r.get('completion')) for r in rows])
        c.commit()
    return len(rows)


def build_report(rows):
    """Aggregate beat-close vs BOTH baselines, overall + per regime. Pure."""
    rep = {'n': len(rows), 'beats_naive': 0, 'beats_base': 0,
           'total_exec_ledger': 0.0, 'total_naive_ledger': 0.0,
           'avg_completion': 0.0, 'by_regime': {}}
    for r in rows:
        ex = r.get('exec_ledger') or 0.0
        rep['total_exec_ledger'] += ex
        rep['total_naive_ledger'] += r.get('naive_ledger') or 0.0
        rep['avg_completion'] += r.get('completion') or 0.0
        if ex > (r.get('naive_ledger') or 0.0):
            rep['beats_naive'] += 1
        if ex > (r.get('vwap_base_ledger') or 0.0):
            rep['beats_base'] += 1
        g = r.get('regime_state') or 'UNKNOWN'
        rg = rep['by_regime'].setdefault(g, {'n': 0, 'exec_ledger': 0.0})
        rg['n'] += 1
        rg['exec_ledger'] += ex
    if rows:
        rep['avg_completion'] /= len(rows)
    return rep


def format_report(mode, rep):
    lines = [f"**B1 shadow ({mode})** — n={rep['n']}",
             f"beats naive-3:55: {rep['beats_naive']}/{rep['n']}  |  beats VWAP-base: {rep['beats_base']}/{rep['n']}",
             f"Σ exec-ledger: {rep['total_exec_ledger']:+.2f}  (naive baseline {rep['total_naive_ledger']:+.2f})",
             f"avg completion: {rep['avg_completion']:.0%}",
             "by regime: " + ", ".join(f"{g}:{v['exec_ledger']:+.1f}(n={v['n']})"
                                       for g, v in rep['by_regime'].items())]
    return "\n".join(lines)


def post_report(text, channel='data-alerts'):
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT webhook_urls->>%s FROM agent_registry WHERE id='botjohn'", (channel,))
        row = cur.fetchone()
    if not row or not row[0]:
        return False
    try:
        r = requests.post(row[0], json={'content': text[:1900]}, timeout=10,
                          headers={'User-Agent': 'OpenClaw-B1Shadow/1.0 (+botjohn)'})
        return 200 <= r.status_code < 300
    except Exception:
        return False
