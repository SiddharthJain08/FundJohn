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
import sys
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
    Unparseable market values are skipped (logged by the caller's line)."""
    bench_tickers = set(bench_tickers or ())
    bench_mv = 0.0
    for sym, p in (positions or {}).items():
        if sym not in bench_tickers:
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
    rule=none exists. Do not reorder or rename tokens."""
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
                 f"pending={int(bool(flatten['pending']))}")
    return line
