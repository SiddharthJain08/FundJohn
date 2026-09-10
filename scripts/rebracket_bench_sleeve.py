#!/usr/bin/env python3
"""rebracket_bench_sleeve.py — re-place the GTC OCO on benchmark-sleeve positions
(S_beta_spy -> SPY) at the SLEEVE's own bracket levels.

Why (2026-09-10): the sizer used to hand SPY whichever alpha strategy's 2xATR
bracket won the weight pick (~1 % stop on SPY), and the whole beta sleeve was
ejected pre-market on a routine dip. The sizer fix (OPENCLAW_BENCH_SLEEVE_BRACKET,
regime_blended_sizer._choose_bracket) applies from the NEXT sizing wave; a
position bought under the old bracket still carries the ~1 % OCO tonight. This
script cancels that OCO and places the sleeve's levels off the position's avg
entry — stop -40 %, target +400 % clamped to +50 % exactly like the executor's
re-anchor (alpaca_executor._recompute_bracket_from_quote) so the levels match
what the fixed sizer path will place from tomorrow.

Idempotent: a position whose resting stop is already at/beyond the sleeve stop
is skipped. Only tickers with a benchmark-sleeve signal in the position's
direction are touched. Default DRY-RUN; --apply places orders.

Usage:
  python3 scripts/rebracket_bench_sleeve.py            # dry-run, whole book
  python3 scripts/rebracket_bench_sleeve.py --apply --symbols SPY
"""
from __future__ import annotations
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.env'))

import psycopg2  # noqa: E402
from execution import stop_reattach as sr  # noqa: E402
from execution.benchmark_sleeve import load_benchmark_sleeve_ids  # noqa: E402

# Mirror alpaca_executor._recompute_bracket_from_quote's outer clamps.
STOP_CLAMP = 0.50
TARGET_CLAMP = 0.50


def sleeve_pcts(conn, ticker: str, side: str, bench_ids: set[str]):
    """(stop_pct, target_pct, strategy_id) from the latest execution_signals row
    of a sleeve strategy for this ticker in the position's direction, or None."""
    want_dir = 'LONG' if side == 'long' else 'SHORT'
    cur = conn.cursor()
    cur.execute(
        """SELECT entry_price, stop_loss, target_1, strategy_id
             FROM execution_signals
            WHERE ticker = %s AND strategy_id = ANY(%s) AND upper(direction) = %s
              AND entry_price > 0 AND stop_loss > 0 AND target_1 > 0
            ORDER BY signal_date DESC, created_at DESC LIMIT 1""",
        (ticker, sorted(bench_ids), want_dir))
    row = cur.fetchone()
    if not row:
        return None
    e, s, t, sid = float(row[0]), float(row[1]), float(row[2]), row[3]
    if side == 'long':
        sp, tp = (e - s) / e, (t - e) / e
    else:
        sp, tp = (s - e) / e, (e - t) / e
    if sp <= 0 or tp <= 0:
        return None
    return min(sp, STOP_CLAMP), min(tp, TARGET_CLAMP), sid


def resting_levels(ticker: str, exit_side: str):
    """(stop, target) of the resting exit-side orders on ticker; None when absent."""
    ok, payload, _ = sr._run_cli(['order', 'list', '--status', 'open', '--symbols', ticker,
                                  '--nested', '--limit', '500'], timeout=30)
    if not ok:
        return None, None
    stop = tgt = None

    def walk(o):
        nonlocal stop, tgt
        if (o.get('side') or '').lower() == exit_side:
            if o.get('type') == 'stop' and o.get('stop_price'):
                stop = float(o['stop_price'])
            elif o.get('type') == 'limit' and o.get('limit_price'):
                tgt = float(o['limit_price'])
        for leg in o.get('legs') or []:
            walk(leg)

    for o in payload or []:
        walk(o)
    return stop, tgt


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--apply', action='store_true', help='place orders (default: dry-run)')
    ap.add_argument('--symbols', default=None, help='comma-separated subset (default: whole book)')
    a = ap.parse_args()
    dry = not a.apply
    want = {s.strip().upper() for s in a.symbols.split(',')} if a.symbols else None

    bench_ids = load_benchmark_sleeve_ids()
    if not bench_ids:
        print('no benchmark sleeve strategies registered — nothing to do')
        return 0
    positions = sr.fetch_positions()
    if positions is None:
        print('broker positions unavailable — aborting (no cancels on unknown state)')
        return 1
    conn = psycopg2.connect(os.environ['POSTGRES_URI'])
    rc = 0
    touched = 0
    for p in positions:
        sym = p.get('symbol')
        side = (p.get('side') or '').lower()
        if not sym or side not in ('long', 'short') or (want and sym not in want):
            continue
        try:
            qty = abs(float(p.get('qty') or 0))
            avg = float(p.get('avg_entry_price') or 0)
            cur = float(p.get('current_price') or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0 or avg <= 0:
            continue
        r = sleeve_pcts(conn, sym, side, bench_ids)
        if not r:
            continue                      # not a benchmark-sleeve ticker
        sp, tp, sid = r
        if side == 'long':
            new_stop, new_tgt = round(avg * (1 - sp), 2), round(avg * (1 + tp), 2)
        else:
            new_stop, new_tgt = round(avg * (1 + sp), 2), round(avg * (1 - tp), 2)
        straddles = (cur <= 0) or (
            (side == 'long' and new_stop < cur < new_tgt) or
            (side == 'short' and new_tgt < cur < new_stop))
        if not straddles:
            print(f'{sym}: sleeve levels {new_stop}/{new_tgt} do not straddle current {cur} — skip')
            continue
        exit_side = 'sell' if side == 'long' else 'buy'
        rs, rt = resting_levels(sym, exit_side)
        already = rs is not None and ((side == 'long' and rs <= new_stop * 1.001) or
                                      (side == 'short' and rs >= new_stop * 0.999))
        if already:
            print(f'{sym}: resting stop {rs} already at/beyond sleeve stop {new_stop} — skip')
            continue
        print(f'{sym} {side} x{qty:g} avg={avg:.2f} cur={cur:.2f}: resting stop={rs} target={rt} '
              f'-> sleeve [{sid}] stop={new_stop} ({-sp*100:+.1f}%) target={new_tgt} ({tp*100:+.1f}%)')
        if dry:
            print('  DRY-RUN — pass --apply to place')
            continue
        touched += 1
        n = sr.cancel_stops_for(sym, False, include_reserving=True, exit_side=exit_side)
        if n and not sr._wait_qty_freed(sym, qty):
            if not sr._position_still_open(sym):
                print(f'{sym}: position closed mid-pass — nothing to protect')
                continue
            print(f'{sym}: shares not freed after cancel — restoring bare stop at sleeve level')
            sr.submit_protective_stop(ticker=sym, position_side=side, qty=qty,
                                      stop_price=new_stop, dry_run=False)
            rc = 1
            continue
        res = sr.submit_protective_oco(ticker=sym, position_side=side, qty=qty,
                                       stop_price=new_stop, target_price=new_tgt, dry_run=False)
        if res.get('status') != 'submitted':
            print(f'{sym}: OCO rejected ({res.get("error", "?")}) — restoring bare stop at sleeve level')
            if sr._position_still_open(sym):
                sr.submit_protective_stop(ticker=sym, position_side=side, qty=qty,
                                          stop_price=new_stop, dry_run=False)
            rc = 1
        else:
            print(f'{sym}: OCO placed stop={new_stop} target={new_tgt} order={res.get("order_id", "?")}')
    print(f'done: {"dry-run" if dry else "applied"} touched={touched} rc={rc}')
    return rc


if __name__ == '__main__':
    sys.exit(main())
