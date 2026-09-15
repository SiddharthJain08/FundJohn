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


def evaluate(alpha: float, peak, equity: float, opening_equity) -> dict:
    """Pure rule evaluation. Returns
    {'peak', 'dd', 'daily', 'rule', 'breach'}.

    `peak` None (first ever tick) seeds from the current alpha NAV, so the
    breaker can never fire on its own first observation. `opening_equity`
    None/0 disables the daily rule for that tick (reported as daily=None)
    rather than inventing a denominator."""
    alpha = float(alpha)
    peak = alpha if peak is None else max(float(peak), alpha)
    dd = (alpha / peak - 1.0) if peak > 0 else 0.0

    daily = None
    if opening_equity not in (None, 0) and float(opening_equity) > 0:
        daily = float(equity) / float(opening_equity) - 1.0

    rules = []
    if dd <= DD_LIMIT + _EPS:
        rules.append('drawdown')
    if daily is not None and daily <= DAILY_LIMIT + _EPS:
        rules.append('daily_loss')

    return {'peak': peak, 'dd': dd, 'daily': daily,
            'rule': '+'.join(rules) if rules else 'none',
            'breach': bool(rules)}


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
               pending_flatten) -> None:
    """Persist the singleton latch. A failed write (e.g. migration 157 not
    yet applied) is logged as an ERROR and swallowed — the caller's cycle
    tick must still complete and retry the write on the next tick."""
    def _write():
        cur.execute(
            """
            UPDATE account_breaker_state
               SET halted = %s, reason = %s, breached_at = %s, peak_alpha_nav = %s,
                   dd = %s, daily = %s, pending_flatten = %s, updated_at = NOW()
             WHERE id = 1
            """,
            (bool(halted), reason, breached_at, peak, dd, daily, bool(pending_flatten)),
        )

    _savepoint_guarded(cur, 'sp_ab_save_state', _write, on_error_level=logging.ERROR)


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


def clear_halt(cur, alpha: float) -> None:
    """Operator re-arm: drop the latch and reset the rolling peak to the
    current alpha NAV, so the next drawdown is measured from here. A failed
    write is logged as an ERROR and swallowed, same as save_state."""
    def _write():
        cur.execute(
            """
            UPDATE account_breaker_state
               SET halted = FALSE, reason = NULL, breached_at = NULL,
                   peak_alpha_nav = %s, pending_flatten = FALSE,
                   rearmed_at = NOW(), updated_at = NOW()
             WHERE id = 1
            """,
            (float(alpha),),
        )

    _savepoint_guarded(cur, 'sp_ab_clear_halt', _write, on_error_level=logging.ERROR)


def format_line(mode: str, *, equity, bench_mv, alpha, st, open_equity,
                open_src, halted, flatten=None) -> str:
    """The operator greps `[account_breaker] shadow` / `[account_breaker] armed`.
    Emitted on EVERY tick — a missing line means the process died, which is why
    rule=none exists. Do not reorder or rename existing tokens; `flatten_partial`
    is appended at the END when `flatten` is given (flatten.get, not flatten[] —
    this line must never KeyError on every-tick emission)."""
    daily = 'n/a' if st.get('daily') is None else f"{st['daily']:.4f}"
    line = (f"[account_breaker] {mode} equity={float(equity):.2f} "
            f"bench_mv={float(bench_mv):.2f} alpha_nav={float(alpha):.2f} "
            f"peak={float(st['peak']):.2f} dd={float(st['dd']):.4f} "
            f"open_equity={float(open_equity):.2f} open_src={open_src} "
            f"daily={daily} rule={st['rule']} breach={int(bool(st['breach']))} "
            f"halted={int(bool(halted))}")
    if flatten is not None:
        line += (f" flatten_ok={int(flatten['ok'])} "
                 f"flatten_fail={int(flatten['fail'])} "
                 f"pending={int(bool(flatten['pending']))} "
                 f"flatten_partial={int(flatten.get('partial', 0))}")
    return line


# ── action layer (C1c) ───────────────────────────────────────────────────────
#
# Mirrors regime_blended_sizer._OCC_RE (:923) — option legs and crypto pairs are
# out of scope for the equity flatten; the option book has its own lifecycle.
_OCC_RE = re.compile(r'^[A-Z.]{1,6}\d{6}[CP]\d{8}$')


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
    symbol we check for an already-WORKING order on that symbol on the CLOSE
    side (opposite the position's own side) via
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

    After the live submit loop we sleep 0.5s (mirrors regime_liquidator's own
    settle pattern before its re-read, liquidate_on_regime_change:683) then
    take ONE regime_liquidator._load_broker_positions() re-read — never a
    per-symbol _poll_to_terminal, which belongs to the operator-triggered
    liquidator's 90s budget, not this 5-minute tick. EVERY attempted symbol
    (all of `tickers`, not just the ones that reported success) is checked
    against the re-read: still non-zero, or an unparseable qty (fail SAFE —
    treat as still open, same instinct as the working-close-order check), and
    not already bucketed `partial`, counts into `partial` (and, if it had
    been bucketed `ok`, moves out of `ok`). This is what catches a submission
    that was accepted (_close_symbol ok=True, no partial_flatten flag) but
    the order was actually rejected, expired, or only partially filled by
    the broker — `_close_symbol`'s `ok=True` means SUBMITTED, not filled.

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
        qty_by_sym_norm = {sym.strip().upper(): sym for sym in touched}
        for o in open_orders:
            if not isinstance(o, dict):
                continue
            osym_norm = str(o.get('symbol') or '').strip().upper()
            orig_sym = qty_by_sym_norm.get(osym_norm)
            if orig_sym is None:
                continue
            close_side = 'sell' if qty_by_sym[orig_sym] > 0 else 'buy'
            if str(o.get('side') or '').strip().lower() == close_side:
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
        time.sleep(0.5)
        try:
            reread = _load_broker_positions()
        except Exception as e:  # noqa: BLE001 — a failed re-read must fail SAFE (still-open)
            logger.warning('[account_breaker] post-flatten broker re-read '
                           'failed (%s: %s); treating every attempted symbol '
                           'as still open (safe default)', type(e).__name__, e)
            reread = None

        for sym in touched:
            if outcome.get(sym) == 'partial':
                continue  # already the safe bucket; the re-read adds nothing
            if reread is None:
                still_open = True
            else:
                pos = reread.get(sym)
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
