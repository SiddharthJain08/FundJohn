#!/usr/bin/env python3
"""bench_relative_sizing_replay.py — size today's book twice, flag OFF and ON,
and print the per-ticker diff. READ-ONLY: no DB writes, no broker calls, no
Discord posts (all posting hooks are stubbed; broker positions are stubbed to
{} so orders == targets). Spec §2.5.2 — this is the parity artefact for rule C.

Run outside 13:00–20:15 UTC. Usage:
    python3 scripts/bench_relative_sizing_replay.py --nav 152000 [--regime LOW_VOL]
--nav is required (read it with: /root/go/bin/alpaca account get --jq .equity).
"""
from __future__ import annotations
import argparse
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))


def diff_books(off: dict, on: dict, bench: set) -> dict:
    g0 = sum(abs(v) for v in off.values()); g1 = sum(abs(v) for v in on.values())
    b0 = (sum(abs(v) for t, v in off.items() if t in bench) / g0) if g0 else 0.0
    b1 = (sum(abs(v) for t, v in on.items() if t in bench) / g1) if g1 else 0.0
    moves = sorted(((t, off.get(t, 0.0), on.get(t, 0.0), on.get(t, 0.0) - off.get(t, 0.0))
                    for t in set(off) | set(on)), key=lambda r: -abs(r[3]))
    return {'dropped': sorted(set(off) - set(on)), 'added': sorted(set(on) - set(off)),
            'moves': moves, 'gross_off': g0, 'gross_on': g1, 'beta_off': b0, 'beta_on': b1}


def _load_env():
    for line in (ROOT / '.env').read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))


def _regime_params(regime: str) -> dict:
    import psycopg2, psycopg2.extras
    with psycopg2.connect(os.environ['POSTGRES_URI']) as c:
        with c.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute('SELECT * FROM regime_sizer_params WHERE regime_state = %s', (regime,))
            row = cur.fetchone()
            return dict(row) if row else {}


def _current_regime() -> str:
    import psycopg2
    with psycopg2.connect(os.environ['POSTGRES_URI']) as c:
        with c.cursor() as cur:
            cur.execute('SELECT state FROM intraday_regime_states ORDER BY ts_utc DESC LIMIT 1')
            row = cur.fetchone()
            return row[0] if row else 'LOW_VOL'


def _size(nav: float, regime: str, flag_on: bool) -> dict:
    import execution.regime_blended_sizer as _sizer
    from execution import benchmark_sizing as bz
    os.environ['OPENCLAW_EOD_RECONCILE'] = '1'
    os.environ['OPENCLAW_INTRADAY_REDEPLOY'] = '0'
    os.environ['OPENCLAW_CLOSE_PROXY_SNAPSHOT'] = '0'
    if flag_on: os.environ[bz.BENCH_SIZING_ENV] = '1'
    else:       os.environ.pop(bz.BENCH_SIZING_ENV, None)
    _sizer._load_broker_positions_usd = lambda: {}
    _sizer._post_corr_cumsharpe_log = lambda line: None
    _sizer._post_flatten_alert = lambda *a, **k: None
    _sizer._post_ops_alert = lambda *a, **k: None
    _sizer._maybe_flatten_zero_conviction = lambda *a, **k: None
    account = {'equity': nav, 'regt_buying_power': 2 * nav, 'long_market_value': 0, 'cash': nav}
    orders = _sizer.size_positions(signals=[], account_state=account, regime={'state': regime},
                                   run_date=date.today(), strategy_state={},
                                   regime_params=_regime_params(regime), confirmer=lambda p: {})
    return {o['ticker']: float(o['target_usd']) for o in orders
            if o.get('action') not in ('close_long', 'close_short') and 'target_usd' in o}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--nav', type=float, required=True)
    ap.add_argument('--regime', default=None)
    ap.add_argument('--top', type=int, default=25)
    a = ap.parse_args(argv)
    _load_env()
    from execution import benchmark_sleeve as bsl
    regime = a.regime or _current_regime()
    bench_ids = bsl.load_benchmark_sleeve_ids()
    off = _size(a.nav, regime, False)
    on = _size(a.nav, regime, True)
    # Benchmark tickers = the beta sleeve's ticker when the registry flags it.
    bench = ({'SPY'} & (set(off) | set(on))) if 'S_beta_spy' in bench_ids else set()
    d = diff_books(off, on, bench)
    print(f'regime={regime} nav={a.nav:.0f} bench_ids={sorted(bench_ids)} bench_tickers={sorted(bench)}')
    print(f"gross OFF={d['gross_off']:.0f} ON={d['gross_on']:.0f}  beta_share OFF={d['beta_off']:.3f} ON={d['beta_on']:.3f}")
    print(f"dropped ({len(d['dropped'])}): {d['dropped']}")
    print(f"added   ({len(d['added'])}): {d['added']}")
    print(f"{'ticker':10s} {'OFF':>12s} {'ON':>12s} {'delta':>12s}")
    for t, o, n, dl in d['moves'][:a.top]:
        print(f'{t:10s} {o:12.0f} {n:12.0f} {dl:12.0f}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
