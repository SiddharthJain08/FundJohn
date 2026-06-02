"""B1 shadow driver (gated OPENCLAW_B1_SHADOW). Modes: 'case_study' (curated history),
'live_shadow' (today's opens). For each order: resolve the worked session + its realized
30-min bars + trailing history → plan (sᵢ-decay) and base-plan (lam=0) → simulate both →
persist. Observation-only: never submits an order.

Grounding (2026-06-02): case studies carry es.signal_date as run_date and resolve the
worked T+1 session = the first bar-session STRICTLY AFTER signal_date (reconstructing the
now-NULL target_date); live orders use the exact run_date."""
from __future__ import annotations
import os
import sys
import argparse
import datetime as dt
import pandas as pd

from execution.b1_planner import expected_volume_profile, plan, RTH_BUCKETS
from execution.b1_simulator import simulate
from execution import b1_order_source as src_mod
from execution import b1_ledger

LAM = 4.0                      # principled prior (spec §5); validated, not fit
TRAILING_DAYS = 20
_PARQUET = 'data/master/prices_30m.parquet'
_OPEN_BUCKET_UTC = (13, 30)   # 9:30 ET = 13:30 UTC (EDT); close 16:00 ET = 20:00 UTC


def bucket_of(datetime_iso):
    """Map a 30-min bar timestamp (UTC ISO) to RTH bucket 0..12, or None if outside
    [9:30,16:00) ET. Assumes EDT (UTC-4); the parquet stores tz-aware UTC."""
    t = dt.datetime.fromisoformat(str(datetime_iso))
    minutes = (t.hour - _OPEN_BUCKET_UTC[0]) * 60 + (t.minute - _OPEN_BUCKET_UTC[1])
    if minutes < 0 or minutes >= RTH_BUCKETS * 30:
        return None
    return minutes // 30


def _by_date(df_t):
    """df_t: prices_30m rows for one ticker → {date_str: {bucket:{vwap,high,low,volume}}}."""
    by_date = {}
    for _, r in df_t.iterrows():
        b = bucket_of(r['datetime'].isoformat())
        if b is None:
            continue
        by_date.setdefault(str(r['date']), {})[b] = {
            'vwap': float(r['vwap']), 'high': float(r['high']),
            'low': float(r['low']), 'volume': float(r['volume'])}
    return by_date


def _resolve_session(by_date, run_date, resolve_next_session):
    """Pick the session to work. live (exact): run_date if present. case_study
    (resolve_next_session): first session STRICTLY AFTER run_date (=signal_date) →
    reconstructs T+1. Returns the session-date string or None (→ skip, never impute)."""
    if resolve_next_session:
        later = sorted(d for d in by_date if d > run_date)
        return later[0] if later else None
    return run_date if run_date in by_date else None


def _trailing(by_date, session):
    """Trailing-N bucket-volume vectors for sessions strictly before `session` (causal)."""
    past = sorted(d for d in by_date if d < session)[-TRAILING_DAYS:]
    return [[by_date[d].get(b, {}).get('volume', 0.0) for b in range(RTH_BUCKETS)]
            for d in past]


def _score_order(o, df_ticker):
    by_date = _by_date(df_ticker)
    session = _resolve_session(by_date, o['run_date'], o.get('resolve_next_session', False))
    if not session or session not in by_date:
        return None  # no coverage — skip (don't impute)
    day_bars = by_date[session]
    close_t1 = o['close_t1']
    if close_t1 is None:
        last = max(day_bars)                       # last available bucket vwap ≈ close proxy
        close_t1 = day_bars[last]['vwap']
    profile = expected_volume_profile(_trailing(by_date, session))
    alpha = simulate(plan(o['signed_qty'], o['s_i'], profile, LAM), day_bars, close_t1, o.get('naive_fill'))
    base = simulate(plan(o['signed_qty'], 0.0, profile, LAM), day_bars, close_t1, o.get('naive_fill'))
    if alpha['actual_fill'] is None:
        return None
    return {**o, 'lam': LAM, 'run_date': session, 'close_t1': close_t1,
            'actual_fill': alpha['actual_fill'], 'exec_ledger': alpha['exec_ledger'],
            'naive_ledger': alpha['naive_ledger'], 'vwap_base_ledger': base['exec_ledger'],
            'completion': alpha['completion']}


def run(mode='case_study', n_losers=25, n_movers=25, run_date=None, post=False):
    if os.environ.get('OPENCLAW_B1_SHADOW') != '1':
        return {'status': 'gate_off', 'persisted': 0}
    orders = (src_mod.case_study_orders(n_losers, n_movers) if mode == 'case_study'
              else src_mod.live_shadow_orders(run_date or dt.date.today().isoformat()))
    df = pd.read_parquet(_PARQUET, columns=['date', 'datetime', 'ticker', 'high', 'low', 'vwap', 'volume'])
    rows = []
    for o in orders:
        if not o.get('run_date'):
            continue
        scored = _score_order(o, df[df['ticker'] == o['ticker']])
        if scored:
            rows.append(scored)
    persisted = b1_ledger.persist_rows(mode, rows)
    rep = b1_ledger.build_report(rows)
    text = b1_ledger.format_report(mode, rep)
    print(text)
    if post:
        b1_ledger.post_report(text)
    return {'status': 'ok', 'persisted': persisted, 'report': rep}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mode', choices=['case_study', 'live_shadow'], default='case_study')
    ap.add_argument('--run-date')
    ap.add_argument('--n-losers', type=int, default=25)
    ap.add_argument('--n-movers', type=int, default=25)
    ap.add_argument('--post', action='store_true')
    a = ap.parse_args()
    out = run(mode=a.mode, n_losers=a.n_losers, n_movers=a.n_movers, run_date=a.run_date, post=a.post)
    print(out['status'], 'persisted=', out.get('persisted'))
    return 0 if out['status'] in ('ok', 'gate_off') else 1


if __name__ == '__main__':
    sys.exit(main())
