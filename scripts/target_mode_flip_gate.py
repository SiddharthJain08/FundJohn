#!/usr/bin/env python3
"""target_mode_flip_gate.py — read-only gate check for the OPENCLAW_TARGET_MODE=atr_r flip.

Prints 'OK' or 'NOT_YET' on the first line, then the per-gate detail.

  G1 fleet uniformity: every manifest state=live strategy's LATEST primary
     backtest row carries config_json.target_mode='atr_r' (at most --max-lagging
     exceptions, default 3 — a strategy whose backtest OOMs every night must not
     hold the flip hostage; exceptions are listed).
  G2 Sharpe sanity: for live strategies with BOTH an atr_r row and an earlier
     flat row, the median (atr_r - flat) total_sharpe must be >= --min-median-dsharpe
     (default -0.10) AND the count of strategies with Sharpe > 0 under atr_r must be
     >= --min-positive-frac (default 0.90) x the count under flat. A geometry change
     that makes the fleet materially worse must not go live automatically — it
     goes to the operator with the numbers.

Usage: python3 scripts/target_mode_flip_gate.py [--max-lagging N] [--min-median-dsharpe X]
                                                 [--min-positive-frac F] [--env-file PATH]
"""
import argparse, json, os, re, statistics, sys

import psycopg2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--max-lagging', type=int, default=3)
    ap.add_argument('--min-median-dsharpe', type=float, default=-0.10)
    ap.add_argument('--min-positive-frac', type=float, default=0.90)
    ap.add_argument('--env-file', default='/root/openclaw/.env')
    ap.add_argument('--manifest', default='/root/openclaw/src/strategies/manifest.json')
    a = ap.parse_args()
    uri = os.environ.get('POSTGRES_URI')
    if not uri:
        m = re.search(r'^POSTGRES_URI=(.*)$', open(a.env_file).read(), re.M)
        uri = m.group(1).strip().strip('"') if m else None
    if not uri:
        print('NOT_YET'); print('  POSTGRES_URI not found'); return 0
    man = json.load(open(a.manifest))['strategies']
    live = sorted(k for k, v in man.items() if v.get('state') == 'live')
    out, ok = [], True
    latest_atr, latest_flat = {}, {}
    # 2026-09-17: the fleet refresh DEMOTES a strategy's previous row to
    # primary_window=false when a new run lands, so every pre-epoch flat
    # baseline is non-primary and a `WHERE primary_window = true` filter left
    # G2 with 0 pairs forever (verified 09-17: 86 atr_r primary rows, 636 flat
    # rows all primary_window=false, same window_kind). The atr_r side is still
    # the PRIMARY row; the flat baseline is the latest non-atr_r row of the
    # SAME window_kind, primary or not.
    with psycopg2.connect(uri) as c, c.cursor() as cur:
        cur.execute("""SELECT strategy_id, COALESCE(config_json->>'target_mode', 'flat') AS mode,
                              total_sharpe, run_at, window_kind, primary_window
                         FROM strategy_backtest_runs
                        ORDER BY strategy_id, run_at DESC""")
        rows = [r for r in cur.fetchall() if r[0] in live]
    for sid, mode, sharpe, run_at, wk, primary in rows:
        if mode == 'atr_r' and primary:
            latest_atr.setdefault(sid, (sharpe, run_at, wk))
    for sid, mode, sharpe, run_at, wk, primary in rows:
        if mode == 'atr_r':
            continue
        atr = latest_atr.get(sid)
        if atr is not None and atr[2] != wk:
            continue  # a different window is not a comparable baseline
        latest_flat.setdefault(sid, (sharpe, run_at))
    # G1 — the LATEST row per live strategy must be atr_r
    lagging = [s for s in live
               if s not in latest_atr
               or (s in latest_flat and latest_flat[s][1] > latest_atr[s][1])]
    g1 = len(lagging) <= a.max_lagging
    ok &= g1
    out.append(f"  G1 fleet: live={len(live)} on_atr_r={len(live) - len(lagging)} "
               f"lagging={len(lagging)} (max {a.max_lagging}) -> {'OK' if g1 else 'NOT_YET'}")
    if lagging:
        out.append('    lagging: ' + ', '.join(lagging[:12]) + (' …' if len(lagging) > 12 else ''))
    # G2 — Sharpe sanity vs the previous (flat) row of the same strategy
    pairs = [(s, float(latest_atr[s][0]), float(latest_flat[s][0]))
             for s in live if s in latest_atr and s in latest_flat
             and latest_atr[s][0] is not None and latest_flat[s][0] is not None]
    if len(pairs) < 20:
        g2 = False
        out.append(f"  G2 sharpe: only {len(pairs)} live strategies have both an atr_r and a flat row (need >= 20) -> NOT_YET")
    else:
        deltas = [x - y for _, x, y in pairs]
        med = statistics.median(deltas)
        pos_atr = sum(1 for _, x, _y in pairs if x > 0)
        pos_flat = sum(1 for _, _x, y in pairs if y > 0)
        frac_ok = pos_atr >= a.min_positive_frac * pos_flat
        g2 = (med >= a.min_median_dsharpe) and frac_ok
        out.append(f"  G2 sharpe: n={len(pairs)} median_dSharpe={med:+.3f} (min {a.min_median_dsharpe:+.2f}) "
                   f"positive atr_r={pos_atr} flat={pos_flat} (need >= {a.min_positive_frac:.2f}x) -> {'OK' if g2 else 'NOT_YET'}")
        worst = sorted(pairs, key=lambda t: t[1] - t[2])[:5]
        best = sorted(pairs, key=lambda t: t[2] - t[1])[:5]
        out.append('    worst: ' + ', '.join(f'{s} {x - y:+.2f}' for s, x, y in worst))
        out.append('    best:  ' + ', '.join(f'{s} {x - y:+.2f}' for s, x, y in best))
    ok &= g2
    print('OK' if ok else 'NOT_YET')
    print('\n'.join(out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
