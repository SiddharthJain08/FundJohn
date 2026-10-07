#!/usr/bin/env python3
"""universe_parity_report.py — read-only before/after table for the backtest UNIVERSE
PARITY epoch (Phase 3, 2026-10-06). The operator reads this BEFORE any
`activation_assigner --all` / weights rebuild (runbook: docs/superpowers/plans/
2026-10-06-universe-parity-epoch.md).

Per live strategy: the PRE-epoch run (latest row dated before the epoch start, primary or
not — the fleet demotes the previous row on every new run — same window_kind as the post
row) vs the FIRST post-epoch run (earliest row at/after the epoch start that carries
config_json.universe_bound_source): universe_size, bound source/tier, total_sharpe,
total_trades, per-regime sharpe, and the per-regime activation verdict under the bench
rule for both, computed with activation_assigner._judge (the pure judge — the same code the
assigner applies) fed the bench vector from load_bench_sharpe (sleeve run, else
pipeline_config.strategy_activation_bench_sharpe) + the pipeline_config excess/min_trades.
Judged strict (no hysteresis band); the CURRENT live eligibility is shown beside it.
Nothing is applied. Sorted by |delta sharpe| descending. Writes <out>.csv and <out>.md.

Queries are indexed only: strategy_backtest_runs by strategy_id = ANY(...) (idx on
(strategy_id, run_at)), strategy_backtest_regimes by run_id = ANY(...) (idx_backtest_regimes_run),
strategy_regime_params by strategy_id. strategy_backtest_trades is never touched.

Usage: python3 scripts/universe_parity_report.py --out PATH_BASE [--since DATE] [--env-file P]
         [--manifest P] [--data-dir P]
"""
import argparse, csv, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))
from universe_parity_gate import _aware, epoch_start, read_uri  # noqa: E402

REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')


def judge_activation(regime_rows, *, bench, excess=0.0, min_trades=None, instrument_class='equity'):
    """{regime: bool} under the bench rule (strict, prior=None), via the assigner's pure judge.
    regime_rows: iterable of dicts {regime_state, sharpe, trade_count, max_dd_pct, calmar}."""
    from backtest.activation_assigner import _judge, _resolve_bench
    from backtest.regime_qualification import class_thresholds
    gate = class_thresholds(instrument_class)
    eff = gate['min_trades'] if min_trades is None else min_trades
    elig, _diag = _judge(list(regime_rows), gate, eff, _resolve_bench(bench),
                         {r: None for r in REGIMES}, False, excess=excess)
    return elig   # None when there are no regime rows


def _f(x):
    return None if x is None else float(x)


def _implication(pre, post, ela, elb, current, post_ref_failed):
    notes = []
    if post is None:
        return 'no post-epoch run yet — excluded from review'
    if post_ref_failed:
        notes.append('universe_filter_ref failed OPEN (static universe) — not epoch-comparable')
    if pre is None:
        notes.append('no pre-epoch baseline')
    if ela is not None and elb is not None:
        lost = [r for r in REGIMES if ela.get(r) and not elb.get(r)]
        gained = [r for r in REGIMES if elb.get(r) and not ela.get(r)]
        if lost:
            notes.append('would be DEACTIVATED in ' + ','.join(lost) + ' by the next activation_assigner --all')
        if gained:
            notes.append('would be ACTIVATED in ' + ','.join(gained))
        if not any(ela.values()) and not any(elb.values()):
            notes.append('ineligible in every regime before and after')
        if any(ela.values()) and not any(elb.values()):
            notes.append('DORMANT after (eligible nowhere) — falls out of the sizer')
    if pre is not None and pre['total_sharpe'] is not None and post['total_sharpe'] is not None \
            and pre['total_sharpe'] > 0 >= post['total_sharpe']:
        notes.append('Sharpe turned non-positive (fails the class gate)')
    if current is not None and elb is not None:
        diff = [r for r in REGIMES if bool(current.get(r)) != bool(elb.get(r))]
        if diff:
            notes.append('live eligibility differs from post-epoch verdict in ' + ','.join(diff))
    return '; '.join(notes) if notes else 'no activation change'


def pick_pair(runs, since):
    """(pre, post) for one strategy's runs. post = earliest run at/after `since` carrying
    universe_bound_source; pre = latest earlier run of the same window_kind (primary or not)."""
    rs = sorted(runs, key=lambda r: _aware(r['run_at']))
    post = next((r for r in rs if _aware(r['run_at']) >= since and r.get('bound_source') is not None), None)
    pre = next((r for r in reversed(rs) if _aware(r['run_at']) < since
                and (post is None or r['window_kind'] == post['window_kind'])), None)
    return pre, post


def assemble(live, manifest, runs_by_sid, regimes_by_run, since, *, bench, excess=0.0,
             min_trades=None, current_by_sid=None, inst_classes=None):
    """runs_by_sid: {sid: [run dict, ...]} with keys run_id, run_at, window_kind, primary_window,
    total_sharpe, total_trades, universe_size, bound_source, filter_ref_tier, bound_tier.
    regimes_by_run: {run_id: [{regime_state, sharpe, trade_count, max_dd_pct, calmar}]}."""
    current_by_sid = current_by_sid or {}
    inst_classes = inst_classes or {}
    out = []
    for sid in live:
        pre, post = pick_pair(runs_by_sid.get(sid, []), since)
        ic = inst_classes.get(sid) or 'equity'
        def act(run):
            if run is None:
                return None
            return judge_activation(regimes_by_run.get(run['run_id'], []), bench=bench, excess=excess,
                                    min_trades=min_trades, instrument_class=ic)
        ela, elb = act(pre), act(post)
        def sh(run):
            return {x['regime_state']: _f(x['sharpe']) for x in regimes_by_run.get(run['run_id'], [])} if run else {}
        has_ref = bool(((manifest.get(sid) or {}).get('metadata') or {}).get('universe_filter_ref'))
        failed_open = bool(post and post.get('bound_source') == 'none' and has_ref)
        d = None
        if pre and post and pre['total_sharpe'] is not None and post['total_sharpe'] is not None:
            d = float(post['total_sharpe']) - float(pre['total_sharpe'])
        cur = current_by_sid.get(sid)
        row = {'strategy_id': sid,
               'pre_run_at': pre['run_at'].isoformat() if pre else '',
               'post_run_at': post['run_at'].isoformat() if post else '',
               'pre_universe_size': pre.get('universe_size') if pre else None,
               'post_universe_size': post.get('universe_size') if post else None,
               'pre_bound_source': (pre.get('bound_source') or 'static(pre-epoch)') if pre else '',
               'post_bound_source': post.get('bound_source') if post else '',
               'post_tier': (post.get('filter_ref_tier') or post.get('bound_tier') or '') if post else '',
               'pre_sharpe': _f(pre['total_sharpe']) if pre else None,
               'post_sharpe': _f(post['total_sharpe']) if post else None,
               'delta_sharpe': d,
               'pre_trades': pre['total_trades'] if pre else None,
               'post_trades': post['total_trades'] if post else None}
        spre, spost = sh(pre), sh(post)
        for r in REGIMES:
            row[f'pre_sharpe_{r}'] = spre.get(r)
            row[f'post_sharpe_{r}'] = spost.get(r)
        row['pre_eligible'] = ','.join(r for r in REGIMES if ela and ela.get(r))
        row['post_eligible'] = ','.join(r for r in REGIMES if elb and elb.get(r))
        row['current_eligible'] = ','.join(r for r in REGIMES if cur and cur.get(r)) if cur is not None else ''
        row['implication'] = _implication(pre, post, ela, elb, cur, failed_open)
        out.append(row)
    out.sort(key=lambda r: (r['delta_sharpe'] is None, -abs(r['delta_sharpe'] or 0.0)))
    return out


def to_markdown(rows, since, bench, excess):
    paired = [r for r in rows if r['delta_sharpe'] is not None]
    lines = [f'# Universe parity epoch — before/after (epoch start {since:%Y-%m-%d} UTC)', '',
             f'bench vector (activation comparator): ' + ', '.join(f'{k}={v:.3f}' for k, v in sorted(bench.items()))
             + f'; excess={excess}',
             f'live strategies: {len(rows)}; with both a pre and post run: {len(paired)}; '
             f'no post run yet: {sum(1 for r in rows if not r["post_run_at"])}', '',
             '| strategy | univ pre→post | source/tier | Sharpe pre→post (Δ) | trades pre→post | elig pre → post | implication |',
             '|---|---|---|---|---|---|---|']
    def n(x, p=2):
        return '—' if x is None else (f'{x:.{p}f}' if isinstance(x, float) else str(x))
    for r in rows:
        dl = '—' if r['delta_sharpe'] is None else f"{r['delta_sharpe']:+.2f}"
        lines.append(f"| {r['strategy_id']} | {n(r['pre_universe_size'], 0)}→{n(r['post_universe_size'], 0)} "
                     f"| {r['post_bound_source'] or '—'}/{r['post_tier'] or '—'} "
                     f"| {n(r['pre_sharpe'])}→{n(r['post_sharpe'])} ({dl}) "
                     f"| {n(r['pre_trades'])}→{n(r['post_trades'])} "
                     f"| {r['pre_eligible'] or '∅'} → {r['post_eligible'] or '∅'} | {r['implication']} |")
    return '\n'.join(lines) + '\n'


def write_outputs(rows, out_base, since, bench, excess):
    if not rows:
        return
    for ext in ('.csv', '.md'):
        if os.path.exists(out_base + ext):
            raise SystemExit(f'refusing to overwrite {out_base + ext}')
    with open(out_base + '.csv', 'w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    with open(out_base + '.md', 'w') as fh:
        fh.write(to_markdown(rows, since, bench, excess))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True, help='output path base (writes .csv and .md; refuses to overwrite)')
    ap.add_argument('--since', default=None)
    ap.add_argument('--env-file', default='/root/openclaw/.env')
    ap.add_argument('--manifest', default='/root/openclaw/src/strategies/manifest.json')
    ap.add_argument('--data-dir', default='/root/openclaw/data')
    a = ap.parse_args(argv)
    since = epoch_start(a.since, a.data_dir)
    if since is None:
        print('epoch start unknown: no --since and no pre-universe-parity checkpoint', file=sys.stderr); return 2
    uri = read_uri(a.env_file)
    if not uri:
        print('POSTGRES_URI not found', file=sys.stderr); return 2
    import psycopg2
    from backtest import activation_assigner as aa
    man = json.load(open(a.manifest))['strategies']
    live = sorted(k for k, v in man.items() if v.get('state') == 'live')
    with psycopg2.connect(uri) as conn:
        conn.set_session(readonly=True)
        cur = conn.cursor()
        cur.execute("""SELECT strategy_id, run_id::text, run_at, window_kind, primary_window, total_sharpe,
                              total_trades, config_json->>'universe_size', config_json->>'universe_bound_source',
                              config_json->>'universe_filter_ref_tier', config_json->>'universe_bound_tier'
                         FROM strategy_backtest_runs WHERE strategy_id = ANY(%s)
                        ORDER BY strategy_id, run_at""", (live,))
        runs = {}
        for (sid, rid, run_at, wk, prim, sh, tr, usz, src, tier, btier) in cur.fetchall():
            runs.setdefault(sid, []).append({'run_id': rid, 'run_at': run_at, 'window_kind': wk,
                'primary_window': prim, 'total_sharpe': sh, 'total_trades': tr,
                'universe_size': float(usz) if usz not in (None, '') else None,
                'bound_source': src, 'filter_ref_tier': tier, 'bound_tier': btier})
        # pick the two run ids per strategy first, then fetch their regime rows by run_id only
        sel = assemble_selection(live, runs, since)
        regimes = {}
        if sel:
            cur.execute("""SELECT run_id::text, regime_state, sharpe, trade_count, max_dd_pct, calmar
                             FROM strategy_backtest_regimes WHERE run_id = ANY(%s::uuid[])""", (sorted(sel),))
            for rid, rs, sh, tc, dd, cal in cur.fetchall():
                regimes.setdefault(rid, []).append({'regime_state': rs, 'sharpe': sh, 'trade_count': tc,
                                                    'max_dd_pct': dd, 'calmar': cal})
        bench, _meta = aa.load_bench_sharpe(conn)
        excess = aa.get_activation_excess(conn.cursor())
        min_trades = aa.get_activation_min_trades(conn.cursor())
        current = {}
        for sid in live:
            if sid in runs:
                current[sid] = {r: v.get('eligible') for r, v in aa._load_prior(conn, sid).items()}
        ic = aa._load_instrument_classes()
    rows = assemble(live, man, runs, regimes, since, bench=bench, excess=excess, min_trades=min_trades,
                    current_by_sid=current, inst_classes=ic)
    write_outputs(rows, a.out, since, bench, excess)
    print(f'wrote {a.out}.csv and {a.out}.md ({len(rows)} live strategies)')
    return 0


def assemble_selection(live, runs, since):
    """The run ids assemble() will actually compare (so regime rows are fetched for ~2/strategy by run_id)."""
    ids = set()
    for sid in live:
        pre, post = pick_pair(runs.get(sid, []), since)
        ids.update(r['run_id'] for r in (pre, post) if r)
    return ids


if __name__ == '__main__':
    sys.exit(main())
