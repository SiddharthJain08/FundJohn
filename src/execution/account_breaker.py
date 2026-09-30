#!/usr/bin/env python3
"""Account-level risk breaker (spec 2026-09-12 §3 C1, operator ruling R2).

Two rules, evaluated every 5 minutes during RTH by the SAME cron that runs
position_circuit_breaker.py (src/engine/cron-schedule.js) — no new thread:

    drawdown    alpha_nav / rolling_peak(alpha_nav) - 1   <= -0.10
    daily loss  (equity - opening_equity) / opening_equity <= -0.03

alpha_nav = broker equity - the market value of every BENCHMARK-sleeve ticker,
so an SPY-sleeve drawdown never trips the alpha drawdown rule; the daily rule
deliberately measures the WHOLE book. The asymmetry is ruling R2 as written —
do not harmonize the two.

ALL FOUR REGIMES. Nothing in this module branches on the trading regime.

Flag: OPENCLAW_ACCOUNT_BREAKER=1 arms the ACTION. Unset = SHADOW — the state
row and the `[account_breaker] shadow ...` line are still written every tick,
no order is submitted, and every circuit_breaker_fires row carries
close_result_json.dry_run=true so the sizer's risk-exit cooldown
(_load_recent_risk_exits) ignores it.

Re-arm is operator-only: OPENCLAW_ACCOUNT_BREAKER_REARM=<breached_at iso> in
.env clears exactly that halt (a stale token cannot clear a later breach) and
resets the rolling peak to the current alpha NAV.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg2

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(ROOT / 'src'))

logger = logging.getLogger(__name__)

ARM_ENV = 'OPENCLAW_ACCOUNT_BREAKER'
REARM_ENV = 'OPENCLAW_ACCOUNT_BREAKER_REARM'
NAV_OHLC_PATH_ENV = 'OPENCLAW_PNL_OHLC_PATH'
DEFAULT_NAV_OHLC_PATH = ROOT / 'logs' / 'pnl_daily_ohlc.json'

DD_LIMIT = -0.10        # alpha-sleeve drawdown from the rolling peak
DAILY_LIMIT = -0.03     # total-equity loss vs the session's opening equity
BENCH_LOOKBACK_DAYS = 30

_ET = ZoneInfo('America/New_York')

# F-5 (deferred from Task 5's fix round 1, folded into Task 7 by the brief
# supplement items 8/11): after this many CONSECUTIVE 5-minute ticks with a
# still-pending flatten, run_once() posts ONE escalation to #trade-reports
# (not a repeat every tick after) — see flatten_attempts (migration 158).
# Overridable via OPENCLAW_ACCOUNT_BREAKER_FLATTEN_ESCALATE_AFTER (fix round
# 1 minor item 2 — migration 158's comment named this env var before any
# code actually read it; read once at import, same posture as the other
# module-level limits above, with a robust fallback to the 12-tick default
# on anything unparsable).
FLATTEN_ESCALATE_AFTER_ENV = 'OPENCLAW_ACCOUNT_BREAKER_FLATTEN_ESCALATE_AFTER'
try:
    FLATTEN_ESCALATE_AFTER = int(os.environ.get(FLATTEN_ESCALATE_AFTER_ENV) or 12)
except (TypeError, ValueError):
    logger.warning('[account_breaker] %s=%r is not an int; using default 12',
                   FLATTEN_ESCALATE_AFTER_ENV, os.environ.get(FLATTEN_ESCALATE_AFTER_ENV))
    FLATTEN_ESCALATE_AFTER = 12    # ~1 hour at the 5-minute RTH cron cadence

# Float-equality tolerance for the boundary comparisons below: a ratio that is
# mathematically exactly -0.10 (e.g. 90_000 / 100_000 - 1) lands on
# -0.09999999999999998 in IEEE-754 double precision, which is NOT <= -0.10
# and would silently fail to trip the rule at its own documented threshold.
# This epsilon is far smaller than any real gap between "at the limit" and
# "inside the limit" (the latter differs by >= tens of dollars of NAV in
# practice), so it never masks a genuine non-breach.
_EPS = 1e-9


def armed() -> bool:
    """True iff the operator has armed the ACTION. Read at call time so a
    .env edit takes effect on the next 5-minute tick without a restart."""
    return os.environ.get(ARM_ENV) == '1'


def nav_ohlc_path() -> Path:
    return Path(os.environ.get(NAV_OHLC_PATH_ENV) or DEFAULT_NAV_OHLC_PATH)


def alpha_nav(equity: float, positions: dict, bench_tickers) -> tuple[float, float]:
    """(alpha_nav, benchmark_market_value).

    `positions` is regime_liquidator._load_broker_positions() shape:
    {symbol: {'qty': float, 'side': str, 'market_value': str}}. A short
    benchmark leg has a negative market value and correctly RAISES alpha NAV.
    Unparseable market values are skipped (logged by the caller's line).

    Membership is compared case-insensitively on both sides: a casing
    mismatch between the caller's bench_tickers set and the broker's symbol
    casing must never silently degrade the 10% alpha-sleeve drawdown rule
    into a total-NAV rule."""
    bench_tickers = {str(t).strip().upper() for t in (bench_tickers or ())}
    bench_mv = 0.0
    for sym, p in (positions or {}).items():
        if str(sym).strip().upper() not in bench_tickers:
            continue
        try:
            bench_mv += float((p or {}).get('market_value') or 0.0)
        except (TypeError, ValueError):
            continue
    return float(equity) - bench_mv, bench_mv


def evaluate(alpha: float, peak, equity: float, opening_equity,
             dd_override=None, skip_drawdown: bool = False) -> dict:
    """Pure rule evaluation. Returns
    {'peak', 'dd', 'daily', 'rule', 'breach'}.

    `peak` None (first ever tick) seeds from the current alpha NAV, so the
    breaker can never fire on its own first observation. `opening_equity`
    None/0 disables the daily rule for that tick (reported as daily=None)
    rather than inventing a denominator."""
    alpha = float(alpha)
    peak = alpha if peak is None else max(float(peak), alpha)
    dd_nav = (alpha / peak - 1.0) if peak > 0 else 0.0
    # C1 amendment 2: when the caller supplies the cumulative-alpha-P&L
    # drawdown, THAT drives the drawdown rule; the NAV-based measure is kept
    # only as 'dd_nav' for the transition log line.
    dd = dd_nav if dd_override is None else float(dd_override)
    if skip_drawdown:
        # C1 amendment 2a (F3): armed tick with no alpha-P&L measure — the
        # legacy NAV drawdown is defective (sleeve rebalances), so the
        # drawdown rule is SKIPPED (never fed dd_nav); daily-loss still runs.
        dd = 0.0

    daily = None
    if opening_equity not in (None, 0) and float(opening_equity) > 0:
        daily = float(equity) / float(opening_equity) - 1.0

    rules = []
    if not skip_drawdown and dd <= DD_LIMIT + _EPS:
        rules.append('drawdown')
    if daily is not None and daily <= DAILY_LIMIT + _EPS:
        rules.append('daily_loss')

    return {'peak': peak, 'dd': dd, 'dd_nav': dd_nav, 'daily': daily,
            'rule': '+'.join(rules) if rules else 'none',
            'breach': bool(rules)}


# ── cumulative alpha P&L (C1 amendment 2) ───────────────────────────────────

def _f(x, default=None):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if v == v else default    # NaN -> default


def _pos_key(sym) -> str:
    """Position key shared by both legs: upper-case, '/' stripped, so a crypto
    fill symbol 'BTC/USD' matches the broker's position symbol 'BTCUSD'."""
    return str(sym or '').strip().upper().replace('/', '')


_BUY_SIDES = ('buy',)
_SELL_SIDES = ('sell', 'sell_short')
_QTY_EPS = 1e-9


def alpha_pnl(equity, positions, bench_tickers, fills_since_epoch, epoch_lots) -> dict:
    """Cumulative alpha P&L = realized (AVERAGE-COST ledger over fills since the
    epoch, seeded by the epoch lots — mirrors the broker, C1 amendment 2a) +
    unrealized (open non-benchmark equity positions).

    positions: {symbol: {'qty','side','avg_entry_price','current_price',
    'market_value','unrealized_pl'}} (regime_liquidator._load_broker_positions
    shape). fills_since_epoch: iterable of {ticker, side, qty, price, filled_at,
    activity_id} (sorted here by filled_at then activity_id). epoch_lots:
    iterable of {ticker, qty, avg_entry_price, side}.

    Average cost per ticker: a buy adds at the running average; a sell realizes
    q*(p-avg). A sell with no open long OPENS a short (avg = sell price); a
    long->short flip's remainder opens a short at the fill price; a buy against
    a short realizes q*(avg-p). `sell_short` == sell. An unknown side (or an
    unparseable qty/price) is counted in `unmatched`, never dropped silently;
    a negative qty with no side is a short sale.

    Scope (F4): benchmark tickers AND non-equity symbols (`_is_equity_symbol`:
    options, crypto pairs) are excluded from BOTH legs; the number of distinct
    excluded keys is returned as `excluded`. Symbols are matched on `_pos_key`
    so `BTC/USD` fills and `BTCUSD` positions land on the same (excluded) key.
    The unrealized leg prefers the broker's `unrealized_pl`, falling back to
    qty*(cur-avg). `recon` = tickers whose ledger qty != broker qty (including
    closed positions with a non-zero ledger qty). Fail-open: nothing raises.
    Returns {alpha_pnl, realized, unrealized, unmatched, unpriced, n_positions,
    excluded, recon}."""
    bench = {_pos_key(t) for t in (bench_tickers or ())}
    fills = list(fills_since_epoch or ())
    lots_in = list(epoch_lots or ())
    positions = positions or {}

    # A key is non-equity when ANY raw symbol mapping onto it fails the equity
    # predicate (crypto 'BTC/USD' fills poison the 'BTCUSD' position too).
    non_equity = set()
    for sym in list(positions) + [f.get('ticker') for f in fills] + \
            [l.get('ticker') for l in lots_in]:
        if sym is not None and not _is_equity_symbol(sym):
            non_equity.add(_pos_key(sym))
    excluded_keys = set()

    def _in_scope(sym) -> bool:
        k = _pos_key(sym)
        if not k:
            return False
        if k in bench:
            return False
        if k in non_equity:
            excluded_keys.add(k)
            return False
        return True

    unrealized, n_pos, unpriced = 0.0, 0, 0
    broker_qty = {}
    for sym, p in positions.items():
        if not _in_scope(sym):
            continue
        p = p or {}
        qty = _f(p.get('qty'), 0.0)
        if not qty:
            continue
        sign = -1.0 if str(p.get('side') or '').lower() == 'short' else 1.0
        broker_qty[_pos_key(sym)] = sign * abs(qty)
        n_pos += 1
        upl = _f(p.get('unrealized_pl'))
        avg, cur_px = _f(p.get('avg_entry_price')), _f(p.get('current_price'))
        if upl is not None:
            unrealized += upl
        elif avg is not None and cur_px is not None:
            unrealized += abs(qty) * (cur_px - avg) * sign
        else:
            unpriced += 1
            logger.warning('[account_breaker] unpriced position %s (no avg/current price)', sym)

    # average-cost state per key: [signed_qty, avg_price]
    book: dict = {}

    def _apply(k, dq, px) -> float:
        """Apply signed qty `dq` at `px`; returns realized P&L."""
        st = book.setdefault(k, [0.0, 0.0])
        q0, a0 = st
        realized = 0.0
        if abs(q0) <= _QTY_EPS or (q0 > 0) == (dq > 0):
            tot = abs(q0) + abs(dq)
            st[1] = (abs(q0) * a0 + abs(dq) * px) / tot if tot else 0.0
            st[0] = q0 + dq
            return 0.0
        closing = min(abs(dq), abs(q0))
        realized = closing * (px - a0) * (1.0 if q0 > 0 else -1.0)
        q1 = q0 + dq
        if abs(q1) <= _QTY_EPS:
            st[0], st[1] = 0.0, 0.0
        elif (q1 > 0) == (q0 > 0):
            st[0] = q1                       # partial close: avg unchanged
        else:
            st[0], st[1] = q1, px            # flipped: remainder opens at px
        return realized

    for l in lots_in:
        k = _pos_key(l.get('ticker'))
        q, px = _f(l.get('qty')), _f(l.get('avg_entry_price'))
        if not _in_scope(l.get('ticker')) or not q or px is None:
            continue
        signed = -abs(q) if str(l.get('side') or '').lower() == 'short' else abs(q)
        _apply(k, signed, px)

    def _key(f):
        return (str(f.get('filled_at') or ''), str(f.get('activity_id') or ''))

    realized, unmatched = 0.0, 0
    for f in sorted(fills, key=_key):
        if not _in_scope(f.get('ticker')):
            continue
        k = _pos_key(f.get('ticker'))
        q, px = _f(f.get('qty')), _f(f.get('price'))
        side = str(f.get('side') or '').strip().lower()
        if not q or px is None:
            unmatched += 1
            logger.warning('[account_breaker] unusable fill %s qty=%r price=%r '
                           '(counted, skipped)', k, f.get('qty'), f.get('price'))
            continue
        if side in _BUY_SIDES:
            dq = abs(q)
        elif side in _SELL_SIDES:
            dq = -abs(q)
        elif not side and q < 0:
            dq = q                          # negative qty, no side => short sale
        else:
            unmatched += 1
            logger.warning('[account_breaker] unknown side %r on fill %s qty=%s @%s '
                           '(counted, not applied)', f.get('side'), k, q, px)
            continue
        realized += _apply(k, dq, px)

    recon = 0
    for k in set(book) | set(broker_qty):
        lq = book.get(k, [0.0, 0.0])[0]
        bq = broker_qty.get(k, 0.0)
        if abs(lq - bq) > 1e-4 * max(1.0, abs(bq)):
            recon += 1
            logger.warning('[account_breaker] recon mismatch %s ledger_qty=%.6f '
                           'broker_qty=%.6f', k, lq, bq)

    return {'alpha_pnl': realized + unrealized, 'realized': realized,
            'unrealized': unrealized, 'unmatched': unmatched,
            'unpriced': unpriced, 'n_positions': n_pos,
            'excluded': len(excluded_keys), 'recon': recon}


# ── state layer (C1b) ────────────────────────────────────────────────────────
#
# Every function below shares a `cur` owned by the caller (run_once(), Task 7)
# rather than opening its own connection. Migration 157 may not have been
# applied yet wherever this module first runs (deploy and migration-apply are
# separate steps), so each DB path here is SAVEPOINT-isolated: a failed query
# (e.g. "relation account_breaker_state does not exist") must not poison the
# rest of the caller's transaction the way an unguarded failure would in
# Postgres — the same problem alpaca_reconcile.py solves with its
# `sp_broker_fills` savepoint. Reads fail open to a documented default and log
# a WARNING; writes fail open (swallow, log ERROR) since a lost write here
# must never abort the 5-minute cron tick, only be missed until next tick.
#
# Callers must supply a cursor inside a TRANSACTION BLOCK — autocommit
# disables every savepoint guard above (a SAVEPOINT outside a transaction is
# meaningless, and RELEASE/ROLLBACK TO SAVEPOINT lose their point), which
# would turn one failed query into a connection that poisons the rest of the
# caller's tick. run_once() checks `conn.autocommit` and refuses with
# return 2 before the first guarded call for exactly this reason (brief
# supplement item 4; fix round 1 item 3 replaced the original `assert` — an
# `assert` is stripped under `python -O`, and the brief's exit-code
# contract wants a normal control-flow `return 2`, not an uncaught
# AssertionError).

_STATE_COLS = ('halted', 'reason', 'breached_at', 'peak_alpha_nav', 'dd',
               'daily', 'pending_flatten')
_EMPTY_STATE = {'halted': False, 'reason': None, 'breached_at': None,
                'peak': None, 'dd': None, 'daily': None, 'pending_flatten': False}


def _savepoint_guarded(cur, name, thunk, *, on_error_level=logging.WARNING):
    """Run `thunk()` (a zero-arg callable issuing one or more cur.execute()
    calls) inside a named SAVEPOINT. Returns (ok, result). On failure, rolls
    back to the savepoint (so the caller's outer transaction survives) and
    logs at `on_error_level` — it never re-raises, because nothing in this
    module may abort the caller's cycle step."""
    try:
        cur.execute(f'SAVEPOINT {name}')
        result = thunk()
        cur.execute(f'RELEASE SAVEPOINT {name}')
        return True, result
    except Exception as e:  # noqa: BLE001 — a DB path here must fail open
        logger.log(on_error_level,
                   '[account_breaker] %s failed (%s: %s); rolling back to savepoint',
                   name, type(e).__name__, e)
        try:
            cur.execute(f'ROLLBACK TO SAVEPOINT {name}')
            cur.execute(f'RELEASE SAVEPOINT {name}')
        except Exception:  # noqa: BLE001 — cursor may already be unusable
            pass
        return False, None


def load_state(cur) -> dict:
    """The singleton latch. A missing row (pre-migration, or a DB that has
    never run the breaker) is a clean, un-halted default. A query failure
    (e.g. migration 157 not yet applied) fails open to the same default,
    logged as a WARNING rather than raised."""
    def _read():
        cur.execute(
            'SELECT halted, reason, breached_at, peak_alpha_nav, dd, daily, '
            'pending_flatten FROM account_breaker_state WHERE id = 1')
        return cur.fetchone()

    ok, row = _savepoint_guarded(cur, 'sp_ab_load_state', _read)
    if not ok or not row:
        return dict(_EMPTY_STATE)
    halted, reason, breached_at, peak, dd, daily, pending = row
    return {'halted': bool(halted), 'reason': reason, 'breached_at': breached_at,
            'peak': None if peak is None else float(peak),
            'dd': None if dd is None else float(dd),
            'daily': None if daily is None else float(daily),
            'pending_flatten': bool(pending)}


def save_state(cur, *, halted, reason, breached_at, peak, dd, daily,
               pending_flatten, flatten_attempts=None, peak_alpha_pnl=None) -> bool:
    """Persist the singleton latch. Returns True iff the write actually
    landed (supplement item 2) — every caller in run_once() must check this,
    because a failed write here (e.g. migration 157 not yet applied) is
    logged as an ERROR and swallowed rather than raised; the caller's cycle
    tick must still be able to complete and retry the write on the next
    tick, but the ONE call that gates a broker action (the pre-flatten latch
    in run_once()) must see the failure to refuse that action.

    `flatten_attempts` (migration 158, F-5) is None by default, meaning
    "leave the consecutive-pending-tick counter untouched" — COALESCE keeps
    whatever is already stored so existing callers that never pass it (every
    branch except the two that actually attempt a flatten) don't reset it."""
    def _write():
        cur.execute(
            """
            UPDATE account_breaker_state
               SET halted = %s, reason = %s, breached_at = %s, peak_alpha_nav = %s,
                   dd = %s, daily = %s, pending_flatten = %s,
                   flatten_attempts = COALESCE(%s, flatten_attempts), updated_at = NOW()
             WHERE id = 1
            """,
            (bool(halted), reason, breached_at, peak, dd, daily, bool(pending_flatten),
             flatten_attempts),
        )

    ok, _ = _savepoint_guarded(cur, 'sp_ab_save_state', _write, on_error_level=logging.ERROR)
    if ok and peak_alpha_pnl is not None:
        save_peak_alpha_pnl(cur, peak_alpha_pnl)
    return ok


def save_peak_alpha_pnl(cur, peak) -> bool:
    """Persist the alpha-P&L high-water mark (migration 160). Its own savepoint
    so a missing column never rolls back the latch write that precedes it."""
    def _write():
        cur.execute('UPDATE account_breaker_state SET peak_alpha_pnl = %s WHERE id = 1',
                    (float(peak),))
    ok, _ = _savepoint_guarded(cur, 'sp_ab_save_peak_pnl', _write,
                               on_error_level=logging.ERROR)
    return ok


def load_alpha_state(cur):
    """(peak_alpha_pnl | None, alpha_epoch_at | None), or None when the read
    failed / migration 160 is not applied / no state row (caller falls back to
    the legacy NAV drawdown for that tick). fetchall() only, so the caller's
    fetchone() sequencing is never disturbed."""
    def _read():
        cur.execute('SELECT peak_alpha_pnl, alpha_epoch_at FROM account_breaker_state '
                    'WHERE id = 1')
        return cur.fetchall()
    ok, rows = _savepoint_guarded(cur, 'sp_ab_load_alpha_state', _read)
    if not ok or not rows:
        return None
    peak, epoch_at = rows[0]
    return (None if peak is None else float(peak)), epoch_at


def take_alpha_epoch(cur, positions, bench_tickers) -> bool:
    """Snapshot the open non-benchmark lots and stamp alpha_epoch_at, in ONE
    savepoint (all-or-nothing). Called only when alpha_epoch_at IS NULL; the
    UPDATE is additionally guarded `AND alpha_epoch_at IS NULL` so the epoch can
    never move. Returns True iff it landed."""
    bench = {str(t).strip().upper() for t in (bench_tickers or ())}
    rows = []
    for sym, p in (positions or {}).items():
        if str(sym).strip().upper() in bench or not _is_equity_symbol(sym):
            continue                       # F4: same scope predicate as the flatten list
        p = p or {}
        qty, avg = _f(p.get('qty'), 0.0), _f(p.get('avg_entry_price'))
        if not qty or avg is None:
            continue
        side = 'short' if str(p.get('side') or '').lower() == 'short' else 'long'
        rows.append((str(sym).strip().upper(), abs(qty), avg, side))

    def _write():
        cur.execute('UPDATE account_breaker_state SET alpha_epoch_at = NOW() '
                    'WHERE id = 1 AND alpha_epoch_at IS NULL')
        if getattr(cur, 'rowcount', 1) == 0:
            raise RuntimeError('epoch already set or no state row')
        for r in rows:
            cur.execute('INSERT INTO account_breaker_alpha_epoch '
                        '(ticker, qty, avg_entry_price, side, taken_at) '
                        'VALUES (%s, %s, %s, %s, NOW())', r)

    ok, _ = _savepoint_guarded(cur, 'sp_ab_alpha_epoch', _write,
                               on_error_level=logging.ERROR)
    return ok


def load_alpha_inputs(cur, epoch_at):
    """(epoch_lots, fills_since_epoch) as lists of dicts, or None on failure."""
    def _read():
        cur.execute('SELECT ticker, qty, avg_entry_price, side '
                    'FROM account_breaker_alpha_epoch')
        lots = [{'ticker': r[0], 'qty': r[1], 'avg_entry_price': r[2], 'side': r[3]}
                for r in (cur.fetchall() or [])]
        cur.execute('SELECT ticker, side, qty, price, filled_at, activity_id '
                    'FROM broker_fills WHERE filled_at >= %s '
                    'ORDER BY filled_at, activity_id', (epoch_at,))
        fills = [{'ticker': r[0], 'side': r[1], 'qty': r[2], 'price': r[3],
                  'filled_at': r[4], 'activity_id': r[5]}
                 for r in (cur.fetchall() or [])]
        return lots, fills
    ok, res = _savepoint_guarded(cur, 'sp_ab_alpha_inputs', _read)
    return res if ok else None


def load_flatten_attempts(cur) -> int:
    """Consecutive-tick counter for a still-pending flatten (additive column
    `flatten_attempts`, migration 158 — deferred F-5, folded into Task 7 by
    the brief supplement). A SEPARATE SELECT from load_state's fixed
    7-column query — load_state's row shape is a contract several existing
    callers/tests are keyed to, so this reads the new column independently
    rather than widening that tuple. Fails open to 0: a lost read only
    delays the one-time escalation post by a tick, it never blocks the
    retry itself."""
    def _read():
        cur.execute('SELECT flatten_attempts FROM account_breaker_state WHERE id = 1')
        return cur.fetchone()

    ok, row = _savepoint_guarded(cur, 'sp_ab_load_attempts', _read)
    if not ok or not row or row[0] is None:
        return 0
    return int(row[0])


def opening_equity(cur, session_date, equity, path=None) -> tuple[float, str]:
    """(opening_equity, source) for `session_date`, snapshotting it on first use.

    Order: the stored account_daily_open row -> the session's candle `open` in
    logs/pnl_daily_ohlc.json -> the current equity. The OHLC `open` is the
    PRIOR session's close by construction (the sampler rolls candles at
    midnight ET so consecutive candles touch — server.js:2599-2626), which is
    exactly the opening equity for this rule, hence estimated=False. Only the
    current-equity fallback is marked estimated.

    Both the stored-row read and the snapshot write are savepoint-isolated:
    a missing account_daily_open table (migration 157 not yet applied) must
    not stop this function from returning a usable value, and must not
    poison the caller's transaction for whatever it does with `cur` next."""
    def _read_stored():
        cur.execute('SELECT opening_equity FROM account_daily_open '
                    'WHERE session_date = %s', (session_date,))
        return cur.fetchone()

    ok, row = _savepoint_guarded(cur, 'sp_ab_open_read', _read_stored)
    if ok and row and row[0] is not None:
        return float(row[0]), 'stored'

    value, source, estimated = None, None, True
    try:
        days = json.loads(Path(path or nav_ohlc_path()).read_text()).get('days') or {}
        day = days.get(session_date.isoformat())
        if isinstance(day, dict) and day.get('open') is not None:
            value, source, estimated = float(day['open']), 'ohlc', False
    except Exception as e:  # noqa: BLE001 — a missing/broken store is not fatal
        logger.warning('[account_breaker] pnl_daily_ohlc unreadable (%s: %s)',
                       type(e).__name__, e)
    if value is None:
        value, source, estimated = float(equity), 'equity', True

    def _write_snapshot():
        cur.execute(
            'INSERT INTO account_daily_open (session_date, opening_equity, estimated) '
            'VALUES (%s, %s, %s) ON CONFLICT (session_date) DO NOTHING',
            (session_date, value, estimated))

    _savepoint_guarded(cur, 'sp_ab_open_write', _write_snapshot,
                       on_error_level=logging.ERROR)
    return value, source


def _iso_variants(dt) -> set:
    """Spellings of `breached_at` the operator may paste into .env."""
    if dt is None:
        return set()
    if getattr(dt, 'tzinfo', None) is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return {dt.isoformat(),
            dt.replace(microsecond=0).isoformat(),
            dt.isoformat().replace('+00:00', 'Z'),
            dt.replace(microsecond=0).isoformat().replace('+00:00', 'Z')}


def rearm_requested(state: dict) -> bool:
    """True iff the operator echoed back THIS halt's breached_at. Scoping the
    token to the exact breach is what stops a stale .env line from silently
    re-arming a later, different halt."""
    token = (os.environ.get(REARM_ENV) or '').strip()
    if not token or not state.get('halted'):
        return False
    return token in _iso_variants(state.get('breached_at'))


def clear_halt(cur, alpha: float, alpha_pnl_now=None) -> bool:
    """Operator re-arm: drop the latch and reset the rolling peak to the
    current alpha NAV, so the next drawdown is measured from here. Also
    resets flatten_attempts to 0 — a fresh arm should not inherit a stale
    retry count from the halt it just cleared (a literal, not a bound
    param, so it never disturbs the existing `params[0] == alpha` contract
    callers already rely on). Returns True iff the write landed (supplement
    item 2); a failed write is logged as an ERROR and swallowed, same as
    save_state — run_once() checks this and lets the same operator token
    retry the re-arm on the next tick rather than silently doing nothing."""
    def _write():
        cur.execute(
            """
            UPDATE account_breaker_state
               SET halted = FALSE, reason = NULL, breached_at = NULL,
                   peak_alpha_nav = %s, pending_flatten = FALSE,
                   flatten_attempts = 0,
                   rearmed_at = NOW(), updated_at = NOW()
             WHERE id = 1
            """,
            (float(alpha),),
        )

    ok, _ = _savepoint_guarded(cur, 'sp_ab_clear_halt', _write, on_error_level=logging.ERROR)
    if ok and alpha_pnl_now is not None:
        # C1 amendment 2: the alpha-P&L high-water mark resets to the current
        # alpha_pnl (mirror of the peak_alpha_nav reset). The EPOCH is never
        # touched. Own savepoint: a missing migration-160 column must not undo
        # the re-arm above.
        save_peak_alpha_pnl(cur, alpha_pnl_now)
    return ok


def format_line(mode: str, *, equity, bench_mv, alpha, st, open_equity,
                open_src, halted, flatten=None, pnl=None) -> str:
    """The operator greps `[account_breaker] shadow` / `[account_breaker] armed`.
    Emitted on EVERY tick — a missing line means the process died, which is why
    rule=none exists. Do not reorder or rename existing tokens; `flatten_partial`
    is appended at the END when `flatten` is given (flatten.get, not flatten[] —
    this line must never KeyError on every-tick emission)."""
    daily = 'n/a' if st.get('daily') is None else f"{st['daily']:.4f}"
    # peak/dd render 'n/a' rather than raising on None (supplement item 5):
    # the line must be emitted on EVERY tick — a missing line is the signal
    # the process died — so a defensive None here (e.g. a future caller that
    # hasn't seeded a peak yet) must never itself take the process down.
    peak_v = st.get('peak')
    peak_txt = 'n/a' if peak_v is None else f"{float(peak_v):.2f}"
    dd_v = st.get('dd')
    dd_txt = 'n/a' if dd_v is None else f"{float(dd_v):.4f}"
    line = (f"[account_breaker] {mode} equity={float(equity):.2f} "
            f"bench_mv={float(bench_mv):.2f} alpha_nav={float(alpha):.2f} "
            f"peak={peak_txt} dd={dd_txt} "
            f"open_equity={float(open_equity):.2f} open_src={open_src} "
            f"daily={daily} rule={st['rule']} breach={int(bool(st['breach']))} "
            f"halted={int(bool(halted))}")
    if flatten is not None:
        line += (f" flatten_ok={int(flatten['ok'])} "
                 f"flatten_fail={int(flatten['fail'])} "
                 f"pending={int(bool(flatten['pending']))} "
                 f"flatten_partial={int(flatten.get('partial', 0))}")
    # C1 amendment 2 — APPENDED (existing tokens above untouched).
    if pnl is not None:
        def _m(v):
            return 'n/a' if v is None else f"{float(v):.2f}"
        dd_pnl = pnl.get('dd_pnl')
        dd_nav = st.get('dd_nav')
        line += (f" | alpha_pnl={_m(pnl.get('alpha_pnl'))} "
                 f"realized={_m(pnl.get('realized'))} "
                 f"unrealized={_m(pnl.get('unrealized'))} "
                 f"hwm={_m(pnl.get('hwm'))} "
                 f"dd_pnl={'n/a' if dd_pnl is None else f'{float(dd_pnl):.4f}'} "
                 f"unmatched={int(pnl.get('unmatched', 0))} "
                 f"recon={int(pnl.get('recon', 0))} "
                 f"excluded={int(pnl.get('excluded', 0))}"
                 f" | legacy alpha_nav={float(alpha):.2f} "
                 f"dd_nav={'n/a' if dd_nav is None else f'{float(dd_nav):.4f}'}")
    return line


# ── action layer (C1c) ───────────────────────────────────────────────────────
#
# Mirrors regime_blended_sizer._OCC_RE (:923) — option legs and crypto pairs are
# out of scope for the equity flatten; the option book has its own lifecycle.
_OCC_RE = re.compile(r'^[A-Z.]{1,6}\d{6}[CP]\d{8}$')

# Settle delay between the live submit loop and the post-loop broker re-read
# (fix round 2, item nit-1). A module constant rather than a bare
# `time.sleep(0.5)` call so tests can zero it out by monkeypatching the
# attribute (`monkeypatch.setattr(ab, '_SETTLE_S', 0)`) instead of patching
# `time.sleep` itself, which would also silence any *other* sleep this module
# grows later.
_SETTLE_S = 0.5


def _is_equity_symbol(sym) -> bool:
    s = str(sym or '').strip().upper()
    return bool(s) and '/' not in s and not _OCC_RE.match(s)


def bench_tickers(cur):
    """Tickers of the benchmark (beta) sleeve, or None when the lookup FAILED
    OR resolved to zero tickers despite a benchmark-sleeve strategy existing.

    None is load-bearing: the caller must refuse to flatten on None rather than
    treat "unknown" as "no benchmark", which would close the very sleeve C1 is
    required to leave untouched. Fails CLOSED in two distinct cases: (1) either
    query raises (DB hiccup, missing table, etc — caught by _savepoint_guarded,
    which rolls back to the savepoint so the caller's transaction survives),
    and (2) a benchmark-sleeve strategy_registry row EXISTS but resolves to
    ZERO tickers in execution_signals — that is NOT the same thing as "no
    benchmark sleeve configured" (a genuinely empty ids list, which IS a safe
    empty set), so it must not be conflated with it: a sleeve that exists but
    has gone quiet in execution_signals is exactly the ambiguous state where
    silently proceeding could flatten a ticker the caller just doesn't know
    about yet.

    Takes the caller's `cur` — like every other helper in this module (fix
    round 1, item 2) — rather than opening its own connection/cursor, so its
    reads compose with the caller's existing transaction/savepoint discipline
    instead of racing a second implicit transaction against it. Callers that
    still hold a bare `conn` must call `conn.cursor()` themselves first."""
    def _read_ids():
        cur.execute("SELECT id FROM strategy_registry "
                    "WHERE (parameters ->> 'benchmark_sleeve') = 'true'")
        return sorted({r[0] for r in (cur.fetchall() or []) if r and r[0]})

    ok, ids = _savepoint_guarded(cur, 'sp_ab_bench_ids', _read_ids)
    if not ok:
        return None
    if not ids:
        return set()

    def _read_tickers():
        cur.execute(
            """
            SELECT DISTINCT ticker FROM execution_signals
             WHERE strategy_id = ANY(%s)
               AND target_date >= (CURRENT_DATE - %s::int)
            """,
            (ids, BENCH_LOOKBACK_DAYS))
        return {r[0] for r in (cur.fetchall() or []) if r and r[0]}

    ok2, tickers = _savepoint_guarded(cur, 'sp_ab_bench_tickers', _read_tickers)
    if not ok2:
        return None
    if not tickers:
        logger.error(
            '[account_breaker] %d benchmark-sleeve strategy id(s) resolved to '
            'ZERO tickers in the last %d days — refusing to flatten this tick '
            '(fail CLOSED; NOT the same as "no benchmark sleeve configured")',
            len(ids), BENCH_LOOKBACK_DAYS)
        return None
    return tickers


def rule_threshold(rule: str) -> float:
    """The magnitude of the limit that tripped, for circuit_breaker_fires.
    Drawdown wins when both rules fire."""
    return abs(DD_LIMIT) if str(rule).startswith('drawdown') else abs(DAILY_LIMIT)


def rule_magnitude(rule: str, st: dict) -> float:
    """The measured breach fraction that goes into
    circuit_breaker_fires.unrealized_pnl_pct_nav. The account breaker fires on
    ACCOUNT state, not on the position's own P&L, so the account-level ratio is
    the honest value to journal (the column is only read by
    _load_recent_risk_exits, which uses ticker + position_qty)."""
    if str(rule).startswith('drawdown'):
        return float(st.get('dd') or 0.0)
    return float(st.get('daily') or 0.0)


def _record_fire(cur, ticker, qty, magnitude, threshold, payload) -> None:
    """Journal one flatten attempt. Savepoint-guarded (fix round 1, item 3):
    a failed INSERT (e.g. migration not yet applied) must never abort the
    remaining closes in flatten_alpha's loop — it logs ERROR and returns,
    same fail-open posture as every other write in this module. The
    ok/fail/partial accounting in flatten_alpha happens entirely OUTSIDE
    this call, so a swallowed failure here never skews the counts."""
    def _write():
        cur.execute(
            """
            INSERT INTO circuit_breaker_fires
              (ts_utc, ticker, unrealized_pnl_pct_nav, threshold_pct, position_qty,
               close_result_json)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (datetime.now(timezone.utc), ticker, float(magnitude), float(threshold),
             float(qty), json.dumps(payload)),
        )

    _savepoint_guarded(cur, 'sp_ab_record_fire', _write, on_error_level=logging.ERROR)


def flatten_alpha(positions: dict, bench_tkrs, *, cur, live: bool, rule: str,
                  magnitude: float, journal: bool = True) -> dict:
    """Close every NON-benchmark equity position. RTH-only — the caller gates
    on regime_liquidator._market_is_open(); _close_symbol assumes RTH.

    Returns {'ok', 'fail', 'partial', 'pending', 'aborted', 'tickers'}.
    `fail` answers "did the submit itself fail" (CLI error / exception);
    `partial` answers "is the position actually flat now" — a symbol can be
    counted in BOTH when a submit failure is also confirmed still-open on the
    post-loop re-read (see below); that is deliberate, not a bug, because
    nothing sums these counts against len(tickers) — Task 7's main() only
    reads `pending`, and format_line just prints the three side by side.
    `pending` True means the caller must leave
    account_breaker_state.pending_flatten set so the next 5-minute tick
    retries; it is True whenever fail>0 OR partial>0, and also True on abort.
    The five distinguishable outcomes (fix round 1, items 1 + 4):
      - full flat:        pending=False, fail=0, partial=0
      - any submit fail:  fail>0, pending=True
      - any partial/not-flat (per-attempt partial_flatten payload, a working
        close order already resting, OR still non-zero/unparseable on the
        post-loop re-read): partial>0, pending=True
      - nothing to close: ok=fail=partial=0, pending=False, aborted=False
      - aborted (bench_tkrs is None): aborted=True, pending=True

    In SHADOW (live=False) nothing is submitted and NO broker reads happen at
    all (no open-orders check, no re-read) — `ok` counts what WOULD have been
    closed and journalled rows carry dry_run=true. `journal=False` writes
    nothing at all — main() uses that in shadow, where the spec allows a log
    line only and a sustained breach would otherwise append rows every 5
    minutes.

    Retry safety (live only, fix round 1, item 1): before resubmitting a
    symbol we check for an already-WORKING **market close** on that symbol
    (side opposite the position's own side, type=='market') via
    regime_liquidator._load_open_orders() — read once, not per symbol. If one
    is resting we do NOT resubmit or cancel it (an orphaned resubmit could
    double the close, and cancelling a working close order is the opposite of
    what a retry should do): the symbol is counted as `partial` and we move
    on WITHOUT journalling a fire row for it — circuit_breaker_fires feeds
    _load_recent_risk_exits' re-entry cooldown and
    open_reconcile._closed_today_tickers, and a non-dry-run row for an
    attempt we deliberately did not submit would write into both for
    something that didn't happen this tick. A log line records the skip
    instead. The order-symbol comparison is upper-cased on both sides, same
    precedent as the benchmark-membership check below.

    type=='market' is the deliberate, NOT optional, second half of this
    check (advisor review, post-draft): side-alone is a false-positive trap
    — a normal bracketed long's take-profit is a SELL LIMIT and its stop-
    loss a SELL STOP, both on the position's own close side, and every
    OpenClaw entry is bracketed by alpaca_executor. Side-only would flag
    ordinary protective legs as "already closing" on the very first breach
    tick, forever, on every bracketed symbol — the breaker would never
    actually flatten anything. `_close_symbol` submits its own close via
    `alpaca position close`, which is a market order; resting protective
    legs are limit/stop/stop_limit and never market. Filtering to
    type=='market' also sidesteps needing to walk `legs[]` (the way
    _collect_openclaw_orders_to_cancel does for cancellation): a real close
    order is a standalone market submission, never itself a bracket parent
    with nested legs, so scanning only top-level orders is sufficient here.

    After the live submit loop we sleep _SETTLE_S (mirrors regime_liquidator's
    own settle pattern before its re-read, liquidate_on_regime_change:683)
    then take ONE regime_liquidator._load_broker_positions() re-read — never
    a per-symbol _poll_to_terminal, which belongs to the operator-triggered
    liquidator's 90s budget, not this 5-minute tick. EVERY attempted symbol
    (all of `tickers`, not just the ones that reported success) is checked
    against the re-read (case-normalised lookup, same precedent as the
    benchmark and open-orders comparisons): still non-zero, or an
    unparseable qty (fail SAFE — treat as still open, same instinct as the
    working-close-order check), and not already bucketed `partial`, counts
    into `partial` (and, if it had been bucketed `ok`, moves out of `ok`).
    This is what catches a submission that was accepted (_close_symbol
    ok=True, no partial_flatten flag) but the order was actually rejected,
    expired, or only partially filled by the broker — `_close_symbol`'s
    `ok=True` means SUBMITTED, not filled.

    An EMPTY re-read is UNKNOWN, never "all flat" (fix round 2, item 1):
    `_load_broker_positions()` returns {} (no exception) on a non-zero CLI
    exit or a non-list payload — its PRIMARY failure mode. Both a raised
    exception AND an empty mapping put every attempted symbol (not already
    bucketed `partial`) into `partial` and log ONE WARNING line
    ("flatten re-read unavailable — treating N attempted symbol(s) as still
    open"); treating {} as "every position is flat" would have cleared
    pending_flatten on a halted breaker that never re-evaluates, so an
    unfilled close would never be retried. The benchmark sleeve is always
    held, so a truly empty book is near-impossible — the cost of this fail-
    safe reading is one extra pending tick, after which a retry that
    genuinely finds nothing left to close returns pending=False on its own
    (the `if not touched` early return above).

    The benchmark exemption is compared case-insensitively (mirrors
    alpha_nav's I-1 fix, commit 4dcf6693): `bench_tkrs` comes from
    execution_signals.ticker while `positions` keys come from the broker, and
    a casing mismatch between them must never silently flatten the sleeve
    this action is required to leave alone. Only the COMPARISON is
    normalized — the symbol journalled to circuit_breaker_fires and passed to
    _close_symbol keeps the broker's original casing, since that column is
    what _load_recent_risk_exits and open_reconcile._closed_today_tickers
    join on.

    `bench_tkrs=None` is a second, defense-in-depth fail-closed gate (the
    primary one is the caller checking bench_tickers(cur) for None before
    ever calling this function): treating None the way `set(x or ())` would
    — as "no benchmarks" — would flatten the exempt sleeve on the exact
    failure this whole action exists to prevent, so None ABORTS with nothing
    submitted and nothing journalled.

    Deferred (Task 7, needs a new account_breaker_state column — explicitly
    NOT implemented here, fix round 1, item 5): unbounded retry / an attempt
    counter and an escalation post to the operator after N consecutive
    pending ticks. Every tick currently retries identically forever."""
    if bench_tkrs is None:
        logger.error('[account_breaker] flatten ABORTED: benchmark ticker '
                     'set is None (lookup failed upstream); refusing to '
                     'treat unknown sleeve membership as empty')
        return {'ok': 0, 'fail': 0, 'partial': 0, 'pending': True,
               'aborted': True, 'tickers': []}

    bench_norm = {str(t).strip().upper() for t in bench_tkrs}
    threshold = rule_threshold(rule)

    touched: list = []
    qty_by_sym: dict = {}
    for sym in sorted(positions or {}):
        if str(sym).strip().upper() in bench_norm or not _is_equity_symbol(sym):
            continue
        try:
            qty = float((positions[sym] or {}).get('qty') or 0.0)
        except (TypeError, ValueError):
            continue
        if qty == 0.0:
            continue
        touched.append(sym)
        qty_by_sym[sym] = qty

    if not touched:
        return {'ok': 0, 'fail': 0, 'partial': 0, 'pending': False,
               'aborted': False, 'tickers': []}

    ok = fail = partial = 0
    outcome: dict = {}
    working_close_syms: set = set()

    if live:
        from execution.regime_liquidator import (
            _close_symbol, _load_broker_positions, _load_open_orders,
        )
        try:
            open_orders = _load_open_orders() or []
        except Exception as e:  # noqa: BLE001 — a bad lookup must not abort the flatten
            logger.warning('[account_breaker] open-orders lookup failed '
                           '(%s: %s); proceeding without the retry-safety '
                           'check', type(e).__name__, e)
            open_orders = []
        orig_sym_by_norm = {sym.strip().upper(): sym for sym in touched}
        for o in open_orders:
            if not isinstance(o, dict):
                continue
            osym_norm = str(o.get('symbol') or '').strip().upper()
            orig_sym = orig_sym_by_norm.get(osym_norm)
            if orig_sym is None:
                continue
            close_side = 'sell' if qty_by_sym[orig_sym] > 0 else 'buy'
            side_matches = str(o.get('side') or '').strip().lower() == close_side
            # type=='market' is load-bearing, not decorative: side alone
            # also matches an ordinary resting take-profit (sell LIMIT) or
            # stop-loss (sell STOP) on a bracketed long — every OpenClaw
            # entry is bracketed, so side-only would flag protection as "a
            # close in flight" forever and the breaker would never actually
            # submit a close. _close_symbol's own close (`alpaca position
            # close`) is a market order; protective legs never are.
            is_market_close = str(o.get('type') or '').strip().lower() == 'market'
            if side_matches and is_market_close:
                working_close_syms.add(orig_sym)

    for sym in touched:
        qty = qty_by_sym[sym]

        if live and sym in working_close_syms:
            outcome[sym] = 'partial'
            partial += 1
            logger.info('[account_breaker] %s already has a working close '
                       'order resting on the close side — not resubmitting '
                       '(no fire row for a no-op attempt)', sym)
            continue

        if live:
            try:
                closed, payload = _close_symbol(sym, qty, market_open=True)
            except Exception as e:  # noqa: BLE001 — one bad symbol must not abort the flatten
                closed, payload = False, {'error': f'{type(e).__name__}: {e}'}
            payload = dict(payload) if isinstance(payload, dict) else {'result': payload}
            payload.update({'account_breaker': True, 'rule': rule, 'dry_run': False})

            if not closed:
                outcome[sym] = 'fail'
                fail += 1
                logger.warning('[account_breaker] close FAILED %s qty=%s: %s',
                               sym, qty, payload)
            elif payload.get('partial_flatten'):
                outcome[sym] = 'partial'
                partial += 1
            else:
                outcome[sym] = 'ok'
                ok += 1
        else:
            payload = {'account_breaker': True, 'rule': rule, 'dry_run': True,
                       'would_close_qty': qty}
            outcome[sym] = 'ok'
            ok += 1

        if journal and cur is not None:
            _record_fire(cur, sym, qty, magnitude, threshold, payload)

    if live:
        time.sleep(_SETTLE_S)
        try:
            reread = _load_broker_positions()
        except Exception as e:  # noqa: BLE001 — a failed re-read must fail SAFE (unknown)
            logger.warning('[account_breaker] post-flatten broker re-read '
                           'raised (%s: %s)', type(e).__name__, e)
            reread = None

        # `_load_broker_positions()` returns {} (no exception) on a non-zero
        # CLI exit or a non-list payload — its PRIMARY failure mode (fix
        # round 2, item 1). An empty mapping must be treated exactly like a
        # raised exception: UNKNOWN, never "the book is empty so every
        # position is flat". Under the prior code reread=={} fell into the
        # "symbol not in the dict" branch below and every touched symbol read
        # back as flat, which would have cleared pending_flatten on a halted
        # breaker that never re-evaluates — an unfilled close would then
        # never be retried. The benchmark sleeve is always held, so a truly
        # empty book is near-impossible; the cost of treating {} as unknown
        # is one extra pending tick, after which a retry that genuinely finds
        # nothing left to close returns pending=False on its own (the
        # `if not touched` early return above).
        reread_unknown = reread is None or not reread
        reread_norm = ({str(k).strip().upper(): v for k, v in reread.items()}
                       if reread else {})
        if reread_unknown:
            logger.warning(
                '[account_breaker] flatten re-read unavailable — treating '
                '%d attempted symbol(s) as still open', len(touched))

        for sym in touched:
            if outcome.get(sym) == 'partial':
                continue  # already the safe bucket; the re-read adds nothing
            if reread_unknown:
                still_open = True
            else:
                # Case-normalised lookup — same precedent as the benchmark
                # membership check and the open-orders symbol match above:
                # `touched` comes from one broker read and `reread` from a
                # second, independent one, so a casing mismatch between them
                # must never be misread as "flat".
                pos = reread_norm.get(sym.strip().upper())
                if pos is None:
                    still_open = False   # gone from the book entirely: flat
                else:
                    try:
                        still_open = float(pos.get('qty')) != 0.0
                    except (TypeError, ValueError):
                        still_open = True   # unparseable qty: fail SAFE
            if still_open:
                partial += 1
                if outcome.get(sym) == 'ok':
                    ok -= 1
                    outcome[sym] = 'partial'
                # else outcome[sym] == 'fail': stays 'fail' too — additive,
                # see docstring (fail answers "did submit work", partial
                # answers "is it flat"; one symbol can be both).

    return {'ok': ok, 'fail': fail, 'partial': partial,
           'pending': fail > 0 or partial > 0, 'aborted': False,
           'tickers': touched}


# ── entry point (C1e, Task 7) ────────────────────────────────────────────────

def _post(channel: str, msg: str) -> None:
    """Best-effort Discord post — a webhook failure must never abort a tick."""
    try:
        from execution.regime_liquidator import _post_to_discord
        _post_to_discord(channel, msg)
    except Exception as e:  # noqa: BLE001
        logger.warning('[account_breaker] Discord post failed: %s', e)


def _commit(conn) -> bool:
    """conn.commit() that fails OPEN and LOUD. A commit failure here must
    never crash the 5-minute tick (the caller decides what to do next), but
    supplement item 2 requires every caller to be able to tell it happened —
    this is what the pre-flatten gate in run_once() checks."""
    try:
        conn.commit()
        return True
    except Exception as e:  # noqa: BLE001
        logger.error('[account_breaker] commit failed (%s: %s)', type(e).__name__, e)
        return False


def _flatten_escalation_msg(attempts: int, flat: dict) -> str:
    """Shared wording for both 'flatten still PENDING' escalation posts (the
    already-halted retry branch and the fresh new-breach branch) — fix
    round 1 items 1+2: `flat['tickers']` is the ATTEMPTED set (every
    non-benchmark equity symbol flatten_alpha tried to close this tick), NOT
    the residual/still-open set — residual identities aren't tracked at all,
    so labelling this list "Residual symbols" (the prior wording) was
    actively misleading an operator about which symbols are still open.
    Also repeats the option-book carve-out (item 1): OCC legs are never
    touched by this breaker, so a still-unhedged option book is exactly the
    kind of thing an escalated operator page needs to say out loud."""
    return (
        ':rotating_light: **Account breaker flatten still PENDING** '
        f"after {attempts} consecutive 5-minute ticks (~{attempts * 5} min). "
        f"attempted: {sorted(flat['tickers'])} — fail={flat['fail']} "
        f"partial={flat['partial']} pending={int(bool(flat['pending']))} "
        '(still open per the last re-read; residual identities are not '
        'tracked). Operator attention needed — check broker positions '
        'directly; the breaker keeps retrying automatically. Option legs '
        '(OCC) are NOT flattened by the breaker — the option book stays '
        'open; close by hand if it is now unhedged.'
    )


# last FILL activity id seen by this process (in-memory only, C1 amendment 2a) —
# logged so an operator can see the feed advance; the fetch itself is always the
# full since-epoch window (the ledger is recomputed from scratch each tick).
_LAST_ACTIVITY_ID = None

FALLBACK_POST_PATH_ENV = 'OPENCLAW_ACCOUNT_BREAKER_FALLBACK_POST_PATH'
FALLBACK_POST_EVERY_S = 30 * 60


def _epoch_after_arg(epoch_at) -> str:
    """CLI `--after` value (YYYY-MM-DDTHH:MM:SSZ, UTC, floored to the second so a
    sub-second epoch never excludes its own second; the SQL read re-filters on
    the exact epoch)."""
    if isinstance(epoch_at, datetime):
        dt = epoch_at if epoch_at.tzinfo else epoch_at.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return str(epoch_at)


def sync_fills_since_epoch(cur, epoch_at) -> bool:
    """C1 amendment 2a (F1): pull FILL activities since the epoch from the broker
    (alpaca_reconcile.fetch_fills_since — same client/parsing as the reconcile
    step) and upsert them through alpaca_reconcile.ingest_broker_fills (same
    writer, ON CONFLICT DO NOTHING, append-only) BEFORE the ledger is read.
    Order-shape columns are enriched exactly as the reconcile step does, but only
    when the pull holds activity ids not yet in broker_fills (the reconcile insert
    is DO NOTHING, so a bare breaker-first row would leave parent_order_id NULL
    for good). Returns False (caller falls back, fail-open) when the broker read
    or the upsert fails."""
    global _LAST_ACTIVITY_ID
    from execution import alpaca_reconcile as ar
    try:
        fills = ar.fetch_fills_since(_epoch_after_arg(epoch_at))
    except Exception as e:  # noqa: BLE001
        logger.error('[account_breaker] fill fetch since epoch failed (%s: %s)',
                     type(e).__name__, e)
        return False
    fills = [f for f in fills if isinstance(f, dict)]
    if not fills:
        return True
    ids = [f.get('id') for f in fills if f.get('id')]

    def _known():
        cur.execute('SELECT activity_id FROM broker_fills WHERE activity_id = ANY(%s)',
                    (ids,))
        return {r[0] for r in (cur.fetchall() or [])}
    ok, known = _savepoint_guarded(cur, 'sp_ab_fill_known', _known)
    new_fills = fills if not ok else [f for f in fills if f.get('id') not in known]
    meta = {}
    if new_fills:
        try:
            from execution.stop_reattach import fetch_recent_closed_orders
            syms = sorted({f.get('symbol') for f in new_fills if f.get('symbol')})
            ok_meta, orders = fetch_recent_closed_orders(syms, include_unscoped=False)
            if ok_meta:
                meta = ar.build_order_meta(orders)
        except Exception as e:  # noqa: BLE001
            logger.warning('[account_breaker] closed-order enrichment failed (%s: %s); '
                           'new fills land with NULL order-shape columns',
                           type(e).__name__, e)
    ok, _ = _savepoint_guarded(
        cur, 'sp_ab_fill_ingest',
        lambda: ar.ingest_broker_fills(cur, fills, meta),
        on_error_level=logging.ERROR)
    if ok:
        prev, _LAST_ACTIVITY_ID = _LAST_ACTIVITY_ID, fills[-1].get('id')
        logger.debug('[account_breaker] fills since epoch: %d pulled, %d new, '
                     'last_activity_id %s -> %s', len(fills), len(new_fills),
                     prev, _LAST_ACTIVITY_ID)
    return ok


def _fallback_post_path() -> Path:
    return Path(os.environ.get(FALLBACK_POST_PATH_ENV)
                or ROOT / 'logs' / 'account_breaker_fallback_post.ts')


def post_fallback_notice(now=None) -> bool:
    """F3: an ARMED tick with no alpha-P&L measure skips the drawdown rule; tell
    #trade-reports at most once per 30 min. Cron runs are separate processes, so
    the throttle is a tiny timestamp file (fail-open: an unreadable file posts, an
    unwritable one still posts). Returns True iff a post was attempted."""
    now = time.time() if now is None else now
    path = _fallback_post_path()
    try:
        last = float(path.read_text().strip())
        if 0 <= now - last < FALLBACK_POST_EVERY_S:
            return False
    except Exception:  # noqa: BLE001
        pass
    _post('trade-reports',
          ':warning: **Account breaker degraded** — alpha P&L could not be computed '
          'this tick (migration 160 / fill sync / ledger read); the DRAWDOWN rule is '
          'SKIPPED while armed (daily-loss still evaluated). Repeats at most every '
          '30 min until the measure recovers.')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(now))
    except Exception as e:  # noqa: BLE001
        logger.warning('[account_breaker] fallback-post throttle write failed: %s', e)
    return True


def compute_alpha_pnl_tick(cur, conn, equity, positions, bench):
    """One tick of the C1-amendment-2 measure. Returns
    {alpha_pnl, realized, unrealized, unmatched, hwm, dd_pnl, ...} or None when
    it cannot be computed this tick (migration 160 missing, a read failed, the
    epoch snapshot did not land) — the caller then falls back to the legacy NAV
    drawdown for that tick, fail-open, logged. On the first tick with
    alpha_epoch_at IS NULL the epoch snapshot is written once, in one
    savepoint, and committed immediately."""
    a_state = load_alpha_state(cur)
    if a_state is None:
        logger.warning('[account_breaker] alpha P&L state unreadable (migration 160 '
                       'applied?); legacy NAV drawdown this tick')
        return None
    peak_pnl, epoch_at = a_state
    if epoch_at is None:
        if not take_alpha_epoch(cur, positions, bench) or not _commit(conn):
            logger.error('[account_breaker] alpha epoch snapshot did not land; '
                         'legacy NAV drawdown this tick')
            return None
        a_state = load_alpha_state(cur)
        if a_state is None or a_state[1] is None:
            logger.error('[account_breaker] alpha epoch not readable after snapshot; '
                         'legacy NAV drawdown this tick')
            return None
        peak_pnl, epoch_at = a_state
        logger.info('[account_breaker] alpha epoch taken at %s', epoch_at)
    if not sync_fills_since_epoch(cur, epoch_at):
        logger.warning('[account_breaker] fill sync since epoch failed; the ledger '
                       'would be incomplete — no alpha P&L this tick')
        return None
    inputs = load_alpha_inputs(cur, epoch_at)
    if inputs is None:
        logger.warning('[account_breaker] alpha P&L inputs unreadable; legacy NAV '
                       'drawdown this tick')
        return None
    lots, fills = inputs
    res = alpha_pnl(equity, positions, bench, fills, lots)
    a = res['alpha_pnl']
    hwm = a if peak_pnl is None else max(peak_pnl, a)
    res['hwm'] = hwm
    res['dd_pnl'] = (a - hwm) / float(equity)
    return res


def run_once(session_date=None) -> int:
    """One 5-minute evaluation. 0 = evaluated, 1 = soft failure (no
    evaluation this tick, retried in 5 minutes), 2 = misconfiguration.

    CRITICAL ordering on a NEW breach in ARMED mode (brief supplement item 1
    — overrides the plan's draft, which flattened first and persisted after):
    the halt latch (halted=True, pending_flatten=True) is persisted and
    COMMITTED *before* any broker action; only then does flatten_alpha run;
    only then is the outcome (pending_flatten, flatten_attempts) persisted in
    a SECOND write. If the pre-flatten persist or its commit fails: NO broker
    action is taken this tick, a "detected but NOT latched" warning is posted
    instead of "HALTED" as accomplished fact, and the tick returns 1 so the
    next tick re-evaluates cleanly — a crash between "halt written" and
    "flatten attempted" must never leave the DB believing the account is
    still open for new alpha risk while positions are actually mid-close (or
    vice versa: never believing it's halted when nothing was ever recorded).

    The empty/unavailable-book guard (supplement item 11, and the
    coordinator's clarification during Task 7) sits ahead of EVERY other DB
    interaction — including `load_state` — so it uniformly protects both the
    fresh-evaluation branch and the already-halted retry branch: neither may
    ever call flatten_alpha with an empty positions mapping, because
    flatten_alpha's own "nothing to close" outcome (pending=False) would
    then be misread as "fully flattened" when it was actually just a failed
    broker read, clearing pending_flatten on a halted breaker that never
    re-evaluates and so would never retry the real flatten.
    """
    from execution.alpaca_trader import _alpaca_session, _fetch_account_state
    from execution.regime_liquidator import _load_broker_positions, _market_is_open

    uri = os.environ.get('POSTGRES_URI')
    if not uri:
        logger.error('[account_breaker] POSTGRES_URI not set; aborting')
        return 2
    if not _market_is_open():
        logger.info('[account_breaker] market closed; skipping')
        return 0

    try:
        equity = float(_fetch_account_state(_alpaca_session())['equity'])
    except Exception as e:  # noqa: BLE001
        logger.error('[account_breaker] account fetch failed (%s: %s); aborting',
                     type(e).__name__, e)
        return 1
    if equity <= 0:
        # _fetch_account_state returns zeros on failure — never evaluate on that.
        logger.error('[account_breaker] equity=%s unusable; aborting', equity)
        return 1

    positions = _load_broker_positions()
    if not positions:
        # supplement item 11 (+ coordinator clarification): an empty/None
        # book must not evaluate AT ALL — bench_mv would read 0 and alpha_nav
        # would silently become total equity (a 10% ALPHA rule turning into
        # a 10% TOTAL-NAV rule), and nothing could be flattened either way.
        # Checked before load_state/the halted branch even exists, so a
        # pending flatten from a PRIOR tick is left completely untouched —
        # no state write happens on this path at all.
        logger.error('[account_breaker] positions read empty/unavailable — '
                     'skipping tick')
        return 1

    live = armed()
    mode = 'armed' if live else 'shadow'
    session = session_date or datetime.now(_ET).date()

    conn = None
    try:
        conn = psycopg2.connect(uri)

        # Every read/write below relies on SAVEPOINT-guarded queries
        # composing with the caller's own transaction (module docstring,
        # supplement item 4): autocommit would disable that guard entirely,
        # silently turning one failed query into a poisoned connection for
        # the rest of the tick. A plain `if` + `return 2` (fix round 1 item
        # 3) rather than `assert` — `assert` is stripped under `python -O`,
        # and the brief's exit-code contract wants an ordinary return here,
        # not an uncaught AssertionError. No DB call happens beyond this
        # check (cur = conn.cursor() is the very next line).
        if conn.autocommit:
            logger.error('[account_breaker] connection is autocommit=True; '
                         'refusing — every savepoint guard in this module '
                         'depends on non-autocommit')
            return 2

        cur = conn.cursor()
        bench_raw = bench_tickers(cur)
        if bench_raw is None:
            logger.error('[account_breaker] benchmark ticker lookup failed; '
                         'NO evaluation and NO flatten this tick')
            return 1
        # Defense-in-depth normalization at the call site too (supplement
        # item 7) — alpha_nav/flatten_alpha already normalize internally;
        # this just means every consumer downstream of this point sees the
        # same casing, not a functional change.
        bench = {str(t).strip().upper() for t in bench_raw}
        alpha, bench_mv = alpha_nav(equity, positions, bench)

        state = load_state(cur)
        pnl = compute_alpha_pnl_tick(cur, conn, equity, positions, bench)

        if rearm_requested(state):
            rearmed = clear_halt(cur, alpha,
                                 alpha_pnl_now=None if pnl is None else pnl['alpha_pnl'])
            # fix round 1 item 3: `_commit` is now gated on `rearmed` too —
            # a failed clear_halt already rolled back to its own savepoint
            # (nothing to commit), and on a raising/failing commit AFTER a
            # successful clear_halt write, the pre-clear `state` (still
            # halted) must be kept rather than optimistically switching to
            # the un-halted default a write that never durably landed.
            if rearmed and _commit(conn):
                logger.info('[account_breaker] re-armed by operator token; '
                            'peak reset to %.2f', alpha)
                state = {'halted': False, 'reason': None, 'breached_at': None,
                         'peak': alpha, 'dd': None, 'daily': None,
                         'pending_flatten': False}
                if pnl is not None:
                    pnl['hwm'], pnl['dd_pnl'] = pnl['alpha_pnl'], 0.0
            else:
                logger.error('[account_breaker] re-arm failed to persist '
                             '(clear_halt=%s); halt latch left in place, '
                             'will retry next tick on the same operator '
                             'token', rearmed)

        open_eq, open_src = opening_equity(cur, session, equity)

        if state['halted']:
            # Latched. Never re-evaluate and never move the peak — only
            # retry a flatten that failed to submit or was left partially
            # open on an earlier tick.
            st = {'peak': alpha if state['peak'] is None else state['peak'],
                  'dd': 0.0 if state['dd'] is None else state['dd'],
                  'daily': state['daily'], 'rule': state['reason'] or 'none',
                  'breach': True}
            flat = None
            prior_attempts = attempts = 0
            if state['pending_flatten'] and live:
                prior_attempts = load_flatten_attempts(cur)
                flat = flatten_alpha(positions, bench, cur=cur, live=True,
                                     rule=st['rule'],
                                     magnitude=rule_magnitude(st['rule'], st))
                attempts = prior_attempts + 1 if flat['pending'] else 0
                if not save_state(cur, halted=True, reason=state['reason'],
                                  breached_at=state['breached_at'], peak=st['peak'],
                                  dd=st['dd'], daily=st['daily'],
                                  pending_flatten=flat['pending'],
                                  flatten_attempts=attempts):
                    logger.error('[account_breaker] failed to persist the '
                                 'flatten-retry status; will retry next tick')
            _commit(conn)

            logger.debug('[account_breaker] bench_mv=%.2f bench_tickers=%s',
                         bench_mv, sorted(bench))
            logger.info(format_line(mode, equity=equity, bench_mv=bench_mv,
                                    alpha=alpha, st=st, open_equity=open_eq,
                                    open_src=open_src, halted=True, flatten=flat,
                                    pnl=pnl))

            if flat is not None and prior_attempts < FLATTEN_ESCALATE_AFTER <= attempts:
                _post('trade-reports', _flatten_escalation_msg(attempts, flat))
            return 0

        skip_dd = bool(live and pnl is None)
        if skip_dd:
            logger.error('[account_breaker] ARMED tick without alpha P&L: drawdown '
                         'rule skipped (daily-loss still evaluated)')
            post_fallback_notice()
        st = evaluate(alpha, state['peak'], equity, open_eq,
                      dd_override=None if pnl is None else pnl['dd_pnl'],
                      skip_drawdown=skip_dd)
        line_pnl = pnl if not skip_dd else {
            'alpha_pnl': None, 'realized': None, 'unrealized': None, 'hwm': None,
            'dd_pnl': None, 'unmatched': 0}
        peak_pnl_w = None if pnl is None else pnl['hwm']

        if not st['breach']:
            flat = None
            if not save_state(cur, halted=False, reason=None, breached_at=None,
                              peak=st['peak'], dd=st['dd'], daily=st['daily'],
                              pending_flatten=False, peak_alpha_pnl=peak_pnl_w):
                logger.error('[account_breaker] clean-tick state write '
                             'failed; will retry next tick')
            _commit(conn)

        elif not live:
            # SHADOW breach: compute the would-be flatten counts for the log
            # line ONLY. Never latch, never journal — _apply_account_breaker_
            # gate reads `halted`, so latching here would change routing
            # with the arming flag off (module docstring).
            flat = flatten_alpha(positions, bench, cur=cur, live=False,
                                 rule=st['rule'], magnitude=rule_magnitude(st['rule'], st),
                                 journal=False)
            if not save_state(cur, halted=False, reason=None, breached_at=None,
                              peak=st['peak'], dd=st['dd'], daily=st['daily'],
                              pending_flatten=False, peak_alpha_pnl=peak_pnl_w):
                logger.error('[account_breaker] shadow-tick state write '
                             'failed; will retry next tick')
            _commit(conn)

        else:
            # NEW breach, ARMED — supplement item 1's critical ordering.
            breached_at = datetime.now(timezone.utc)
            persisted = save_state(cur, halted=True, reason=st['rule'],
                                   breached_at=breached_at, peak=st['peak'],
                                   dd=st['dd'], daily=st['daily'],
                                   pending_flatten=True, peak_alpha_pnl=peak_pnl_w)
            committed = _commit(conn)
            if not persisted or not committed:
                logger.error('[account_breaker] failed to persist the halt '
                             'latch before flattening (rule=%s) — refusing '
                             'all broker action this tick', st['rule'])
                _post('trade-reports',
                      ':warning: **Account breaker breach detected but NOT '
                      f"latched** rule={st['rule']} — state persistence "
                      'failed, so NO broker action was taken this tick. '
                      'Will re-evaluate cleanly next tick.')
                return 1

            flat = flatten_alpha(positions, bench, cur=cur, live=True,
                                 rule=st['rule'], magnitude=rule_magnitude(st['rule'], st),
                                 journal=True)

            prior_attempts = load_flatten_attempts(cur)
            attempts = prior_attempts + 1 if flat['pending'] else 0
            if not save_state(cur, halted=True, reason=st['rule'],
                              breached_at=breached_at, peak=st['peak'],
                              dd=st['dd'], daily=st['daily'],
                              pending_flatten=flat['pending'],
                              flatten_attempts=attempts):
                logger.error('[account_breaker] failed to persist the '
                             'post-flatten status; latch stays halted+'
                             'pending from the pre-flatten write, next '
                             'tick retries the flatten')
            _commit(conn)

            daily_txt = 'n/a' if st['daily'] is None else f"{st['daily'] * 100:.2f}%"
            # attempted = len(tickers), NOT ok+fail (supplement item 9) — an
            # ok-but-still-held symbol moves into partial only, so ok+fail
            # can undercount. This is the Discord post only: format_line's
            # ok/fail/partial/pending tail is the byte-exact grep contract
            # (supplement item 2) and stays exactly as it is.
            attempted = len(flat['tickers'])
            _post('trade-reports',
                  ':rotating_light: **Account breaker HALTED** '
                  f"rule={st['rule']}\n"
                  + (f"• alpha P&L ${pnl['alpha_pnl']:,.0f} vs hwm ${pnl['hwm']:,.0f} "
                     f"(dd {st['dd'] * 100:.2f}% of NAV, limit {DD_LIMIT * 100:.0f}%)\n"
                     if pnl is not None else
                     f"• alpha NAV ${alpha:,.0f} vs peak ${st['peak']:,.0f} "
                     f"(dd {st['dd'] * 100:.2f}%, limit {DD_LIMIT * 100:.0f}%)\n") +
                  f"• equity ${equity:,.0f} vs session open ${open_eq:,.0f} "
                  f"(daily {daily_txt}, limit {DAILY_LIMIT * 100:.0f}%)\n"
                  f"• flattened {flat['ok']}/{attempted} alpha positions "
                  f"(fail={flat['fail']} partial={flat['partial']} "
                  f"pending={int(flat['pending'])}); benchmark sleeve "
                  f"untouched ({sorted(bench)})\n"
                  '• option legs (OCC) are NOT flattened by the breaker — '
                  'the option book stays open; close by hand if it is now '
                  'unhedged\n'
                  f"• re-arm (operator only): set "
                  f"OPENCLAW_ACCOUNT_BREAKER_REARM={breached_at.isoformat()} in .env\n"
                  '• OPENCLAW_ACCOUNT_BREAKER is read by BOTH the breaker '
                  "cron and the sizer's trade step (supplement item 10) — "
                  'flip it PROCESS-WIDE in .env and restart user-scope '
                  'johnbot, never as a per-unit Environment= drop-in')

            if prior_attempts < FLATTEN_ESCALATE_AFTER <= attempts:
                _post('trade-reports', _flatten_escalation_msg(attempts, flat))

        logger.debug('[account_breaker] bench_mv=%.2f bench_tickers=%s',
                     bench_mv, sorted(bench))
        logger.info(format_line(mode, equity=equity, bench_mv=bench_mv,
                                alpha=alpha, st=st, open_equity=open_eq,
                                open_src=open_src,
                                halted=bool(live and st['breach']), flatten=flat,
                                pnl=line_pnl))
        return 0
    except Exception as e:  # noqa: BLE001 — fix round 1 item 3: a crash
        # anywhere in this tick, including psycopg2.connect itself (now
        # inside this same try), must never propagate as a traceback — it's
        # caught, logged once, and turned into the same soft-failure return
        # every other guarded path in this function already uses.
        logger.error('[account_breaker] tick failed: %s: %s', type(e).__name__, e)
        return 1
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    return run_once()


if __name__ == '__main__':
    sys.exit(main())
