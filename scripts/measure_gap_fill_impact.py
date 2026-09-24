#!/usr/bin/env python3
"""measure_gap_fill_impact.py — read-only sizing of the A2 gap-fill change.

For every STORED primary backtest run it takes the trades whose exit_reason is
'stop', looks up the exit bar's OPEN in prices.parquet, and re-prices the ones
whose open was already beyond the signal stop. Prints, per strategy, the mean
pnl_pct delta over the gapped trades, the same mean spread over all that
strategy's stop exits, and the counts.

READ-ONLY: no INSERT, no UPDATE, no env flip, no file written.

Comparability (state this whenever you quote the number): the stored
strategy_backtest_trades.pnl_pct is NET of the adverse per-fill slippage that
simulate_trade applies to both legs, while this script re-prices at the RAW
level -> RAW open. What it reports is therefore the GROSS level-vs-open delta;
the true net delta differs by the (unchanged) slippage fraction applied to a
slightly different exit level, which is second order. It is a scoping estimate,
never a backtest result.

Memory (2-core / 8 GB, no swap): prices are read with a pyarrow ticker filter
and a three-column projection, never the whole master. Keep --limit modest;
the default 40 strategies keeps the resident set well under 1 GB.

Usage:
  python3 scripts/measure_gap_fill_impact.py
  python3 scripts/measure_gap_fill_impact.py --strategy S_beta_spy
  python3 scripts/measure_gap_fill_impact.py --limit 80 --min-trades 50
"""
from __future__ import annotations

import argparse
import math
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRICES_PARQUET = ROOT / 'data' / 'master' / 'prices.parquet'


def reprice_stop_exit(direction, entry_price, stop, bar_open):
    """Gross pnl_pct of a stop exit filled at `bar_open`, or None when the bar
    did not gap through `stop` (or an input is unusable).

    long : a gap is open <  stop  -> fill at open, pnl = (open - entry)/entry
    short: a gap is open >  stop  -> fill at open, pnl = (entry - open)/entry
    """
    try:
        entry_price = float(entry_price)
        stop = float(stop)
        bar_open = float(bar_open)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(entry_price) and math.isfinite(stop) and math.isfinite(bar_open)):
        return None
    if entry_price <= 0.0:
        return None
    d = str(direction or '').lower()
    if d == 'long':
        if bar_open >= stop:
            return None
        return (bar_open - entry_price) / entry_price
    if d == 'short':
        if bar_open <= stop:
            return None
        return (entry_price - bar_open) / entry_price
    return None


def _postgres_uri(env_file: str):
    uri = os.environ.get('POSTGRES_URI')
    if uri:
        return uri
    try:
        m = re.search(r'^POSTGRES_URI=(.*)$', open(env_file).read(), re.M)
    except OSError:
        return None
    return m.group(1).strip().strip('"') if m else None


def _fetch_stop_trades(uri, strategy, limit):
    import psycopg2
    sql = """
        SELECT t.strategy_id, t.ticker, t.direction, t.entry_price,
               t.exit_date, t.signal_stop, t.pnl_pct
          FROM strategy_backtest_trades t
          JOIN strategy_backtest_runs r ON r.run_id = t.run_id
         WHERE r.primary_window = TRUE
           AND t.exit_reason = 'stop'
           AND t.signal_stop IS NOT NULL
           AND t.entry_price IS NOT NULL
           AND t.pnl_pct IS NOT NULL
    """
    params = []
    if strategy:
        sql += ' AND t.strategy_id = %s'
        params.append(strategy)
    with psycopg2.connect(uri) as c, c.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    if not strategy and limit:
        keep = sorted({r[0] for r in rows})[:limit]
        keep_set = set(keep)
        rows = [r for r in rows if r[0] in keep_set]
    return rows


def _open_map(tickers):
    """{(ticker, 'YYYY-MM-DD'): open} for the requested tickers only."""
    import pyarrow.parquet as pq
    if not tickers:
        return {}
    tbl = pq.read_table(str(PRICES_PARQUET), columns=['ticker', 'date', 'open'],
                        read_dictionary=['ticker', 'date'],
                        filters=[('ticker', 'in', sorted(tickers))])
    df = tbl.to_pandas()
    del tbl
    # `date` is a STRING dictionary column on disk (that is why the dictionary
    # read works); the DB hands back datetime.date, so both sides key on ISO.
    df['ticker'] = df['ticker'].astype(str)
    df['date'] = df['date'].astype(str).str.slice(0, 10)
    return dict(zip(zip(df['ticker'], df['date']), df['open'].astype(float)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=40,
                    help='max distinct strategies to load (alphabetical); 0 = all')
    ap.add_argument('--strategy', default=None)
    ap.add_argument('--min-trades', type=int, default=20,
                    help='skip strategies with fewer stop exits than this')
    ap.add_argument('--env-file', default=str(ROOT / '.env'))
    a = ap.parse_args()

    uri = _postgres_uri(a.env_file)
    if not uri:
        print('POSTGRES_URI not found', file=sys.stderr)
        return 2
    rows = _fetch_stop_trades(uri, a.strategy, a.limit)
    if not rows:
        print('no stored stop exits matched')
        return 0
    om = _open_map({r[1] for r in rows})

    per: dict = {}
    unmatched = 0
    for sid, ticker, direction, entry_price, exit_date, stop, pnl in rows:
        slot = per.setdefault(sid, {'stops': 0, 'gapped': 0, 'deltas': []})
        slot['stops'] += 1
        key = (str(ticker), exit_date.isoformat() if hasattr(exit_date, 'isoformat') else str(exit_date)[:10])
        bar_open = om.get(key)
        if bar_open is None:
            unmatched += 1
            continue
        gapped = reprice_stop_exit(direction, entry_price, stop, bar_open)
        if gapped is None:
            continue
        slot['gapped'] += 1
        slot['deltas'].append(gapped - float(pnl))

    print(f'{"strategy":<46} {"stops":>6} {"gapped":>7} {"gap%":>6} '
          f'{"mean_d_gapped%":>15} {"mean_d_all%":>12}')
    ranked = sorted(per.items(), key=lambda kv: (sum(kv[1]['deltas']) / kv[1]['stops']) if kv[1]['stops'] else 0.0)
    for sid, s in ranked:
        if s['stops'] < a.min_trades:
            continue
        d_gapped = (sum(s['deltas']) / len(s['deltas']) * 100.0) if s['deltas'] else 0.0
        d_all = (sum(s['deltas']) / s['stops'] * 100.0) if s['stops'] else 0.0
        print(f'{sid:<46} {s["stops"]:>6} {s["gapped"]:>7} '
              f'{(100.0 * s["gapped"] / s["stops"]):>5.1f}% {d_gapped:>+15.3f} {d_all:>+12.3f}')
    tot_stops = sum(s['stops'] for s in per.values())
    tot_gapped = sum(s['gapped'] for s in per.values())
    tot_delta = sum(sum(s['deltas']) for s in per.values())
    print(f'\nTOTAL strategies={len(per)} stops={tot_stops} gapped={tot_gapped} '
          f'({(100.0 * tot_gapped / tot_stops if tot_stops else 0.0):.1f}%) '
          f'mean_delta_all={(100.0 * tot_delta / tot_stops if tot_stops else 0.0):+.3f}% '
          f'unmatched_bars={unmatched}')
    print('NOTE: gross level-vs-open delta; stored pnl_pct is net of adverse '
          'fills, so this is a scoping estimate, not a backtest result.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
