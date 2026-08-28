#!/usr/bin/env python3
"""exit_hook_live_replay.py — exercise engine.update_pnl's exit-hook branch on
REAL data with zero side effects (Phase 2 spec §4.2).

For each --dates entry d: take the backtest run's trades open on d, recover
their entry-time signal_params by re-running the strategy's generate_signals
on the live prices panel truncated to each trade's entry date (backtests are
deterministic — 2026-08-07 ruling), build execution_signals-shaped rows, call
update_pnl with a fake cursor + the real panel truncated to d, and compare the
closes the live branch would issue against the backtest's recorded exits.
Read-only: no DB writes, no broker calls. Run outside 13:00–20:15 UTC.

Per-trade identity (fix round 1, 2026-08-28): comparisons are keyed by the
backtest's own trade_seq, not by ticker. Two concurrently-open trades on the
same ticker (different pairs entered the same day) each get matched to their
OWN recovered Signal by consuming signals_by_entry[(entry_date, ticker)] — a
list, in generate_signals' return order — in trade_seq order, skipping any
signal whose direction doesn't match the trade it's being considered for.
Ticker-keyed dicts previously collapsed such trades into a single comparison.

Caveats:
- The replay uses today's universe/prices panel, not the point-in-time panel
  the backtest used; a fixed LOW_VOL regime; and no aux_data. X1's hook reads
  none of these, so for X1 the comparison is exact — for other strategies the
  agreement number is indicative only.
- engine.load_prices() fetches a LIVE close-proxy row over the network when
  OPENCLAW_CLOSE_PROXY_SNAPSHOT=1 (the production .env value). This replay is
  read-only and must not do that, so main() force-sets
  OPENCLAW_CLOSE_PROXY_SNAPSHOT=0 right after loading .env, alongside
  OPENCLAW_EXIT_HOOK_LIVE=1.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))


def open_trades_on(trades, d):
    return [t for t in trades if t['entry_date'] < d <= t['exit_date']]


def rows_from_trades(open_trades, signals_by_entry):
    """signals_by_entry: dict[(entry_date, ticker)] -> list[Signal], in the
    order generate_signals returned them. Trades are processed in trade_seq
    order (the backtest appends trades in signal order, so the k-th
    same-ticker signal IS the k-th same-ticker trade); for each trade we pop
    the next unconsumed signal for its (entry_date, ticker), skipping any
    whose direction disagrees and continuing to pop. Unrecoverable (queue
    exhausted, or nullable stop/target missing) -> skipped, not fabricated."""
    queues = {k: list(v) for k, v in (signals_by_entry or {}).items()}
    rows = []
    for t in sorted(open_trades, key=lambda x: x['trade_seq']):
        if t.get('signal_stop') is None or t.get('signal_target') is None:
            print(f'[replay] trade_seq={t.get("trade_seq")} {t["ticker"]}: '
                  f'missing signal_stop/signal_target, skipping', file=sys.stderr)
            continue
        key = (t['entry_date'], t['ticker'])
        q = queues.get(key, [])
        sig = None
        while q:
            cand = q.pop(0)
            if str(cand.direction).upper() == str(t['direction']).upper():
                sig = cand
                break
        if sig is None:
            continue
        rows.append({'id': f'replay-{t["trade_seq"]}', 'strategy_id': None, 'ticker': t['ticker'],
                     'direction': str(t['direction']).upper(), 'entry_price': float(t['entry_price']),
                     'mark_entry_price': float(t['entry_price']), 'target_date': t['entry_date'],
                     'lifecycle_state': 'FILLED', 'stop_loss': float(t['signal_stop']),
                     'target_1': float(t['signal_target']), 'signal_date': t['entry_date'],
                     'signal_params': dict(sig.signal_params or {}), 'trade_seq': t['trade_seq']})
    return rows


def compare(live_closes, bt_closes):
    agree = sum(1 for k, v in bt_closes.items() if live_closes.get(k) == v)
    disagree = len(set(bt_closes) | set(live_closes)) - agree
    return agree, disagree


class _FakeCursor:
    def __init__(self, rows): self.rows = rows; self._fetch = []; self.executed = []
    def execute(self, sql, params=None):
        self.executed.append((sql, params)); self._fetch = list(self.rows) if "status = 'open'" in sql else []
    def fetchall(self): return self._fetch


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--strategy', required=True); ap.add_argument('--run-id', required=True)
    ap.add_argument('--dates', required=True, help='comma-separated YYYY-MM-DD')
    args = ap.parse_args()
    from dotenv import load_dotenv; load_dotenv(str(ROOT / '.env'))
    # Controller ruling: this replay is read-only and must never trigger the
    # live close-proxy network fetch inside engine.load_prices() — force it
    # off regardless of the production .env value.
    os.environ['OPENCLAW_CLOSE_PROXY_SNAPSHOT'] = '0'
    os.environ['OPENCLAW_EXIT_HOOK_LIVE'] = '1'
    import psycopg2, psycopg2.extras, pandas as pd
    from execution import engine
    from strategies.registry import load_strategy_class
    from strategies.base import CANONICAL_REGIMES

    dates = [datetime.strptime(s.strip(), '%Y-%m-%d').date() for s in args.dates.split(',') if s.strip()]
    with psycopg2.connect(os.environ['POSTGRES_URI']) as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""SELECT trade_seq, ticker, direction, entry_date, exit_date, entry_price, exit_reason,
                              signal_stop, signal_target FROM strategy_backtest_trades
                       WHERE run_id=%s ORDER BY trade_seq""", (args.run_id,))
        trades = [dict(r) for r in cur.fetchall()]
    cls = load_strategy_class(args.strategy); inst = cls(); inst.active_in_regimes = list(CANONICAL_REGIMES)
    for r in trades: r['strategy_id'] = args.strategy
    panel = engine.load_prices(sorted({t['ticker'] for t in trades}))       # real panel, all needed tickers
    universe = list(panel.columns)
    sig_cache: dict = {}
    computed_entry_dates: set = set()
    total_agree = total_bt = 0
    for d in dates:
        opens = open_trades_on(trades, d)
        for t in opens:
            ed = t['entry_date']
            if ed in computed_entry_dates: continue
            computed_entry_dates.add(ed)
            sub = panel.loc[:pd.Timestamp(ed)]
            try:
                sigs = inst.generate_signals(sub, {'state': 'LOW_VOL'}, universe)
            except Exception as e:
                print(f'[replay] generate_signals failed on {ed}: {e}', file=sys.stderr); sigs = []
            for s in sigs:
                sig_cache.setdefault((ed, s.ticker), []).append(s)
        rows = rows_from_trades(opens, sig_cache)
        for r in rows: r['strategy_id'] = args.strategy
        id_to_seq = {r['id']: r['trade_seq'] for r in rows}
        cur = _FakeCursor(rows)
        engine.update_pnl(cur, panel.loc[:pd.Timestamp(d)], d, strategies=[inst], regime={'state': 'LOW_VOL'})
        live = {}
        for sql, p in cur.executed:
            if 'INSERT INTO signal_pnl' in sql and p[7] == 'closed':
                live[id_to_seq[p[0]]] = p[10]
        bt = {t['trade_seq']: t['exit_reason'] for t in opens if t['exit_date'] == d
              and str(t['exit_reason']).startswith(('strategy_exit:', 'max_hold'))}
        agree, disagree = compare(live, bt)
        total_agree += agree; total_bt += len(bt)
        hook_rows_closed = (engine.LAST_EXIT_HOOK_STATS.get('strategy_exit', 0)
                            + engine.LAST_EXIT_HOOK_STATS.get('max_hold', 0))
        print(f'{d} open={len(opens)} rows={len(rows)} live_closes={len(live)} backtest_closes={len(bt)} '
              f'agree={agree} disagree={disagree} hook_rows_closed={hook_rows_closed} '
              f'stats={engine.LAST_EXIT_HOOK_STATS}')
    print(f'AGREEMENT {total_agree}/{total_bt}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
