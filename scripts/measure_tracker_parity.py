#!/usr/bin/env python3
"""measure_tracker_parity.py — two-checkout replay parity for the D3 tracker refactor.

  capture --out PATH [--repo PATH] [--run-ids a,b,c | --rows-json PATH] [--env-file F]
  compare A.json B.json            (rc 0 iff every run is IDENTICAL on all fields)

`capture` replays stored backtest runs (strategy + the provenance flags recorded in
their config_json) through `backtest.unified_backtest.run_backtest` with a MagicMock
connection and `commit=False`, spying on the INSERTs. It records the trade rows, the
config_json string and the run row with `run_id` and `code_sha` stripped. Capture once
on the pre-D3 checkout and once on the D3 checkout (same data snapshot, same
PYTHONHASHSEED=0 — determinism 1acb7eb), then `compare`.

This is a controller operation reading the real masters and Postgres: run it as a
transient systemd unit outside 18:45-20:45Z and never alongside a fleet unit.
`--rows-json` (a JSON list of [run_id, strategy_id, config_json]) replaces Postgres
and is the test seam.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

DEFAULT_REPO = Path(__file__).resolve().parents[1]


def replay_kwargs(config: dict) -> tuple[dict, dict]:
    kw = {'fill_model': config.get('fill_model', 'same_close')}
    for k in ('start_date', 'end_date', 'max_hold_days'):
        if config.get(k) is not None:
            kw[k] = config[k]
    env: dict = {}

    def put(name, val):
        env[name] = str(val)

    if 'gap_fill' in config:
        put('OPENCLAW_BT_GAP_FILL', config['gap_fill'])
    if 'financials_pit' in config:
        put('OPENCLAW_FINANCIALS_PIT', '1' if config['financials_pit'] else '0')
    put('OPENCLAW_MACRO_PIT', '1' if config.get('macro_pit') else '0')
    if 'target_mode' in config:
        put('OPENCLAW_TARGET_MODE', config['target_mode'])
    if 'double_touch' in config:
        put('OPENCLAW_BT_DOUBLE_TOUCH', config['double_touch'])
    if 'event_gate' in config:
        put('OPENCLAW_BT_EVENT_GATE', '1' if config['event_gate'] == 'on' else '0')
    if 'intraday_exit' in config:
        put('OPENCLAW_BT_INTRADAY_EXIT', '0' if config['intraday_exit'] == 'off' else '1')
    if 'asset_gate' in config:
        put('OPENCLAW_BT_ASSET_GATE', config['asset_gate'])
    rf = config.get('rf')
    if isinstance(rf, dict) and 'source' in rf:
        put('OPENCLAW_RF_SOURCE', rf['source'])
    return kw, env


def capture_one(ub, strategy_id: str, kwargs: dict, env: dict) -> dict:
    seen = {'run': None, 'trades': []}
    cur = MagicMock()

    def exec_spy(sql, params=None):
        if 'INSERT INTO strategy_backtest_runs' in str(sql):
            seen['run'] = list(params)
    cur.execute.side_effect = exec_spy
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = lambda s: s
    conn.__exit__ = MagicMock(return_value=False)

    def spy(c, sql, rows, *a, **k):
        if 'strategy_backtest_trades' in str(sql):
            seen['trades'].extend(list(r) for r in rows)

    with patch.object(ub.psycopg2.extras, 'execute_values', spy), \
            patch.dict(os.environ, env):
        ub.run_backtest(strategy_id, conn=conn, commit=False,
                        instrument_class=ub._resolve_instrument_class(strategy_id), **kwargs)
    run = seen['run']
    if run is None:
        raise RuntimeError(f'{strategy_id}: no strategy_backtest_runs INSERT observed')
    cfg = next((p for p in run if isinstance(p, str) and p.startswith('{')
                and 'max_hold_days' in p), None)
    return {'trade_rows': json.loads(json.dumps([r[1:] for r in seen['trades']], default=str)),
            'config_json': cfg,
            'run_row': json.dumps([p for i, p in enumerate(run) if i not in (0, 2)],
                                  default=str)}


def _first_diff(a, b):
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            return i
    return min(len(a), len(b))


def _cfg_keys(a, b):
    try:
        da, db = json.loads(a), json.loads(b)
    except (TypeError, ValueError):
        return ['<unparseable>']
    return sorted(k for k in set(da) | set(db) if da.get(k) != db.get(k))


def compare(a: dict, b: dict) -> tuple[bool, list[str]]:
    ra, rb = a.get('runs', {}), b.get('runs', {})
    lines, ok = [], True
    for rid in sorted(set(ra) | set(rb)):
        x, y = ra.get(rid), rb.get(rid)
        if x is None or y is None:
            sid = (x or y).get('strategy_id')
            lines.append(f"[tracker_parity] run={rid} sid={sid} DIFF(missing in "
                         f"{'A' if x is None else 'B'})")
            ok = False
            continue
        ta, tb = x['trade_rows'], y['trade_rows']
        t_ok = ta == tb
        tr = 'IDENTICAL' if t_ok else f'DIFF(n={len(ta)}/{len(tb)} first={_first_diff(ta, tb)})'
        c_ok = x['config_json'] == y['config_json']
        cf = 'IDENTICAL' if c_ok else \
            f"DIFF(keys={','.join(_cfg_keys(x['config_json'], y['config_json']))})"
        r_ok = x['run_row'] == y['run_row']
        rr = 'IDENTICAL' if r_ok else 'DIFF'
        ok = ok and t_ok and c_ok and r_ok
        lines.append(f"[tracker_parity] run={rid} sid={x.get('strategy_id')} "
                     f"trades={tr} config_json={cf} run_row={rr}")
    for ln in lines:
        print(ln)
    return ok, lines


def _load_env_file(path):
    try:
        with open(path) as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith('#') and '=' in ln:
                    k, v = ln.split('=', 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass


def load_rows(run_ids=None, rows_json=None):
    """[(run_id, strategy_id, config_dict)]. --rows-json never touches Postgres."""
    if rows_json:
        with open(rows_json) as f:
            raw = json.load(f)
    else:
        import psycopg2
        uri = os.environ['POSTGRES_URI']
        with psycopg2.connect(uri) as c, c.cursor() as cur:
            if run_ids:
                cur.execute('SELECT run_id, strategy_id, config_json FROM '
                            'strategy_backtest_runs WHERE run_id = ANY(%s)', (run_ids,))
                raw = cur.fetchall()
            else:
                raw = []
                for pred in ("config_json->>'exit_hook' = 'true'",
                             "(config_json->>'intraday_trades')::int > 0",
                             "COALESCE(config_json->>'exit_hook','') <> 'true' AND "
                             "COALESCE((config_json->>'intraday_trades')::int,0) = 0"):
                    cur.execute('SELECT run_id, strategy_id, config_json FROM '
                                'strategy_backtest_runs WHERE primary_window = true AND '
                                + pred + ' ORDER BY run_at DESC LIMIT 1')
                    raw.extend(cur.fetchall())
    out = []
    for rid, sid, cfg in raw:
        out.append((str(rid), sid, json.loads(cfg) if isinstance(cfg, str) else dict(cfg)))
    return out


def cmd_capture(a) -> int:
    if os.environ.get('PYTHONHASHSEED') != '0':
        env = dict(os.environ, PYTHONHASHSEED='0')
        os.execvpe(sys.executable, [sys.executable] + sys.argv, env)
    repo = str(Path(a.repo).resolve())
    for p in (os.path.join(repo, 'src'), repo):  # repo ends up first
        sys.path.insert(0, p)
    _load_env_file(a.env_file)
    ids = [s for s in a.run_ids.split(',') if s] if a.run_ids else None
    rows = load_rows(ids, a.rows_json)
    import backtest.unified_backtest as ub
    runs = {}
    for rid, sid, cfg in rows:
        kw, env = replay_kwargs(cfg)
        res = capture_one(ub, sid, kw, env)
        res['strategy_id'] = sid
        runs[rid] = res
        print(f'[tracker_parity] captured run={rid} sid={sid} trades={len(res["trade_rows"])}')
    Path(a.out).write_text(json.dumps({'repo': repo, 'runs': runs}, indent=1, sort_keys=True))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    c = sub.add_parser('capture')
    c.add_argument('--out', required=True)
    c.add_argument('--repo', default=str(DEFAULT_REPO))
    g = c.add_mutually_exclusive_group()
    g.add_argument('--run-ids', default=None)
    g.add_argument('--rows-json', default=None)
    c.add_argument('--env-file', default='/root/openclaw/.env')
    d = sub.add_parser('compare')
    d.add_argument('a')
    d.add_argument('b')
    a = ap.parse_args(argv)
    if a.cmd == 'capture':
        return cmd_capture(a)
    docs = []
    for pth in (a.a, a.b):
        with open(pth) as f:
            docs.append(json.load(f))
    ok, _ = compare(*docs)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
