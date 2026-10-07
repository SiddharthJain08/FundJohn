#!/usr/bin/env python3
"""universe_parity_gate.py — read-only gate for the backtest UNIVERSE PARITY epoch
(Phase 3, 2026-10-06). Modelled on scripts/target_mode_flip_gate.py.

Prints 'OK' or 'NOT_YET' on the first line, then per-gate detail.

  G1 fleet uniformity: every manifest state=live strategy's LATEST primary
     backtest row (run_at >= the epoch start) carries config_json.universe_bound_source
     in ('cap', 'filter_ref'); OR the strategy has no universe_filter_ref and no
     backtest_universe_cap in the manifest, in which case 'none' is acceptable
     (listed separately — it stays on the static universe by design). At most
     --max-lagging exceptions (default 3). A strategy whose ref FAILED OPEN
     (source 'none' but the manifest has a ref), a row without the key (pre-epoch
     code), a row older than the epoch start, or source 'explicit' is lagging.
  G2 Sharpe sanity vs the PRE-epoch run: the latest row (primary or not — the
     fleet refresh demotes the previous row to primary_window=false when a new run
     lands, so every baseline is non-primary; same window_kind as the post row)
     dated BEFORE the epoch start. Median (after - before) total_sharpe must be
     >= --min-median-dsharpe (default -0.10) AND count(sharpe>0 after) >=
     --min-positive-frac (default 0.90) x count(sharpe>0 before). Needs >=
     --min-pairs (default 20) pairs.

Epoch start: --since YYYY-MM-DD[THH:MM] (UTC), else the rotate time stamped by
`epoch_universe_parity.sh rotate --run` in data/.refresh_backtests.done.pre-universe-parity-<YYYYMMDD>.since,
else the date (00:00 UTC) in the newest checkpoint file name.
NOT_YET goes to the operator with the numbers; this script never writes anything.

Usage: python3 scripts/universe_parity_gate.py [--max-lagging N] [--min-median-dsharpe X]
         [--min-positive-frac F] [--min-pairs N] [--since DATE] [--env-file PATH]
         [--manifest PATH] [--data-dir PATH]
"""
import argparse, glob, json, os, re, statistics, sys
from datetime import datetime, timezone

OK_SOURCES = ('cap', 'filter_ref')
TAG_RE = re.compile(r'\.refresh_backtests\.done\.pre-universe-parity-(\d{8})$')


def epoch_start(since, data_dir):
    """UTC datetime of the epoch start, or None."""
    if since:   # 'YYYY-MM-DD' (00:00 UTC) or 'YYYY-MM-DDTHH:MM' (UTC)
        fmt = '%Y-%m-%dT%H:%M' if 'T' in since else '%Y-%m-%d'
        return datetime.strptime(since, fmt).replace(tzinfo=timezone.utc)
    tags = sorted(m.group(1) for p in glob.glob(os.path.join(data_dir, '.refresh_backtests.done.pre-universe-parity-*'))
                  if (m := TAG_RE.search(p)))
    if not tags:
        return None
    # `rotate --run` stamps the exact rotate time (ISO UTC) next to the checkpoint; prefer it.
    stamp = os.path.join(data_dir, f'.refresh_backtests.done.pre-universe-parity-{tags[-1]}.since')
    try:
        txt = open(stamp).read().strip()
        return datetime.strptime(txt[:16], '%Y-%m-%dT%H:%M').replace(tzinfo=timezone.utc)
    except (OSError, ValueError):
        pass
    return datetime.strptime(tags[-1], '%Y%m%d').replace(tzinfo=timezone.utc)


def _aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def has_ref(entry):
    md = (entry or {}).get('metadata') or {}
    return bool(md.get('universe_filter_ref') or md.get('backtest_universe_cap'))


def evaluate(rows, manifest_strategies, since, *, max_lagging=3, min_median_dsharpe=-0.10,
             min_positive_frac=0.90, min_pairs=20):
    """rows: iterable of (strategy_id, bound_source|None, filter_ref_tier|None, total_sharpe|None,
    run_at, window_kind, primary_window), any order. Returns (ok: bool, detail_lines: list[str])."""
    live = sorted(k for k, v in manifest_strategies.items() if v.get('state') == 'live')
    liveset = set(live)
    by_sid = {}
    for r in rows:
        if r[0] in liveset:
            by_sid.setdefault(r[0], []).append(r)
    for v in by_sid.values():
        v.sort(key=lambda r: _aware(r[4]), reverse=True)
    out, lagging, none_ok, post = [], [], [], {}
    for sid in live:
        rs = by_sid.get(sid, [])
        prim = next((r for r in rs if r[6]), None)
        if prim is None:
            lagging.append((sid, 'no primary row')); continue
        src = prim[1]
        if _aware(prim[4]) < since:
            lagging.append((sid, 'stale (pre-epoch)')); continue
        if src is None:
            lagging.append((sid, 'row lacks universe_bound_source')); continue
        post[sid] = prim
        if src in OK_SOURCES:
            continue
        if src == 'none' and not has_ref(manifest_strategies[sid]):
            none_ok.append(sid); continue
        lagging.append((sid, f"source={src}" + (' (ref failed open)' if src == 'none' else '')))
    g1 = len(lagging) <= max_lagging
    out.append(f"  G1 fleet: live={len(live)} bound(cap/filter_ref)={len(post) - len(none_ok)} "
               f"static_by_design={len(none_ok)} lagging={len(lagging)} (max {max_lagging}) -> {'OK' if g1 else 'NOT_YET'}")
    if none_ok:
        out.append('    static_by_design (no filter ref): ' + ', '.join(none_ok[:12]) + (' …' if len(none_ok) > 12 else ''))
    if lagging:
        out.append('    lagging: ' + ', '.join(f'{s} [{why}]' for s, why in lagging[:12]) + (' …' if len(lagging) > 12 else ''))
    # G2
    pairs = []
    for sid, prim in post.items():
        base = next((r for r in by_sid[sid] if _aware(r[4]) < since and r[5] == prim[5]), None)
        if base is not None and base[3] is not None and prim[3] is not None:
            pairs.append((sid, float(prim[3]), float(base[3])))
    if len(pairs) < min_pairs:
        g2 = False
        out.append(f"  G2 sharpe: only {len(pairs)} live strategies have both a post-epoch and a pre-epoch row (need >= {min_pairs}) -> NOT_YET")
    else:
        deltas = [x - y for _, x, y in pairs]
        med = statistics.median(deltas)
        pos_a = sum(1 for _, x, _y in pairs if x > 0)
        pos_b = sum(1 for _, _x, y in pairs if y > 0)
        g2 = med >= min_median_dsharpe and pos_a >= min_positive_frac * pos_b
        out.append(f"  G2 sharpe: n={len(pairs)} median_dSharpe={med:+.3f} (min {min_median_dsharpe:+.2f}) "
                   f"positive after={pos_a} before={pos_b} (need >= {min_positive_frac:.2f}x) -> {'OK' if g2 else 'NOT_YET'}")
        out.append('    worst: ' + ', '.join(f'{s} {x - y:+.2f}' for s, x, y in sorted(pairs, key=lambda t: t[1] - t[2])[:5]))
        out.append('    best:  ' + ', '.join(f'{s} {x - y:+.2f}' for s, x, y in sorted(pairs, key=lambda t: t[2] - t[1])[:5]))
    return (g1 and g2), out


def read_uri(env_file):
    uri = os.environ.get('POSTGRES_URI')
    if not uri:
        try:
            m = re.search(r'^POSTGRES_URI=(.*)$', open(env_file).read(), re.M)  # parsed, never sourced
        except OSError:
            return None
        uri = m.group(1).strip().strip('"') if m else None
    return uri


def fetch_rows(uri, live):
    import psycopg2
    with psycopg2.connect(uri) as c:
        c.set_session(readonly=True)
        cur = c.cursor()   # indexed: (strategy_id, run_at DESC)
        cur.execute("""SELECT strategy_id, config_json->>'universe_bound_source',
                              config_json->>'universe_filter_ref_tier', total_sharpe, run_at,
                              window_kind, primary_window
                         FROM strategy_backtest_runs
                        WHERE strategy_id = ANY(%s)
                        ORDER BY strategy_id, run_at DESC""", (list(live),))
        return cur.fetchall()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--max-lagging', type=int, default=3)
    ap.add_argument('--min-median-dsharpe', type=float, default=-0.10)
    ap.add_argument('--min-positive-frac', type=float, default=0.90)
    ap.add_argument('--min-pairs', type=int, default=20)
    ap.add_argument('--since', default=None)
    ap.add_argument('--env-file', default='/root/openclaw/.env')
    ap.add_argument('--manifest', default='/root/openclaw/src/strategies/manifest.json')
    ap.add_argument('--data-dir', default='/root/openclaw/data')
    a = ap.parse_args(argv)
    since = epoch_start(a.since, a.data_dir)
    if since is None:
        print('NOT_YET'); print('  epoch start unknown: no --since and no .refresh_backtests.done.pre-universe-parity-* checkpoint'); return 0
    uri = read_uri(a.env_file)
    if not uri:
        print('NOT_YET'); print('  POSTGRES_URI not found'); return 0
    man = json.load(open(a.manifest))['strategies']
    live = [k for k, v in man.items() if v.get('state') == 'live']
    ok, detail = evaluate(fetch_rows(uri, live), man, since, max_lagging=a.max_lagging,
                          min_median_dsharpe=a.min_median_dsharpe,
                          min_positive_frac=a.min_positive_frac, min_pairs=a.min_pairs)
    print('OK' if ok else 'NOT_YET')
    print(f'  epoch start {since:%Y-%m-%d} UTC')
    print('\n'.join(detail))
    return 0


if __name__ == '__main__':
    sys.exit(main())
