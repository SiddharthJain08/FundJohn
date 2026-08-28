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
OWN recovered Signal by consuming signals_by_entry[(entry_date, ticker,
direction)] — a list, in generate_signals' return order — in trade_seq order.
Ticker-keyed dicts previously collapsed such trades into a single comparison.

Partner-leg recovery (fix round 3, 2026-08-28 final review): rounds 1-2 still
resolved ties by FIFO within an (entry_date, ticker, direction) queue. X1 can
open several pairs on the SAME ticker, direction and day (A/X and A/Y both go
LONG A), and a queue only holds as many signals as the strategy emitted — so
once the A/X trade had already CLOSED, it was absent from `open_trades` and
never consumed its signal, and FIFO handed A/X's signal to the surviving A/Y
trade. That trade then ran the hook with the wrong pair's beta/alpha/z and
exited early: exactly the three "unexplained" divergences (trade_seq 1073,
1082, 1086 — whose siblings 1046, 1065, 1078 closed the day before with the
same reason). Recovery is now, in order: (1) the signal whose
`signal_params['pair']` is exactly {this ticker, the PARTNER LEG's ticker} —
X1 appends a pair's two legs as consecutive `trade_seq` with the same
`entry_date` and opposite direction, so the partner is looked up at
`trade_seq +/- 1` in the FULL trade list (closed siblings included, which is
why `main()` passes every trade, not just those open on `d`); (2) an exact
(entry_price, stop, target) fingerprint match; (3) FIFO, as before. A signal
is consumed by at most one trade per call.

Direction-partitioned recovery (fix round 2, 2026-08-28): round 1 used a
single mixed-direction queue per (entry_date, ticker) and discarded any
popped signal whose direction didn't match the trade under consideration —
a discarded signal could never be given back to a later, opposite-direction
trade that actually needed it (repro: trades LONG then SHORT, same ticker/
entry date, signal queue order [SHORT, LONG] silently dropped the SHORT
trade). Recovery queues are now keyed by (entry_date, ticker,
direction.upper()) end to end — both in main()'s sig_cache and in
rows_from_trades — so a trade only ever draws from its own direction's
queue and no cross-direction discarding can occur.

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


def _pair_tickers(sig):
    """The set of tickers named by a recovered Signal's `signal_params['pair']`
    ('AAA/BBB' -> {'AAA','BBB'}). None when the field is absent or not a
    string. Split-and-compare-as-a-set, never substring matching: 'WES' is a
    substring of plenty of other tickers."""
    pair = (getattr(sig, 'signal_params', None) or {}).get('pair')
    if not isinstance(pair, str):
        return None
    parts = {p.strip() for p in pair.split('/') if p.strip()}
    return parts or None


def _partner_tickers(trade, by_seq):
    """Tickers of this trade's possible PARTNER LEGS. X1 appends the two legs
    of a pair as consecutive `trade_seq` sharing an `entry_date` with opposite
    directions, so the partner is at `trade_seq +/- 1`. Both neighbours are
    returned when both qualify (the pair boundary is not knowable from seq
    alone); the pair-set match below then picks the one that actually has a
    signal."""
    seq, ed = trade.get('trade_seq'), trade.get('entry_date')
    if seq is None:
        return []
    direction = str(trade['direction']).upper()
    out = []
    for neighbour in (seq - 1, seq + 1):
        cand = by_seq.get((ed, neighbour))
        if cand is None or str(cand['direction']).upper() == direction:
            continue
        if cand['ticker'] not in out:
            out.append(cand['ticker'])
    return out


def _fingerprint(entry_price, stop, target):
    try:
        return (round(float(entry_price), 4), round(float(stop), 4), round(float(target), 4))
    except (TypeError, ValueError):
        return None


def _recover_signal(trade, queue, by_seq):
    """Pick THIS trade's Signal out of its (entry_date, ticker, direction)
    queue. Order: partner-leg pair match -> (entry, stop, target) fingerprint
    -> FIFO. Returns None only when the queue is empty."""
    if not queue:
        return None
    wanted = [{trade['ticker'], partner} for partner in _partner_tickers(trade, by_seq)]
    if wanted:
        for sig in queue:
            tickers = _pair_tickers(sig)
            if tickers is not None and tickers in wanted:
                return sig
    fp = _fingerprint(trade.get('entry_price'), trade.get('signal_stop'), trade.get('signal_target'))
    if fp is not None:
        for sig in queue:
            if _fingerprint(getattr(sig, 'entry_price', None), getattr(sig, 'stop_loss', None),
                            getattr(sig, 'target_1', None)) == fp:
                return sig
    return queue[0]


def rows_from_trades(open_trades, signals_by_entry, all_trades=None):
    """signals_by_entry: dict[(entry_date, ticker, direction.upper())] ->
    list[Signal], in the order generate_signals returned them, PARTITIONED
    BY DIRECTION (round 2 fix) so a trade only ever consumes signals of its
    own direction — no cross-direction discarding is possible or needed.

    `all_trades` is the FULL trade list for the run (round 3 fix); the partner
    leg of a still-open trade is frequently a trade that has already CLOSED,
    so it is absent from `open_trades` and must be found here. Defaults to
    `open_trades` for callers that have nothing wider.

    Each trade draws from its own (entry_date, ticker, direction) queue via
    `_recover_signal` (partner-leg pair -> fingerprint -> FIFO); the chosen
    signal is removed, so no signal is ever handed to two trades.
    Unrecoverable (queue exhausted/absent, or nullable stop/target missing)
    -> skipped, not fabricated."""
    queues = {k: list(v) for k, v in (signals_by_entry or {}).items()}
    pool = list(all_trades) if all_trades else list(open_trades)
    by_seq = {(t.get('entry_date'), t.get('trade_seq')): t for t in pool}
    rows = []
    for t in sorted(open_trades, key=lambda x: x['trade_seq']):
        if t.get('signal_stop') is None or t.get('signal_target') is None:
            print(f'[replay] trade_seq={t.get("trade_seq")} {t["ticker"]}: '
                  f'missing signal_stop/signal_target, skipping', file=sys.stderr)
            continue
        direction = str(t['direction']).upper()
        key = (t['entry_date'], t['ticker'], direction)
        q = queues.get(key)
        sig = _recover_signal(t, q, by_seq) if q else None
        if sig is None:
            continue
        # remove BY IDENTITY: Signal is a dataclass, so list.remove() would
        # drop the first equal object, not necessarily the one we picked.
        for _i, _s in enumerate(q):
            if _s is sig:
                del q[_i]
                break
        rows.append({'id': f'replay-{t["trade_seq"]}', 'strategy_id': None, 'ticker': t['ticker'],
                     'direction': direction, 'entry_price': float(t['entry_price']),
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
                sig_cache.setdefault((ed, s.ticker, str(s.direction).upper()), []).append(s)
        # round 3: the partner pool is EVERY trade of the run, not just those
        # open on d — a still-open leg's partner has often already closed.
        rows = rows_from_trades(opens, sig_cache, all_trades=trades)
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
