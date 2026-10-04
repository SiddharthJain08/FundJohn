#!/usr/bin/env python3
"""Account-level risk breaker (spec 2026-09-12 §3 C1, operator ruling R2).

Two rules, evaluated every 5 minutes during RTH by the SAME cron that runs
position_circuit_breaker.py (src/engine/cron-schedule.js) — no new thread:

    drawdown    (alpha_pnl - hwm(alpha_pnl)) / equity     <= -0.10   (C1 amendment 2;
                the legacy alpha_nav/rolling-peak measure was removed, Task 2 / P4)
    daily loss  (equity - opening_equity) / opening_equity <= -0.03

alpha_nav = broker equity - the market value of every BENCHMARK-sleeve ticker.
It is now DISPLAY-ONLY (the `alpha_nav=` / `bench_mv=` tokens of the log line);
the drawdown rule runs on the benchmark-excluded alpha P&L, the daily rule
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
resets the alpha-P&L high-water mark to the current alpha P&L (the legacy NAV
peak no longer exists, Task 2 / P4). A token ALWAYS re-arms, and the latch drop
is atomic (one UPDATE) with the high-water mark: on a trusted tick the HWM is the
current alpha P&L; on a distrusted tick (pnl unavailable, or recon > 0) it is
CLEARED (NULL) and re-seeds from the first trusted tick — never left at its
pre-halt value, which would re-latch immediately.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
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

DD_LIMIT = -0.10        # alpha-P&L drawdown (NAV-denominated) from its high-water mark
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


def evaluate(dd, equity: float, opening_equity, skip_drawdown: bool = False) -> dict:
    """Pure rule evaluation. Returns {'dd', 'daily', 'rule', 'breach'}.

    `dd` is the cumulative-alpha-P&L drawdown (C1 amendment 2, NAV-denominated;
    pnl['dd_pnl']). Task 2 / P4 (operator ack 2026-10-02) removed the legacy
    alpha_nav/rolling-peak measure: there is NO fallback drawdown any more. When
    `dd` is None or `skip_drawdown` is set (the alpha ledger is unavailable, or
    armed with recon>0 — P2) the drawdown rule is SKIPPED and daily-loss still
    runs. `opening_equity` None/0 disables the daily rule for that tick
    (reported as daily=None) rather than inventing a denominator."""
    skip = skip_drawdown or dd is None
    dd = 0.0 if skip else float(dd)

    daily = None
    if opening_equity not in (None, 0) and float(opening_equity) > 0:
        daily = float(equity) / float(opening_equity) - 1.0

    rules = []
    if not skip and dd <= DD_LIMIT + _EPS:
        rules.append('drawdown')
    if daily is not None and daily <= DAILY_LIMIT + _EPS:
        rules.append('daily_loss')

    return {'dd': dd, 'daily': daily,
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


_EQUITY_CLASS = 'us_equity'


def _symbol_class(sym) -> str:
    """Shape-based class guess, used ONLY for a symbol the broker gave no
    position payload for (a fill/lot of a position that is already closed — the
    fills table carries no asset_class)."""
    s = str(sym or '').strip().upper()
    if '/' in s:
        return 'crypto'
    if _OCC_RE.match(s):
        return 'us_option'
    return _EQUITY_CLASS


def _asset_class(sym, p=None) -> str:
    """P3 (Task 2): the broker position's own `asset_class` is authoritative
    (regime_liquidator._load_broker_positions carries it). Only when the payload
    has none (absent/None) do we fall back to the symbol-shape guess."""
    ac = (p or {}).get('asset_class') if isinstance(p, dict) else None
    return str(ac).strip().lower() if ac else _symbol_class(sym)


def _is_equity_position(sym, p=None) -> bool:
    return bool(str(sym or '').strip()) and _asset_class(sym, p) == _EQUITY_CLASS


_BUY_SIDES = ('buy',)
_SELL_SIDES = ('sell', 'sell_short')
_QTY_EPS = 1e-9


def _as_dt(x):
    """tz-aware UTC datetime from a datetime / ISO string, else None. A naive
    value is read as UTC (one comparison domain for fills, lot rows and the
    clock — mixing naive and aware raises TypeError)."""
    if isinstance(x, str):
        try:
            x = datetime.fromisoformat(x.strip().replace('Z', '+00:00'))
        except ValueError:
            return None
    if not isinstance(x, datetime):
        return None
    return x if x.tzinfo else x.replace(tzinfo=timezone.utc)


def _select_lot_rows(rows):
    """Amendment 2b ledger read. Per ticker key the LATEST lot row (max
    taken_at; on a tie a rebase beats an epoch row, then the later row) decides:
      * latest is a `rebase` row -> only that row seeds the key's book, only
        fills with filled_at > its taken_at apply to the key, and its
        realized_carry joins the key's realized P&L;
      * otherwise (epoch rows only) -> every row of the key applies exactly as
        before the rebase feature existed, with no fill cutoff.
    A rebase row with an unreadable taken_at is IGNORED (logged): the key then
    falls back to its epoch row and recon flags it — never a guessed cutoff.
    Returns (rows_to_apply, {key: cutoff_dt}, {key: carry})."""
    by_key = {}
    for i, r in enumerate(rows):
        k = _pos_key(r.get('ticker'))
        by_key.setdefault(k, []).append((i, r))
    keep, cutoff, carry = [], {}, {}
    floor = datetime.min.replace(tzinfo=timezone.utc)
    for k, items in by_key.items():
        usable = []
        for i, r in items:
            is_rb = str(r.get('kind') or '').lower() == 'rebase'
            dt = _as_dt(r.get('taken_at'))
            if is_rb and dt is None:
                logger.error('[account_breaker] rebase row for %s has no usable '
                             'taken_at; ignored', k)
                continue
            usable.append(((dt or floor, 1 if is_rb else 0, i), i, r, is_rb, dt))
        if not usable:
            continue
        best = max(usable, key=lambda u: u[0])
        if best[3]:
            keep.append((best[1], best[2]))
            cutoff[k] = best[4]
            carry[k] = _f(best[2].get('realized_carry'), 0.0)
        else:
            keep.extend((u[1], u[2]) for u in usable if not u[3])
    keep.sort(key=lambda t: t[0])
    return [r for _, r in keep], cutoff, carry


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

    Scope (F4 / P3): benchmark tickers AND anything whose broker `asset_class`
    is not `us_equity` (options, crypto) are excluded from BOTH legs; the number
    of distinct excluded keys is returned as `excluded` and per class as
    `excluded_by_class`. Symbols are matched on `_pos_key` so `BTC/USD` fills
    and `BTCUSD` positions land on the same (excluded) key; a fill/lot key with
    no live position takes the shape-based class (see _symbol_class).
    The unrealized leg prefers the broker's `unrealized_pl`, falling back to
    qty*(cur-avg). `recon` = tickers whose ledger qty != broker qty (including
    closed positions with a non-zero ledger qty). Fail-open: nothing raises.

    Amendment 2b (rebase): epoch_lots rows may carry `kind` / `taken_at` /
    `realized_carry` (see _select_lot_rows). Rows without `kind` (the legacy
    shape) and epoch-only keys behave exactly as before.

    Returns {alpha_pnl, realized, unrealized, unmatched, unpriced, n_positions,
    excluded, excluded_by_class, recon} plus the additive per-key maps
    `realized_by_key` ({key: realized $ incl. carry}) and `mismatch`
    ({key: (ledger_qty, broker_qty)})."""
    bench = {_pos_key(t) for t in (bench_tickers or ())}
    fills = list(fills_since_epoch or ())
    lots_in = list(epoch_lots or ())
    positions = positions or {}
    rb_cutoff, rb_carry = {}, {}
    if any(str(l.get('kind') or '').lower() == 'rebase' for l in lots_in):
        lots_in, rb_cutoff, rb_carry = _select_lot_rows(lots_in)

    # Class per key: a live broker position's asset_class wins; keys seen only
    # in fills/lots (closed positions) use the shape guess. A key is non-equity
    # when its position says so, or — absent a position — any raw symbol
    # mapping onto it fails the shape check (crypto 'BTC/USD' fills poison
    # 'BTCUSD').
    class_by_key, pos_keys = {}, set()
    for sym, p in positions.items():
        k = _pos_key(sym)
        if isinstance(p, dict) and p.get('asset_class'):
            pos_keys.add(k)                 # explicit broker class: authoritative
        class_by_key[k] = _asset_class(sym, p)
    for sym in [f.get('ticker') for f in fills] + [l.get('ticker') for l in lots_in]:
        k = _pos_key(sym)
        if not k or k in pos_keys:
            continue                        # the broker's explicit class stands
        c = _symbol_class(sym)
        if c != _EQUITY_CLASS or k not in class_by_key:
            class_by_key[k] = c
    excluded_keys = {}

    def _in_scope(sym) -> bool:
        k = _pos_key(sym)
        if not k:
            return False
        if k in bench:
            return False
        c = class_by_key.get(k, _EQUITY_CLASS)
        if c != _EQUITY_CLASS:
            excluded_keys[k] = c
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
    realized_by_key = {}
    for f in sorted(fills, key=_key):
        if not _in_scope(f.get('ticker')):
            continue
        k = _pos_key(f.get('ticker'))
        if k in rb_cutoff:
            # Amendment 2b: only fills strictly after the key's latest (rebase)
            # lot row apply; earlier ones are already inside its seed + carry.
            fdt = _as_dt(f.get('filled_at'))
            if fdt is None:
                unmatched += 1
                logger.warning('[account_breaker] fill %s %s has no usable filled_at '
                               'against a rebase cutoff (counted, skipped)',
                               k, f.get('activity_id'))
                continue
            if fdt <= rb_cutoff[k]:
                continue
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
        r_k = _apply(k, dq, px)
        realized += r_k
        realized_by_key[k] = realized_by_key.get(k, 0.0) + r_k

    for k, c in rb_carry.items():
        if c and _in_scope(k):
            realized += c
            realized_by_key[k] = realized_by_key.get(k, 0.0) + c

    recon, mismatch = 0, {}
    for k in set(book) | set(broker_qty):
        lq = book.get(k, [0.0, 0.0])[0]
        bq = broker_qty.get(k, 0.0)
        if abs(lq - bq) > 1e-4 * max(1.0, abs(bq)):
            recon += 1
            mismatch[k] = (lq, bq)
            logger.warning('[account_breaker] recon mismatch %s ledger_qty=%.6f '
                           'broker_qty=%.6f', k, lq, bq)

    return {'alpha_pnl': realized + unrealized, 'realized': realized,
            'unrealized': unrealized, 'unmatched': unmatched,
            'unpriced': unpriced, 'n_positions': n_pos,
            'excluded': len(excluded_keys),
            'excluded_by_class': dict(Counter(excluded_keys.values())),
            'recon': recon, 'realized_by_key': realized_by_key,
            'mismatch': mismatch,
            'ledger_qty_by_key': {k: v[0] for k, v in book.items()},
            'broker_qty_by_key': dict(broker_qty)}


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


def save_state(cur, *, halted, reason, breached_at, dd, daily,
               pending_flatten, flatten_attempts=None, peak_alpha_pnl=None,
               peak=None) -> bool:
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
    branch except the two that actually attempt a flatten) don't reset it.

    P4 (Task 2): `peak` (the legacy NAV peak) is accepted for call-site
    compatibility but NO LONGER WRITTEN — the peak_alpha_nav column stays in the
    schema (append-only) and simply goes stale."""
    def _write():
        cur.execute(
            """
            UPDATE account_breaker_state
               SET halted = %s, reason = %s, breached_at = %s,
                   dd = %s, daily = %s, pending_flatten = %s,
                   flatten_attempts = COALESCE(%s, flatten_attempts), updated_at = NOW()
             WHERE id = 1
            """,
            (bool(halted), reason, breached_at, dd, daily, bool(pending_flatten),
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
    failed / migration 160 is not applied / no state row (caller skips the drawdown
    rule for that tick). fetchall() only, so the caller's
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
        if str(sym).strip().upper() in bench or not _is_equity_position(sym, p):
            continue                       # F4/P3: same scope predicate as the flatten list
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


class _AlphaInputs(tuple):
    """(lots, fills) that also says whether the migration-162 columns were
    readable (`has_162`); the repair step is skipped entirely when they were not."""
    def __new__(cls, pair, has_162):
        o = super().__new__(cls, pair)
        o.has_162 = has_162
        return o


def load_alpha_inputs(cur, epoch_at):
    """(lot_rows, fills_since_epoch) as lists of dicts, or None on failure.

    The legacy epoch-lot and fills reads are UNCHANGED (same SQL). Amendment 2b:
    a separate savepoint-guarded read adds the migration-162 columns (kind,
    taken_at, realized_carry, reason, activity_ref) for ALL lot rows; when it
    succeeds and returns rows those REPLACE the legacy lot list (alpha_pnl picks
    each ticker's latest row and applies the per-ticker fill cutoff). When it
    fails (migration 162 not applied) or returns nothing, the legacy epoch lots
    are used — exactly today's behaviour. If it fails while the legacy read
    shows more than one row for a ticker (a rebase exists but cannot be read),
    the ledger would double-count: return None (drawdown skipped this tick)."""
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
    if not ok:
        return None
    lots, fills = res

    def _read_rows():
        cur.execute('SELECT kind, ticker, qty, avg_entry_price, side, taken_at, '
                    'realized_carry, reason, activity_ref '
                    'FROM account_breaker_alpha_epoch ORDER BY taken_at')
        return [{'kind': r[0], 'ticker': r[1], 'qty': r[2], 'avg_entry_price': r[3],
                 'side': r[4], 'taken_at': r[5], 'realized_carry': r[6],
                 'reason': r[7], 'activity_ref': r[8]}
                for r in (cur.fetchall() or [])]
    ok2, rows = _savepoint_guarded(cur, 'sp_ab_alpha_lot_rows', _read_rows)
    if ok2 and rows:
        return _AlphaInputs((rows, fills), True)
    if not ok2:
        keys = [_pos_key(l.get('ticker')) for l in lots]
        if len(keys) != len(set(keys)):
            logger.error('[account_breaker] lot rows carry more than one row per '
                         'ticker but the migration-162 columns are unreadable; '
                         'the ledger would double-count — no alpha P&L this tick')
            return None
    return _AlphaInputs((lots, fills), bool(ok2))


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


def clear_halt(cur, alpha=None, alpha_pnl_now=None) -> bool:
    """Operator re-arm, ONE UPDATE: drop the latch AND set the alpha-P&L
    high-water mark — to `alpha_pnl_now` when the caller has a trusted current
    alpha P&L, else to SQL NULL (cleared; it re-seeds from the first trusted
    tick). Either way the HWM is never left at its pre-halt value. `alpha` (the
    legacy NAV peak reset) is accepted and ignored. Also resets flatten_attempts
    to 0 — a fresh arm should not inherit a stale retry count (a literal). The
    NULL case binds no params. Returns True iff the write landed; a failed write
    is logged as an ERROR and swallowed, and run_once() then keeps the latch and
    retries on the same operator token next tick."""
    def _write():
        if alpha_pnl_now is None:
            cur.execute(
                """
                UPDATE account_breaker_state
                   SET halted = FALSE, reason = NULL, breached_at = NULL,
                       pending_flatten = FALSE,
                       flatten_attempts = 0,
                       peak_alpha_pnl = NULL,
                       rearmed_at = NOW(), updated_at = NOW()
                 WHERE id = 1
                """,
            )
        else:
            cur.execute(
                """
                UPDATE account_breaker_state
                   SET halted = FALSE, reason = NULL, breached_at = NULL,
                       pending_flatten = FALSE,
                       flatten_attempts = 0,
                       peak_alpha_pnl = %s,
                       rearmed_at = NOW(), updated_at = NOW()
                 WHERE id = 1
                """,
                (float(alpha_pnl_now),),
            )

    ok, _ = _savepoint_guarded(cur, 'sp_ab_clear_halt', _write, on_error_level=logging.ERROR)
    return ok


def _excluded_token(pnl) -> str:
    """P3: `excluded=0` when nothing was excluded, else the per-class counts,
    sorted, comma-joined: `excluded=crypto:2` / `excluded=crypto:2,us_option:1`
    (grep-able as excluded=(0|\\w+:\\d+(,\\w+:\\d+)*)). A pnl dict carrying only the
    legacy integer count (no per-class map) renders as `unknown:N`."""
    by = pnl.get('excluded_by_class')
    if not by:
        n = int(pnl.get('excluded', 0) or 0)
        return f'unknown:{n}' if n else '0'
    return ','.join(f'{c}:{int(n)}' for c, n in sorted(by.items()))


def format_line(mode: str, *, equity, bench_mv, alpha, st, open_equity,
                open_src, halted, flatten=None, pnl=None) -> str:
    """The operator greps `[account_breaker] shadow` / `[account_breaker] armed`.
    Emitted on EVERY tick — a missing line means the process died, which is why
    rule=none exists. Do not reorder or rename existing tokens; `flatten_partial`
    is appended at the END when `flatten` is given (flatten.get, not flatten[] —
    this line must never KeyError on every-tick emission). The `| alpha_pnl=…`
    tail ends with `excluded=… rebased=<n>` (Task 4: rebases performed THIS tick,
    normally 0; a pnl dict without the key renders rebased=0)."""
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
        line += (f" | alpha_pnl={_m(pnl.get('alpha_pnl'))} "
                 f"realized={_m(pnl.get('realized'))} "
                 f"unrealized={_m(pnl.get('unrealized'))} "
                 f"hwm={_m(pnl.get('hwm'))} "
                 f"dd_pnl={'n/a' if dd_pnl is None else f'{float(dd_pnl):.4f}'} "
                 f"unmatched={int(pnl.get('unmatched', 0))} "
                 f"recon={int(pnl.get('recon', 0))} "
                 f"excluded={_excluded_token(pnl)} "
                 f"rebased={int(pnl.get('rebased', 0) or 0)}")
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
        if (str(sym).strip().upper() in bench_norm
                or not _is_equity_position(sym, (positions or {}).get(sym))):
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
# logged so an operator can see the feed advance. The broker fetch starts at the
# persisted watermark (P1); the LEDGER is still recomputed each tick from the full
# since-epoch set read from broker_fills.
_LAST_ACTIVITY_ID = None
# R1 (Task 4 final round): the activities newly INSERTED into broker_fills by the
# most recent sync_fills_since_epoch (raw broker activity dicts); [] when nothing
# was new OR when "which were new" could not be determined. The ledger pass uses
# it to detect a LATE fill (filled_at at/before an existing rebase cutoff).
_LAST_NEW_FILLS = []

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


# P1 (Task 2): the breaker's broker pull is bounded — a page cap and a wall-clock
# budget — and a pull that hits either returns False (fallback tick), never a
# truncated ledger. 20 pages x 100 = 2000 fills per tick is far above one
# watermark window of activity; the first sync after migration 161 (watermark
# NULL => whole epoch) is the only pull that can approach it.
FILL_MAX_PAGES = 20
FILL_BUDGET_S = 60
FILL_WATERMARK_OVERLAP = timedelta(minutes=10)


def load_fill_watermark(cur):
    """last_synced_filled_at (tz-aware datetime) or None — unset, migration 161
    not applied, or the read failed (all mean: fetch from the epoch). Own
    savepoint, fetchall() only, so no other caller's sequencing is disturbed."""
    def _read():
        cur.execute('SELECT last_synced_filled_at FROM account_breaker_state WHERE id = 1')
        return cur.fetchall()
    ok, rows = _savepoint_guarded(cur, 'sp_ab_fill_watermark', _read)
    if not ok or not rows or rows[0][0] is None:
        return None
    wm = rows[0][0]
    if isinstance(wm, datetime) and wm.tzinfo is None:
        wm = wm.replace(tzinfo=timezone.utc)
    return wm if isinstance(wm, datetime) else None


def _fetch_after(epoch_at, watermark):
    """Lower bound of the broker fetch: max(watermark - 10 min, epoch)."""
    if isinstance(epoch_at, datetime) and isinstance(watermark, datetime):
        if epoch_at.tzinfo is None:
            epoch_at = epoch_at.replace(tzinfo=timezone.utc)
        return max(watermark - FILL_WATERMARK_OVERLAP, epoch_at)
    return epoch_at


def _max_filled_at(fills):
    best = None
    for f in fills:
        ts = f.get('transaction_time')
        if not isinstance(ts, str):
            continue
        try:
            dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if best is None or dt > best:
            best = dt
    return best


def _save_fill_watermark(cur, wm) -> bool:
    """Monotone (GREATEST) so a late/overlapping pull can never move it back."""
    def _write():
        cur.execute('UPDATE account_breaker_state SET last_synced_filled_at = '
                    'GREATEST(COALESCE(last_synced_filled_at, %s), %s) WHERE id = 1',
                    (wm, wm))
    ok, _ = _savepoint_guarded(cur, 'sp_ab_save_watermark', _write,
                               on_error_level=logging.ERROR)
    return ok


def sync_fills_since_epoch(cur, epoch_at) -> bool:
    """C1 amendment 2a (F1): pull FILL activities from the broker
    (alpaca_reconcile.fetch_fills_since — same client/parsing as the reconcile
    step) and upsert them through alpaca_reconcile.ingest_broker_fills (same
    writer, ON CONFLICT DO NOTHING, append-only) BEFORE the ledger is read.
    Order-shape columns are enriched exactly as the reconcile step does, but only
    when the pull holds activity ids not yet in broker_fills (the reconcile insert
    is DO NOTHING, so a bare breaker-first row would leave parent_order_id NULL
    for good).

    P1 (Task 2): the fetch starts at max(last_synced_filled_at - 10 min, epoch)
    — the persisted watermark (migration 161) — not at the epoch every tick; the
    watermark advances only after the upsert succeeded, to the max filled_at just
    ingested. The LEDGER is unaffected: load_alpha_inputs still reads the full
    since-epoch set from broker_fills. The pull is capped (FILL_MAX_PAGES pages,
    FILL_BUDGET_S seconds): hitting either returns False — the caller treats it
    as no alpha P&L this tick (fallback), never a truncated ledger. The rows that
    DID arrive are still ingested (append-only, real fills) and the watermark
    advanced over them, so a long first catch-up converges over a few ticks
    instead of livelocking. Returns False (caller falls back, fail-open) when
    the broker read or the upsert fails."""
    global _LAST_ACTIVITY_ID, _LAST_NEW_FILLS
    _LAST_NEW_FILLS = []
    from execution import alpaca_reconcile as ar
    after = _fetch_after(epoch_at, load_fill_watermark(cur))
    truncated = False
    try:
        fills = ar.fetch_fills_since(_epoch_after_arg(after),
                                     max_pages=FILL_MAX_PAGES, raise_on_cap=True,
                                     deadline_s=FILL_BUDGET_S)
    except ar.FillPagesTruncated as e:
        truncated, fills = True, e.fills
        logger.error('[account_breaker] fill fetch truncated (%s); ingesting the %d '
                     'rows that arrived, no alpha P&L this tick', e, len(fills))
    except Exception as e:  # noqa: BLE001
        logger.error('[account_breaker] fill fetch since epoch failed (%s: %s)',
                     type(e).__name__, e)
        return False
    fills = [f for f in fills if isinstance(f, dict)]
    if not fills:
        return not truncated
    ids = [f.get('id') for f in fills if f.get('id')]

    def _known():
        cur.execute('SELECT activity_id FROM broker_fills WHERE activity_id = ANY(%s)',
                    (ids,))
        return {r[0] for r in (cur.fetchall() or [])}
    known_ok, known = _savepoint_guarded(cur, 'sp_ab_fill_known', _known)
    new_fills = fills if not known_ok else [f for f in fills if f.get('id') not in known]
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
        _LAST_NEW_FILLS = list(new_fills) if known_ok else []
        prev, _LAST_ACTIVITY_ID = _LAST_ACTIVITY_ID, fills[-1].get('id')
        logger.debug('[account_breaker] fills since %s: %d pulled, %d new, '
                     'last_activity_id %s -> %s', after, len(fills), len(new_fills),
                     prev, _LAST_ACTIVITY_ID)
        wm = _max_filled_at(fills)
        if wm is not None:
            _save_fill_watermark(cur, wm)
        if truncated and isinstance(after, datetime):
            # MINOR-2 liveness: a capped tick must move the next fetch start
            # (wm - overlap) strictly past this one, else every tick re-hits the
            # cap from the same point and falls back forever.
            a = after if after.tzinfo else after.replace(tzinfo=timezone.utc)
            if wm is None or wm - FILL_WATERMARK_OVERLAP <= a:
                logger.error('[account_breaker] fill pull capped with NO forward '
                             'progress (watermark %s, overlap %s, fetch started at '
                             '%s): every tick will fall back until the window '
                             'holds fewer fills', wm, FILL_WATERMARK_OVERLAP, a)
    return ok and not truncated


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


# ── per-ticker REBASE (Breaker alpha P&L Task 4; spec C1 Amendment 2b) ────────
#
# A broker event that changes a position's quantity WITHOUT a FILL activity
# (split, spin-off, merger, symbol change, option exercise/assignment, journal /
# ACATS transfer) leaves ledger qty != broker qty for that ticker forever, which
# under P2 makes an ARMED breaker skip the drawdown rule forever. Distrust must be
# bounded: a mismatched ticker is repaired by a per-ticker `rebase` row in
# account_breaker_alpha_epoch (re-seeds ONE ticker from the broker position and
# carries its realized P&L), never by moving the epoch and never by touching the
# high-water mark. Only an EXPLAINED, STABLE mismatch (a corporate-action activity
# with a structured symbol match and a consistent quantity exists, and the
# (ledger_qty, broker_qty) pair has not moved for REBASE_FILL_QUIET_S) is repaired
# automatically; anything else may be a missing/lagged fill (unknown real P&L)
# and waits for the operator (`--rebase`).

REBASE_FILL_QUIET_S = 120       # stable-pair age AND ingested-fill quiet window
REBASE_ESCALATE_S = 1800        # continuous distrust before the operator is told
REBASE_ACTIVITY_MAX_PAGES = 5   # bounded like P1: page cap + wall-clock budget
REBASE_ACTIVITY_BUDGET_S = 30
REBASE_ACTIVITY_LOOKBACK = timedelta(days=3)   # activity date >= ET date(first_seen - 3d)

# Alpaca account-activity type codes. verified live 2026-10-04 22:0x UTC (paper):
# unknown codes are rejected with HTTP 400 for the whole request (ONE invalid code
# fails the combined call; SSP / SSO / OPXRC are INVALID, SPLIT / SPIN / OPEXC are
# the real codes; OPEXP is not requested — expiry never moves shares).
#   AUTO     — corporate actions where the broker carries cost basis, so the
#              ledger may be re-seeded from the broker position automatically:
#              SPLIT split, SPIN spin-off, SC symbol change, NC name change,
#              MA merger/acquisition, REORG reorganisation.
#   OPERATOR — option delivery and transfers: they fold NON-alpha P&L into the
#              broker's unrealized leg, so they are fetched in the same call but
#              NEVER auto-rebase; a match is only named in the escalation notice
#              ("possible cause") and the operator decides via the CLI:
#              OPASN option assignment, OPEXC option exercise, JNLS securities
#              journal, ACATS ACATS securities transfer.
REBASE_AUTO_ACTIVITY_TYPES = ('SPLIT', 'SPIN', 'SC', 'NC', 'MA', 'REORG')
REBASE_OPERATOR_ACTIVITY_TYPES = ('OPASN', 'OPEXC', 'JNLS', 'ACATS')
REBASE_ACTIVITY_TYPES = REBASE_AUTO_ACTIVITY_TYPES + REBASE_OPERATOR_ACTIVITY_TYPES
_OPTION_ACTIVITY_TYPES = frozenset({'OPASN', 'OPEXC'})
_SYMBOL_CHANGE_TYPES = frozenset({'SC', 'NC', 'MA', 'REORG', 'SPIN'})
_OCC_UNDERLYING_RE = re.compile(r'^([A-Z.]{1,6})\d{6}[CP]\d{8}$')
_OLD_SYMBOL_FIELDS = ('old_symbol', 'symbol_old', 'from_symbol')
_NEW_SYMBOL_FIELDS = ('new_symbol', 'symbol_new', 'to_symbol', 'related_symbol')


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _broker_position(positions, key):
    for sym, p in (positions or {}).items():
        if _pos_key(sym) == key:
            return sym, (p or {})
    return None, None


def rebase_seed(positions, key):
    """(qty, avg_entry_price, side) a rebase row for `key` would hold: the BROKER
    position now — (0.0, None, None) when the broker holds none. None when the
    broker holds a position whose average entry price is unusable (never guess)."""
    _sym, p = _broker_position(positions, key)
    qty = _f((p or {}).get('qty'), 0.0) if p else 0.0
    if not qty:
        return (0.0, None, None)
    avg = _f(p.get('avg_entry_price'))
    if avg is None:
        return None
    return (abs(qty), avg, 'short' if str(p.get('side') or '').lower() == 'short' else 'long')


def _newest_fill_at(fills, key):
    best = None
    for f in fills:
        if _pos_key(f.get('ticker')) != key:
            continue
        dt = _as_dt(f.get('filled_at'))
        if dt is not None and (best is None or dt > best):
            best = dt
    return best


def _inside_quiet_window(fills, key, now) -> bool:
    nf = _newest_fill_at(fills, key)
    return nf is not None and (now - nf).total_seconds() < REBASE_FILL_QUIET_S


def _used_activity_refs(lots, key) -> set:
    return {str(r.get('activity_ref')) for r in lots
            if _pos_key(r.get('ticker')) == key and r.get('activity_ref')}


def _activity_date(a):
    for f in ('date', 'transaction_time', 'created_at'):
        v = a.get(f)
        if isinstance(v, str) and len(v) >= 10:
            try:
                return datetime.strptime(v[:10], '%Y-%m-%d').date()
            except ValueError:
                continue
    return None


def _classify_activity(a, key):
    """How non-FILL activity `a` relates to ticker `key`: None, or
    (kind, via) with kind 'structured' | 'description':
      * via 'symbol'      — the activity's `symbol` field equals the key;
      * via 'occ'         — option types (OPASN/OPEXC): the OCC contract's
                            UNDERLYING equals the key;
      * via 'related_old' / 'related_new' — an explicit old / new / related-symbol
                            field of an SC/NC/MA/REORG/SPIN activity equals the key;
      * 'description'     — the key (3+ chars, avoids 'A'/'T'/'ON') appears as a
                            whole upper-case word in `description` of such a type.
    Only a STRUCTURED match can ever auto-rebase; a description match is only
    surfaced in the escalation notice as a possible cause."""
    t = str(a.get('activity_type') or '').upper()
    if t not in REBASE_ACTIVITY_TYPES:
        return None
    raw = str(a.get('symbol') or '').strip().upper()
    if raw and _pos_key(raw) == key:
        return ('structured', 'symbol')
    if t in _OPTION_ACTIVITY_TYPES:
        m = _OCC_UNDERLYING_RE.match(raw)
        return ('structured', 'occ') if (m and m.group(1) == key) else None
    if t in _SYMBOL_CHANGE_TYPES:
        for f in _OLD_SYMBOL_FIELDS:
            if a.get(f) and _pos_key(a.get(f)) == key:
                return ('structured', 'related_old')
        for f in _NEW_SYMBOL_FIELDS:
            if a.get(f) and _pos_key(a.get(f)) == key:
                return ('structured', 'related_new')
        desc = str(a.get('description') or '')
        if len(key) >= 3 and re.search(
                r'(?<![A-Z0-9.])' + re.escape(key) + r'(?![A-Z0-9.])', desc):
            return ('description', 'description')
    return None


def _qty_tol(broker_q) -> float:
    return 1e-4 * max(1.0, abs(broker_q))


def _ratio_plausible(ledger_q, broker_q) -> bool:
    """R2: broker_q / ledger_q is a split-like ratio p/q of small positive integers
    (p, q <= 20: 2:1, 3:2, 1:10 reverse...) within the recon tolerance. A ledger
    qty of 0 (nothing to scale) or a sign change is never plausible."""
    if not ledger_q or ledger_q * broker_q <= 0:
        return False
    tol = _qty_tol(broker_q)
    for p in range(1, 21):
        for q in range(1, 21):
            if abs(broker_q - ledger_q * p / q) <= tol:
                return True
    return False


def _quantity_consistent(a, via, t, ledger_q, broker_q) -> bool:
    """Does activity `a` account for the mismatch delta = broker_q - ledger_q?
    Reads ONLY the activity's `qty` field (Alpaca non-trade `qty`; per-share /
    ratio fields are not read), and only when the match is on the key's OWN
    `symbol` (a related-symbol match carries the OTHER symbol's quantity).
      * `qty` == delta (shares added/removed), or for symbol-change-like types
        (SC/NC/MA/REORG/SPIN) == -delta (removal reported with either sign):
        consistent, no further test;
      * `qty` == broker_q (reported as the resulting position) and the "no usable
        quantity" case (absent / unparseable / zero) rest on weaker evidence, so
        R2 additionally requires ledger_q != 0 and broker_q/ledger_q ~ p/q for
        small positive integers p, q <= 20 (_ratio_plausible); a key that went to
        zero / appeared from zero is handled only by the old/new-symbol rules
        (R3 pairing in _repair_ledger), never here;
      * `qty` present but none of the above => NOT explained;
      * match via an OLD-symbol field -> the old key must be gone (broker_q ~ 0);
      * match via a NEW-symbol field -> no quantity test here (the qty is the old
        symbol's); _repair_ledger requires the paired old key (R3)."""
    tol = _qty_tol(broker_q)
    if via == 'related_old':
        return abs(broker_q) <= tol
    if via != 'symbol':
        return True
    q = _f(a.get('qty'))
    if q is None or q == 0:
        return _ratio_plausible(ledger_q, broker_q)
    delta = broker_q - ledger_q
    if abs(q - delta) <= tol:
        return True
    if t in _SYMBOL_CHANGE_TYPES and abs(q + delta) <= tol:
        return True
    if abs(q - broker_q) <= tol:
        return _ratio_plausible(ledger_q, broker_q)
    return False


def evaluate_activities(activities, key, ledger_q, broker_q, since_d, used_refs=()):
    """(auto_activity | None, possible_causes[str], via) for mismatched ticker `key`
    (`via` = how the auto activity matched the key; None without one).
    Auto: earliest activity of an AUTO type, dated >= since_d (the ET date of the
    watch episode's first_seen - 3 days), with an id not already an activity_ref on
    this ticker, a STRUCTURED symbol match and a consistent quantity. Everything
    else that touches the key becomes a human-readable possible cause: operator
    types ('OPASN 2026-10-05'), description-only mentions, quantity-inconsistent
    auto-type matches."""
    best, causes, best_via = None, [], None
    for a in activities or ():
        aid, d = a.get('id'), _activity_date(a)
        if d is None or since_d is None or d < since_d or (aid and str(aid) in used_refs):
            continue
        cls = _classify_activity(a, key)
        if cls is None:
            continue
        kind, via = cls
        t = str(a.get('activity_type') or '').upper()
        if kind == 'description':
            causes.append(f'{t} {d} (description mention only)')
        elif t in REBASE_OPERATOR_ACTIVITY_TYPES:
            causes.append(f'{t} {d}')
        elif not _quantity_consistent(a, via, t, ledger_q, broker_q):
            causes.append(f'{t} {d} (quantity inconsistent)')
        elif aid:
            cand = (d, str(aid))
            if best is None or cand < best[0]:
                best, best_via = (cand, a), via
    return (None if best is None else best[1]), causes, best_via


def fetch_rebase_activities(after_dt):
    """ONE bounded broker call for the REBASE_ACTIVITY_TYPES activities created
    after the ET date of `after_dt` (the caller passes earliest eligible
    first_seen_at - 3 days, so the window never grows):
    alpaca account activity list --activity-types <comma-joined union of the AUTO
    and OPERATOR lists> --after YYYY-MM-DD --direction asc --page-size 100, paged
    with --page-token like the FILL pull; page cap REBASE_ACTIVITY_MAX_PAGES and
    wall clock REBASE_ACTIVITY_BUDGET_S. Returns (activities, None), or
    (None, short_reason) on ANY failure / cap hit / timeout / rejected type — the
    caller then does not rebase this tick and the notice says the lookup failed."""
    try:
        from execution import alpaca_reconcile as ar
        after = after_dt.astimezone(_ET).date().isoformat()
        acts = ar.fetch_activities_since(
            after, REBASE_ACTIVITY_TYPES, max_pages=REBASE_ACTIVITY_MAX_PAGES,
            raise_on_cap=True, deadline_s=REBASE_ACTIVITY_BUDGET_S)
    except Exception as e:  # noqa: BLE001 — incl. FillPagesTruncated, timeouts
        name = type(e).__name__
        if name == 'FillPagesTruncated':
            reason = 'page cap / budget hit'
        elif 'Timeout' in name:
            reason = 'timeout'
        else:
            reason = (str(e) or name)[:80].replace('\n', ' ')
        logger.error('[account_breaker] corporate-action activity lookup failed '
                     '(%s: %s); no rebase this tick', name, e)
        return None, reason
    return [a for a in acts if isinstance(a, dict)
            and str(a.get('activity_type') or '').upper() != 'FILL'], None


def rebase_row(key, seed, realized_carry, taken_at, reason, activity_ref=None) -> dict:
    qty, avg, side = seed
    return {'kind': 'rebase', 'ticker': key, 'qty': qty, 'avg_entry_price': avg,
            'side': side, 'taken_at': taken_at, 'realized_carry': float(realized_carry),
            'reason': reason, 'activity_ref': activity_ref}


def write_rebase_row(cur, row) -> bool:
    """INSERT one rebase row in ONE savepoint (all-or-nothing). Never touches
    alpha_epoch_at or peak_alpha_pnl. Returns True iff it landed."""
    def _write():
        cur.execute(
            'INSERT INTO account_breaker_alpha_epoch '
            '(ticker, qty, avg_entry_price, side, taken_at, kind, realized_carry, '
            'reason, activity_ref) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)',
            (row['ticker'], row['qty'], row['avg_entry_price'], row['side'],
             row['taken_at'], 'rebase', row['realized_carry'], row['reason'],
             row['activity_ref']))
    ok, _ = _savepoint_guarded(cur, 'sp_ab_rebase', _write, on_error_level=logging.ERROR)
    return ok


_RECON_WATCH_UPSERT = (
    'INSERT INTO account_breaker_recon_watch '
    '(ticker, first_seen_at, last_seen_at, notified_at, cleared_at, ledger_qty, '
    'broker_qty) VALUES (%s, %s, %s, %s, %s, %s, %s) '
    'ON CONFLICT (ticker) DO UPDATE SET first_seen_at = EXCLUDED.first_seen_at, '
    'last_seen_at = EXCLUDED.last_seen_at, notified_at = EXCLUDED.notified_at, '
    'cleared_at = EXCLUDED.cleared_at, ledger_qty = EXCLUDED.ledger_qty, '
    'broker_qty = EXCLUDED.broker_qty')


def _load_watch(cur):
    """{ticker: (ticker, first_seen, last_seen, notified, cleared, ledger_qty,
    broker_qty, late_fill_ref)} or None when the table/columns are unreadable
    (migration 162)."""
    def _read():
        cur.execute('SELECT ticker, first_seen_at, last_seen_at, notified_at, '
                    'cleared_at, ledger_qty, broker_qty, late_fill_ref '
                    'FROM account_breaker_recon_watch')
        return cur.fetchall() or []
    ok, rows = _savepoint_guarded(cur, 'sp_ab_recon_watch_read', _read)
    return {str(r[0]): r for r in rows} if ok else None


def _episodes(watch, mism, now) -> dict:
    """Per mismatched key: {first, notified, new}. A NEW episode (first_seen = now,
    notified cleared) starts when there is no open watch row, or the stored
    (ledger_qty, broker_qty) pair differs from the current one beyond the recon
    tolerance (a lagged fill landing changes the ledger qty and so restarts it)."""
    eps = {}
    for k, (lq, bq) in mism.items():
        r = watch.get(k)
        same = False
        if r is not None and r[4] is None and r[5] is not None and r[6] is not None:
            tol = _qty_tol(bq)
            same = (abs(_f(r[5], 1e18) - lq) <= tol and abs(_f(r[6], 1e18) - bq) <= tol)
        if same:
            eps[k] = {'first': _as_dt(r[1]) or now, 'notified': _as_dt(r[3]), 'new': False}
        else:
            eps[k] = {'first': now, 'notified': None, 'new': True}
    return eps


_LATE_FILL_UPSERT = (
    'INSERT INTO account_breaker_recon_watch '
    '(ticker, first_seen_at, last_seen_at, late_fill_ref) VALUES (%s, %s, %s, %s) '
    'ON CONFLICT (ticker) DO UPDATE SET late_fill_ref = EXCLUDED.late_fill_ref')
_LATE_FILL_CLEAR = ('UPDATE account_breaker_recon_watch SET late_fill_ref = NULL '
                    'WHERE ticker = %s')


def _late_markers(watch) -> dict:
    """{ticker: late_fill_ref} for every OPEN late-fill marker."""
    return {k: str(r[7]) for k, r in watch.items() if len(r) > 7 and r[7]}


def _force_late(res, markers) -> dict:
    """R1: an open late-fill marker forces its ticker into `mismatch` (and `recon`)
    on EVERY tick, whatever the quantities say, until the operator clears it.
    Idempotent."""
    res['late_fill'] = dict(markers)
    mism = res.setdefault('mismatch', {})
    for k in markers:
        if k not in mism:
            mism[k] = ((res.get('ledger_qty_by_key') or {}).get(k, 0.0),
                       (res.get('broker_qty_by_key') or {}).get(k, 0.0))
            res['recon'] = int(res.get('recon', 0) or 0) + 1
    return res


def _detect_late_fills(cur, lots, bench, watch, new_fills, now):
    """R1: a fill NEWLY ingested by this tick's sync whose filled_at is at/before its
    ticker's latest REBASE cutoff is skipped by the per-ticker cutoff, and if it was
    a closing fill its realized P&L is in neither the carry nor the ledger. Detect it
    (never skip silently): log ERROR, persist an open marker in
    account_breaker_recon_watch.late_fill_ref (one savepoint) so it survives across
    ticks. Mechanism: the newly-inserted set of this tick's sync (no schema beyond
    migration 162). Returns the updated `watch`."""
    if not new_fills:
        return watch
    _rows, cutoff, _carry = _select_lot_rows(lots)
    add = {}
    for f in new_fills:
        sym = f.get('symbol')
        k = _pos_key(sym)
        if not k or k in bench or k not in cutoff or _symbol_class(sym) != _EQUITY_CLASS:
            continue
        fdt = _as_dt(f.get('transaction_time'))
        if fdt is None or fdt > cutoff[k]:
            continue
        aid = str(f.get('id'))
        logger.error('[account_breaker] LATE FILL %s %s filled_at=%s <= rebase cutoff %s: '
                     'realized P&L of this fill is NOT in the ledger',
                     aid, k, fdt.isoformat(), cutoff[k].isoformat())
        add.setdefault(k, []).append(f'{aid}@{fdt.isoformat()}')
    for k, refs in add.items():
        r = watch.get(k)
        cur_ref = str(r[7]) if r is not None and len(r) > 7 and r[7] else ''
        have = {x.split('@')[0] for x in cur_ref.split(';') if x}
        refs = [x for x in refs if x.split('@')[0] not in have]
        if not refs:
            continue
        new_ref = ';'.join([x for x in cur_ref.split(';') if x] + refs)
        first = _as_dt(r[1]) if r is not None else None

        def _w(k=k, new_ref=new_ref, first=first):
            cur.execute(_LATE_FILL_UPSERT, (k, first or now, now, new_ref))
        ok, _ = _savepoint_guarded(cur, 'sp_ab_late_fill', _w, on_error_level=logging.ERROR)
        base = r if r is not None else (k, now, now, None, None, None, None, None)
        watch[k] = tuple(base[:7]) + (new_ref,)
        if not ok:
            logger.error('[account_breaker] late-fill marker for %s could not be '
                         'persisted; forced distrust holds for this tick only', k)
    return watch


def clear_late_marker(cur, key) -> bool:
    """Operator rebase clears the marker (one savepoint)."""
    ok, _ = _savepoint_guarded(cur, 'sp_ab_late_clear',
                               lambda: cur.execute(_LATE_FILL_CLEAR, (key,)),
                               on_error_level=logging.ERROR)
    return ok


def _escalation_msg(due, mismatch, causes=None, lookup_fail=None, late=None) -> str:
    causes = causes or {}
    late = late or {}
    names = ','.join(sorted(due))
    detail = '; '.join(f'{k}: ledger {mismatch[k][0]:g} vs broker {mismatch[k][1]:g}'
                       for k in sorted(due))
    if lookup_fail:
        why = (f'The corporate-action activity lookup is failing ({lookup_fail}), so '
               'the breaker could not look for an explanation')
    else:
        why = 'No auto-rebasable corporate-action activity explains it'
    poss = '; '.join(f'{k}: possible cause {", ".join(causes[k])}'
                     for k in sorted(due) if causes.get(k))
    lf = '; '.join(f'{k}: late fill after a rebase — operator review required '
                   f'(late fill {late[k]}; its realized P&L is NOT in the ledger; pass '
                   '--realized-adjust <amount> to account for it)'
                   for k in sorted(due) if k in late)
    if lf:
        poss = f'{poss}; {lf}' if poss else lf
    return (f':warning: **Account breaker ledger mismatch, unexplained** — {detail} '
            f'for >= {REBASE_ESCALATE_S // 60} min. {why}, so the breaker will NOT '
            'auto-repair it (it may be a missing fill = unknown P&L) and an ARMED '
            'drawdown rule stays skipped.'
            + (f' {poss}.' if poss else '') +
            ' After confirming the broker position is right, from /root/openclaw run '
            f'`python3 src/execution/account_breaker.py --rebase {names} '
            '--reason "<why>"` (dry-run; prints ledger/broker qty and the alpha P&L '
            'before/after), then repeat with `--apply`.')


def _track_recon(cur, conn, res, now, watch, causes=None, lookup_fail=None,
                 late=None) -> None:
    """Continuity of unreconciled tickers in account_breaker_recon_watch (migration
    162). Upserts mismatched tickers with the CURRENT (ledger_qty, broker_qty)
    pair (first_seen kept while the pair is unchanged, else the episode restarts;
    last_seen advanced), marks reconciled ones cleared (never deleted; a later
    mismatch starts a NEW episode), and when a ticker has been continuously
    mismatched with a stable pair >= REBASE_ESCALATE_S and notified_at is NULL /
    older than that window, stamps notified_at, commits, THEN posts ONE operator
    notice (its own throttle — not the fallback file)."""
    mism = res.get('mismatch') or {}
    eps = _episodes(watch, mism, now)
    writes, due = [], []
    for k in sorted(mism):
        lq, bq = mism[k]
        e = eps[k]
        notified = e['notified']
        if (not e['new'] and (now - e['first']).total_seconds() >= REBASE_ESCALATE_S
                and (notified is None
                     or (now - notified).total_seconds() >= REBASE_ESCALATE_S)):
            notified = now
            due.append(k)
        writes.append((k, e['first'], now, notified, None, lq, bq))
    for k, r in watch.items():
        if k not in mism and r[4] is None:                # reconciled: mark, keep
            writes.append((k, _as_dt(r[1]) or now, _as_dt(r[2]) or now,
                           _as_dt(r[3]), now, r[5], r[6]))
    if not writes:
        return

    def _write():
        for w in writes:
            cur.execute(_RECON_WATCH_UPSERT, w)
    ok, _ = _savepoint_guarded(cur, 'sp_ab_recon_watch', _write,
                               on_error_level=logging.ERROR)
    if not ok or not due:
        return
    if not _commit(conn):
        return                      # notified_at not durable: never post unthrottled
    _post('trade-reports', _escalation_msg(due, mism, causes, lookup_fail, late))


def _repair_ledger(cur, conn, equity, positions, bench, lots, fills, res, epoch_at,
                   positions_at=None):
    """Amendment 2b, automatic leg + continuity tracking. Returns the (possibly
    recomputed) result dict with `rebased` = rebases performed this tick. Fail-open:
    nothing here raises; on any error the tick keeps what it already has.
    A mismatched ticker is auto-rebased only if ALL hold: (a) its
    (ledger_qty, broker_qty) pair is unchanged since the watch episode's
    first_seen_at and that was >= REBASE_FILL_QUIET_S ago (a lagged closing fill
    landing changes the pair and restarts the episode); (b) its newest INGESTED
    fill is older than REBASE_FILL_QUIET_S; (c) an AUTO-type activity with a
    structured symbol match and a consistent quantity, dated >= ET date(first_seen
    - 3 d), exists (ONE bounded lookup per tick, only when a ticker is eligible).
    The row's taken_at is `positions_at` (the broker-positions SNAPSHOT time t0,
    captured before the positions were loaded), so a fill after the snapshot is
    applied by the ledger and one before it is inside the seed. Every landed
    rebase is committed; the ledger is then recomputed so `recon` reflects it.
    R1: a late fill (see _detect_late_fills) opens a marker that forces the ticker
    into `mismatch` every tick and bars it from auto-rebase. R3: a new-symbol key
    rebases only together with its old-symbol key (old first)."""
    res['rebased'] = 0
    rebased = 0
    markers = {}
    try:
        now = _utcnow()
        t0 = positions_at or now
        watch = _load_watch(cur)
        if watch is None:
            logger.warning('[account_breaker] recon-watch unreadable (migration 162); '
                           'no rebase / escalation this tick')
            return res
        watch = _detect_late_fills(cur, lots, bench, watch, _LAST_NEW_FILLS, now)
        markers = _late_markers(watch)
        res = _force_late(res, markers)
        mism = res.get('mismatch') or {}
        eps = _episodes(watch, mism, now)
        eligible = []
        for k in sorted(mism):
            age = (now - eps[k]['first']).total_seconds()
            if k in markers:
                logger.error('[account_breaker] %s carries an open LATE-FILL marker (%s): '
                             'distrusted, never auto-rebased; operator review required',
                             k, markers[k])
            elif eps[k]['new'] or age < REBASE_FILL_QUIET_S:
                logger.info('[account_breaker] recon mismatch %s: pair not yet stable '
                            '(%.0fs < %ds); no rebase yet', k, age, REBASE_FILL_QUIET_S)
            elif _inside_quiet_window(fills, k, now):
                logger.info('[account_breaker] recon mismatch %s inside the %ds fill-'
                            'quiet window; no rebase yet', k, REBASE_FILL_QUIET_S)
            else:
                eligible.append(k)
        causes, lookup_fail = {}, None
        if eligible:
            acts, lookup_fail = fetch_rebase_activities(
                min(eps[k]['first'] for k in eligible) - REBASE_ACTIVITY_LOOKBACK)
            cand = {}
            for k in (eligible if acts is not None else ()):
                lq, bq = mism[k]
                since_d = (eps[k]['first'] - REBASE_ACTIVITY_LOOKBACK).astimezone(_ET).date()
                act, causes[k], via = evaluate_activities(
                    acts, k, lq, bq, since_d, _used_activity_refs(lots, k))
                if act is None:
                    logger.info('[account_breaker] recon mismatch %s has no explaining '
                                'auto-rebasable activity; staying distrusted', k)
                else:
                    cand[k] = (act, via)
            # R3: a NEW-symbol key rebases only together with its OLD-symbol key
            # (same activity, also mismatched, broker qty ~ 0), the old one first.
            def _partner(k):
                act, via = cand[k]
                if via != 'related_new':
                    return None
                for o, (oact, _ov) in cand.items():
                    if (o != k and oact.get('id') == act.get('id')
                            and abs(mism[o][1]) <= _qty_tol(mism[o][1])):
                        return o
                return None
            order = [k for k in sorted(cand) if cand[k][1] != 'related_new']
            for k in sorted(cand):
                if cand[k][1] == 'related_new':
                    if _partner(k) is None:
                        causes.setdefault(k, []).append(
                            f"{cand[k][0].get('activity_type')} new symbol only (old "
                            'symbol not being rebased together)')
                        logger.info('[account_breaker] %s is the NEW symbol of an '
                                    'activity whose old key is not rebased this tick; '
                                    'left for the operator', k)
                    else:
                        order.append(k)
            done = set()
            for k in order:
                act, via = cand[k]
                if via == 'related_new' and _partner(k) not in done:
                    continue
                lq, bq = mism[k]
                seed = rebase_seed(positions, k)
                if seed is None:
                    logger.warning('[account_breaker] cannot rebase %s: the broker '
                                   'position has no usable avg_entry_price', k)
                    continue
                typ = str(act.get('activity_type') or '').upper()
                row = rebase_row(k, seed, (res.get('realized_by_key') or {}).get(k, 0.0),
                                 t0, f'auto:{typ}', str(act.get('id')))
                if not write_rebase_row(cur, row):
                    continue
                if not _commit(conn):
                    break
                lots.append(row)
                done.add(k)
                rebased += 1
                logger.info('[account_breaker] rebased %s (auto:%s activity %s): ledger '
                            'qty %.6f -> broker qty %.6f, realized carry %.2f',
                            k, typ, act.get('id'), lq, bq, row['realized_carry'])
        if rebased:
            res = alpha_pnl(equity, positions, bench, fills, lots)
            res['rebased'] = rebased
            res = _force_late(res, markers)
        _track_recon(cur, conn, res, now, watch, causes, lookup_fail, markers)
    except Exception as e:  # noqa: BLE001 — a repair failure must never abort the tick
        logger.error('[account_breaker] ledger repair step failed (%s: %s); keeping '
                     'the unrepaired ledger', type(e).__name__, e)
        if rebased and res.get('rebased') != rebased:
            try:
                res = _force_late(alpha_pnl(equity, positions, bench, fills, lots), markers)
            except Exception:  # noqa: BLE001
                pass
            res['rebased'] = rebased
    return res


def compute_alpha_pnl_tick(cur, conn, equity, positions, bench, positions_at=None):
    """One tick of the C1-amendment-2 measure. Returns
    {alpha_pnl, realized, unrealized, unmatched, hwm, dd_pnl, ...} or None when
    it cannot be computed this tick (migration 160 missing, a read failed, the
    epoch snapshot did not land) — the caller then SKIPS the drawdown rule for that
    tick (P4: no legacy NAV fallback), fail-open, logged. On the first tick with
    alpha_epoch_at IS NULL the epoch snapshot is written once, in one
    savepoint, and committed immediately."""
    a_state = load_alpha_state(cur)
    if a_state is None:
        logger.warning('[account_breaker] alpha P&L state unreadable (migration 160 '
                       'applied?); drawdown rule skipped this tick')
        return None
    peak_pnl, epoch_at = a_state
    if epoch_at is None:
        if not take_alpha_epoch(cur, positions, bench) or not _commit(conn):
            logger.error('[account_breaker] alpha epoch snapshot did not land; '
                         'drawdown rule skipped this tick')
            return None
        a_state = load_alpha_state(cur)
        if a_state is None or a_state[1] is None:
            logger.error('[account_breaker] alpha epoch not readable after snapshot; '
                         'drawdown rule skipped this tick')
            return None
        peak_pnl, epoch_at = a_state
        logger.info('[account_breaker] alpha epoch taken at %s', epoch_at)
    if not sync_fills_since_epoch(cur, epoch_at):
        logger.warning('[account_breaker] fill sync since epoch failed; the ledger '
                       'would be incomplete — no alpha P&L this tick')
        return None
    inputs = load_alpha_inputs(cur, epoch_at)
    if inputs is None:
        logger.warning('[account_breaker] alpha P&L inputs unreadable; '
                       'drawdown rule skipped this tick')
        return None
    lots, fills = inputs
    res = alpha_pnl(equity, positions, bench, fills, lots)
    # Amendment 2b: explained mismatches are rebased (and recon re-derived) before
    # the high-water mark is evaluated; never moves the epoch or the HWM itself.
    if getattr(inputs, 'has_162', True):
        res = _repair_ledger(cur, conn, equity, positions, bench, lots, fills, res,
                             epoch_at, positions_at)
    else:
        res['rebased'] = 0          # MINOR-5: migration 162 unreadable => identical to main
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

    # t0 = the broker-positions SNAPSHOT time (Task 4 / MAJOR-1): a rebase row's
    # taken_at, so fills after it stay in the ledger and fills before it are
    # already inside the broker position it seeds from.
    t0 = _utcnow()
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
        pnl = compute_alpha_pnl_tick(cur, conn, equity, positions, bench,
                                     positions_at=t0)

        if rearm_requested(state):
            # A token ALWAYS re-arms; the latch drop and the HWM write are ONE
            # UPDATE (clear_halt). Trusted: HWM = current alpha_pnl. Distrusted
            # (pnl None, or recon>0 — P2): HWM cleared to NULL, re-seeded by the
            # first trusted tick (never left at the pre-halt peak).
            trusted = pnl is not None and not int(pnl.get('recon', 0) or 0) > 0
            rearmed = clear_halt(cur, alpha_pnl_now=pnl['alpha_pnl'] if trusted else None)
            # fix round 1 item 3: `_commit` is gated on `rearmed` too — a
            # failed clear_halt already rolled back to its own savepoint
            # (nothing to commit), and on a raising/failing commit AFTER a
            # successful clear_halt write, the pre-clear `state` (still
            # halted) must be kept rather than optimistically switching to
            # the un-halted default a write that never durably landed.
            if rearmed and _commit(conn):
                if trusted:
                    logger.info('[account_breaker] re-armed by operator token; '
                                'alpha-P&L hwm reset to %.2f', pnl['alpha_pnl'])
                else:
                    logger.warning('[account_breaker] re-armed by operator token on '
                                   'an UNTRUSTED ledger (%s); alpha-P&L hwm CLEARED '
                                   '— it re-seeds from the first trusted tick',
                                   'pnl unavailable' if pnl is None
                                   else f"recon={int(pnl.get('recon', 0) or 0)}")
                state = {'halted': False, 'reason': None, 'breached_at': None,
                         'peak': None, 'dd': None, 'daily': None,
                         'pending_flatten': False}
                if pnl is not None:
                    # the pre-clear hwm/dd_pnl in `pnl` are stale; the stored HWM
                    # is now alpha_pnl (trusted) or NULL (which seeds to
                    # alpha_pnl) — so this tick's displayed/evaluated values are
                    # hwm = alpha_pnl, dd_pnl = 0. Persistence is separately
                    # gated (peak_pnl_w None while distrusted).
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
            st = {'peak': None,
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
                                  breached_at=state['breached_at'],
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

        # P2 (Task 2): on an ARMED tick a ledger/broker quantity mismatch
        # (recon > 0) means the alpha P&L cannot be trusted to latch a flatten,
        # so it is treated EXACTLY like pnl is None: drawdown rule skipped,
        # daily-loss still runs, degraded notice posted. Shadow keeps evaluating
        # (and logging) so the operator can watch recon before arming.
        recon_n = 0 if pnl is None else int(pnl.get('recon', 0) or 0)
        skip_dd = bool(pnl is None or (live and recon_n > 0))
        if skip_dd and live:
            logger.error('[account_breaker] ARMED tick without a trustworthy alpha '
                         'P&L (%s): drawdown rule skipped (daily-loss still '
                         'evaluated)',
                         'unavailable' if pnl is None else f'recon={recon_n}')
            post_fallback_notice()
        # P4: no legacy NAV fallback — an unavailable ledger skips the drawdown
        # rule in BOTH modes (shadow shows dd_pnl=n/a rather than a NAV number).
        st = evaluate(None if pnl is None else pnl['dd_pnl'], equity, open_eq,
                      skip_drawdown=skip_dd)
        st['peak'] = None
        line_pnl = pnl if not skip_dd else {
            'alpha_pnl': None, 'realized': None, 'unrealized': None, 'hwm': None,
            'dd_pnl': None, 'unmatched': 0,
            'recon': recon_n, 'excluded': 0 if pnl is None else pnl.get('excluded', 0),
            'rebased': 0 if pnl is None else pnl.get('rebased', 0),
            'excluded_by_class': {} if pnl is None else pnl.get('excluded_by_class')}
        # MAJOR-1: never persist a HWM computed from a distrusted ledger (pnl None
        # or recon>0), in BOTH modes — a shadow tick must not seed an inflated HWM
        # that arming inherits. The line may still display the computed hwm.
        peak_pnl_w = None if (pnl is None or recon_n > 0) else pnl['hwm']

        if not st['breach']:
            flat = None
            if not save_state(cur, halted=False, reason=None, breached_at=None,
                              dd=st['dd'], daily=st['daily'],
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
                              dd=st['dd'], daily=st['daily'],
                              pending_flatten=False, peak_alpha_pnl=peak_pnl_w):
                logger.error('[account_breaker] shadow-tick state write '
                             'failed; will retry next tick')
            _commit(conn)

        else:
            # NEW breach, ARMED — supplement item 1's critical ordering.
            breached_at = datetime.now(timezone.utc)
            persisted = save_state(cur, halted=True, reason=st['rule'],
                                   breached_at=breached_at,
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
                              breached_at=breached_at,
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
                     if 'drawdown' in st['rule'] and pnl is not None else '') +
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


# ── operator rebase CLI (Task 4, spec C1 Amendment 2b) ───────────────────────

def _parse_tickers(arg) -> list:
    out, seen = [], set()
    for t in re.split(r'[,\s]+', str(arg or '')):
        k = _pos_key(t)
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def rebase_cli(cur, conn, positions, bench, tickers, reason, apply, *,
               now=None, positions_at=None, realized_adjust=0.0, out=print) -> int:
    """`--rebase TICKER[,TICKER...] [--reason TEXT] [--apply]`. Dry-run by default:
    prints, per ticker, the ledger qty, the broker qty, the row it would write and
    the alpha P&L before/after (equal by construction; a difference > 1 cent
    aborts), then ROLLS BACK — nothing persists (the fill sync it needs for an
    up-to-date ledger is in the same rolled-back transaction). `--apply` writes
    one `kind='rebase'`, reason='operator:<text>' row per ticker (one savepoint
    each, ONE commit at the end; any failure rolls everything back).
    ALL-OR-NOTHING validation: a ticker that currently reconciles, a benchmark or
    non-us_equity ticker, one inside the fill-quiet window, or one whose broker
    position has no usable average price refuses the WHOLE command (exit 2).
    Never runs a breaker tick, never moves the epoch, never touches the HWM.
    0 = ok, 1 = failed, 2 = refused."""
    now = now or _utcnow()
    taken_at = positions_at or now      # the positions SNAPSHOT time t0 (MAJOR-1)
    if not tickers:
        out('refused: no tickers given')
        return 2
    if not positions:
        out('refused: broker positions unavailable/empty (cannot tell "holds none" '
            'from a failed read)')
        return 2
    if bench is None:
        out('refused: benchmark ticker lookup failed')
        return 2
    bench = {str(t).strip().upper() for t in bench}
    a_state = load_alpha_state(cur)
    if a_state is None or a_state[1] is None:
        out('refused: alpha epoch not set / alpha state unreadable (no ledger to rebase)')
        return 2
    epoch_at = a_state[1]
    if not sync_fills_since_epoch(cur, epoch_at):
        out('refused: fill sync since the epoch failed or was truncated; the ledger '
            'would be incomplete')
        return 2
    inputs = load_alpha_inputs(cur, epoch_at)
    if inputs is None:
        out('refused: alpha ledger inputs unreadable')
        return 2
    lots, fills = inputs
    before = alpha_pnl(0.0, positions, bench, fills, lots)
    watch = _load_watch(cur) or {}
    markers = _late_markers(watch)
    before = _force_late(before, markers)          # R1: an open late-fill marker is a mismatch
    mism = before.get('mismatch') or {}
    adj = float(realized_adjust or 0.0)

    refusals, plan = [], []
    for k in tickers:
        sym, p = _broker_position(positions, k)
        if k in bench:
            refusals.append(f'{k}: benchmark ticker (not alpha)')
        elif _asset_class(sym or k, p) != _EQUITY_CLASS:
            refusals.append(f'{k}: not a us_equity instrument (options/crypto are out '
                            'of scope)')
        elif k not in mism:
            refusals.append(f'{k}: currently reconciles — nothing to rebase')
        elif _inside_quiet_window(fills, k, now):
            refusals.append(f'{k}: newest fill is inside the {REBASE_FILL_QUIET_S}s '
                            'quiet window — retry shortly')
        else:
            seed = rebase_seed(positions, k)
            if seed is None:
                refusals.append(f'{k}: broker position has no usable avg_entry_price')
            else:
                plan.append((k, seed))
    if adj and len(plan) != 1 and not refusals:
        refusals.append('--realized-adjust applies to exactly one ticker')
    if refusals:
        out('refused (nothing written):')
        for r in refusals:
            out(f'  - {r}')
        conn.rollback()
        return 2

    text = (reason or '').strip() or 'manual'
    if adj:
        text += f' realized-adjust={adj:+.2f}'
    rows, sim = [], list(lots)
    for k, seed in plan:
        row = rebase_row(k, seed, (before.get('realized_by_key') or {}).get(k, 0.0) + adj,
                         taken_at, f'operator:{text}', None)
        rows.append(row)
        sim.append(row)
    after = alpha_pnl(0.0, positions, bench, fills, sim)
    for k, _seed in plan:
        row = next(r for r in rows if r['ticker'] == k)
        lq, bq = mism[k]
        if k in markers:
            out(f'{k}: LATE FILL marker OPEN — fill(s) ingested after a rebase whose '
                f'realized P&L is NOT in the ledger: {markers[k]} (account for them '
                'with --realized-adjust <amount>)')
        w = watch.get(k)
        if w is not None and w[4] is None and _as_dt(w[1]) is not None:
            out(f'{k}: watch episode first seen {_as_dt(w[1]).isoformat()} '
                f'(age {(now - _as_dt(w[1])).total_seconds() / 60:.1f} min)')
        out(f'{k}: ledger qty {lq:g} | broker qty {bq:g} | would write '
            f"qty={row['qty']:g} avg={row['avg_entry_price']} side={row['side']} "
            f"realized_carry={row['realized_carry']:.2f} reason={row['reason']!r}")
    out(f"alpha P&L before {before['alpha_pnl']:.2f} | after {after['alpha_pnl']:.2f} "
        f"(recon {before['recon']} -> {after['recon']})")
    if abs(after['alpha_pnl'] - before['alpha_pnl'] - adj) > 0.005:
        out('refused: alpha P&L moved by more than the --realized-adjust across the '
            'rebase (invariant broken); nothing written')
        conn.rollback()
        return 2
    if not apply:
        out('DRY RUN — nothing written. Re-run with --apply to write the row(s).')
        conn.rollback()
        return 0
    for row in rows:
        if (not write_rebase_row(cur, row)
                or (row['ticker'] in markers and not clear_late_marker(cur, row['ticker']))):
            out(f"FAILED writing the rebase row for {row['ticker']}; rolled back, "
                'nothing written')
            conn.rollback()
            return 1
    if not _commit(conn):
        out('FAILED to commit; nothing written')
        return 1
    out(f'APPLIED {len(rows)} rebase row(s): {", ".join(r["ticker"] for r in rows)}')
    return 0


def _run_rebase_cli(tickers, reason, apply, realized_adjust=0.0) -> int:
    uri = os.environ.get('POSTGRES_URI')
    if not uri:
        print('POSTGRES_URI not set; aborting')
        return 2
    conn = None
    try:
        from execution.regime_liquidator import _load_broker_positions
        t0 = _utcnow()                       # snapshot time, BEFORE the positions load
        positions = _load_broker_positions()
        conn = psycopg2.connect(uri)
        if conn.autocommit:
            print('connection is autocommit=True; refusing')
            return 2
        cur = conn.cursor()
        return rebase_cli(cur, conn, positions, bench_tickers(cur), tickers, reason,
                          apply, positions_at=t0, realized_adjust=realized_adjust)
    except Exception as e:  # noqa: BLE001
        logger.error('[account_breaker] rebase CLI failed: %s: %s', type(e).__name__, e)
        return 1
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def main(argv=None) -> int:
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    ap = argparse.ArgumentParser(
        prog='account_breaker.py',
        description='No arguments: one breaker tick (the cron path). '
                    '--rebase: operator ledger repair, dry-run unless --apply.')
    ap.add_argument('--rebase', metavar='TICKER[,TICKER...]',
                    help='re-seed the alpha ledger of these mismatched tickers from '
                         'the broker position (no breaker tick is run)')
    ap.add_argument('--reason', help='free text recorded as reason=operator:<text>')
    ap.add_argument('--realized-adjust', type=float, default=0.0, metavar='AMOUNT',
                    help='$ added to the (single) ticker\'s realized_carry — accounts for '
                         'a LATE FILL whose realized P&L is not in the ledger; recorded '
                         'in the row reason (default 0)')
    ap.add_argument('--apply', action='store_true',
                    help='write the rebase row(s); default is a dry-run')
    args = ap.parse_args(argv)
    if args.rebase is None:
        if args.reason is not None or args.apply or args.realized_adjust:
            ap.error('--reason/--apply/--realized-adjust require --rebase')
        return run_once()
    return _run_rebase_cli(_parse_tickers(args.rebase), args.reason, args.apply,
                           args.realized_adjust)


if __name__ == '__main__':
    sys.exit(main())
