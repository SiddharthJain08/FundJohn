#!/usr/bin/env python3
"""
alpaca_executor.py — final pipeline step. Reads the sized handoff written by
regime_blended_sizer_live and submits bracket orders to Alpaca paper.

Safety gates (in order, any failure → skip):
  1. daily_signal_summary → same quality gate as the trade step.
  2. Idempotency — skip any (run_date, strategy_id, ticker) with an existing
     alpaca_order_id in position_recommendations.

Note: no per-order or aggregate notional caps. The sharpe_cadence sizer's
pure target_usd = ticker_w × (λ × NAV / Σ|ticker_w|) formulation is the
single source of deployment policy.

Session-aware order shape:
  - RTH (09:30–16:00 ET): bracket day market orders (unchanged).
  - premarket (04:00–09:30 ET) / afterhours (16:00–20:00 ET):
    simple day LIMIT orders with --extended-hours, marketable bid/ask
    offset (default 0.5%). Bracket order_class is unsupported by
    Alpaca in extended hours; stops are managed by alpaca_replace_stop.
  - closed (20:00–04:00 ET): refuse submission. Orders are logged
    and skipped so the next morning's reconcile sees the deferral.

OPG (market-on-open) was removed 2026-05-19 — the May-18 OPG paper-fill
incident demonstrated it isn't a reliable substitute for live orders.

Writes:
  - Inserts/updates position_recommendations with action='OPEN' +
    alpaca_order_id after each submission.
  - Posts a Discord summary via the tradedesk_alpaca_orders webhook if
    configured; otherwise logs.

Usage:
    python3 src/execution/alpaca_executor.py [--date YYYY-MM-DD] [--dry-run]
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution.alpaca_trader import _alpaca_session, _fetch_equity, _fetch_account_state, _price_str  # noqa: E402
from execution.handoff import read_handoff, HANDOFF_DIR  # noqa: E402
# SP-6 Task 10: OPG dual-path for is_dropped close_only orders. Reuse the
# poll-to-terminal loop + terminal-status set from regime_liquidator so a
# re-introduced OPG (market-on-open) order is NEVER treated as filled on ack —
# the 2026-05-18 paper-fill incident (214/224 OPG closes ack'd-but-expired while
# the DB wrongly said "closed"). Imported into THIS module's namespace so tests
# patch `execution.alpaca_executor._poll_to_terminal` (and the underlying CLI is
# regime_liquidator._run_cli, which the import binding keeps decoupled from this
# module's _run_alpaca_cli).
from execution.regime_liquidator import _poll_to_terminal, _TERMINAL_ORDER_STATUSES  # noqa: E402


# No executor-side clamps. The sizer's pure
# target_usd = ticker_w × (λ × NAV / Σ|ticker_w|) formulation governs
# deployment in full.

# Per-run in-memory cache of tickers Alpaca paper rejected as
# untradable/unsupported (delisted, halted, asset-not-found, etc.). Keyed
# by ticker; populated on the first 4xx with an asset-class signature so
# subsequent attempts in the same cycle skip cleanly without wasting an
# API roundtrip. Cleared on every main() invocation; assets get
# relisted/unhalted between days, so persisting wouldn't be safe.
_unsupported_assets: set[str] = set()

# Per-run cache of Alpaca asset metadata keyed by symbol. Populated lazily
# by _get_asset_meta() on the first eligibility check; reused for the
# remainder of the cycle. Cleared on every main() invocation — asset
# status (tradable/active) can flip between cycles.
_asset_cache: dict[str, dict] = {}

# Per-run cache of current broker positions {symbol: signed_qty}. Populated
# once per main() invocation via `alpaca position list`. Used to detect
# close/reverse orders (e.g. buy on existing short, sell on existing long)
# so we can drop bracket→simple, since Alpaca rejects "bracket orders must
# be entry orders" on buys-to-close. Empty dict on fetch failure means
# "treat all orders as opens" — degrades gracefully to existing behavior.
_broker_positions_cache: dict[str, float] = {}
_broker_positions_loaded: bool = False

# Alpaca CLI binary. Override with ALPACA_CLI_BIN env var if installed elsewhere.
ALPACA_CLI = os.environ.get('ALPACA_CLI_BIN', '/root/go/bin/alpaca')


def _load_broker_positions() -> None:
    """One-shot loader for the broker-positions cache. Called from main()."""
    global _broker_positions_loaded
    _broker_positions_cache.clear()
    try:
        proc = subprocess.run(
            [ALPACA_CLI, 'position', 'list'],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if proc.returncode == 0 and proc.stdout:
            for p in json.loads(proc.stdout) or []:
                sym = p.get('symbol')
                if not sym:
                    continue
                qty = float(p.get('qty') or 0)
                # Alpaca returns qty as signed (negative for short). Be defensive.
                if p.get('side') == 'short' and qty > 0:
                    qty = -qty
                _broker_positions_cache[sym] = qty
            log(f'broker positions loaded: {len(_broker_positions_cache)} symbols')
        else:
            log(f'broker positions fetch rc={proc.returncode}: {proc.stderr[:200]}')
    except Exception as exc:
        log(f'broker positions fetch failed ({type(exc).__name__}: {exc})')
    _broker_positions_loaded = True


def _current_broker_qty(ticker: str) -> float:
    """Return signed current broker qty for ticker (0.0 if unknown/no position)."""
    return float(_broker_positions_cache.get(ticker, 0.0))


def _would_close_or_reverse(side: str, ticker: str) -> bool:
    """True if a new order on this side would close or reverse an existing
    position in the opposite direction. Alpaca rejects bracket orders on
    these because the entry leg isn't an opening fill."""
    cur = _current_broker_qty(ticker)
    if cur == 0.0:
        return False
    if side == 'buy' and cur < 0:
        return True   # buy-to-close (or buy-to-reverse) an existing short
    if side == 'sell' and cur > 0:
        return True   # sell-to-close (or sell-to-reverse) an existing long
    return False


# ── Helpers ─────────────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f'{ts} [ALPACA_EXEC] {msg}')


_market_hours_cache = {'is_open': None, 'cached_at': 0.0}
_session_kind_cache = {'session': None, 'cached_at': 0.0}


def _alpaca_session_kind() -> str:
    """Classify the current market session for execution-policy purposes.

    Returns one of:
      - 'rth'        — regular trading hours, 09:30–16:00 ET (is_open=True)
      - 'premarket'  — 04:00–09:30 ET on a trading day (is_open=False)
      - 'afterhours' — 16:00–20:00 ET on a trading day (is_open=False)
      - 'closed'     — fully closed: weekends, holidays, or 20:00–04:00 ET
                       overnight gap on a trading day

    Derived from the existing `alpaca clock` payload. When is_open=True we
    short-circuit to 'rth'. Otherwise we project the *current* moment onto
    America/New_York and compare against the pre/post boundaries — preferring
    `next_open` / `next_close` from the clock payload to determine whether
    today is a trading day, falling back to weekday math + ET-window check
    if the payload lacks those fields. Cached for 60s alongside the in-hours
    cache to amortize CLI shell-out cost.

    Pre/post boundaries (ET):
      pre_open  = 04:00   # ECN pre-market opens
      rth_open  = 09:30
      rth_close = 16:00
      post_end  = 20:00   # ECN post-market closes
    """
    import time as _time
    now = _time.time()
    if (_session_kind_cache['session'] is not None and
            now - _session_kind_cache['cached_at'] < 60.0):
        return _session_kind_cache['session']

    payload: dict | None = None
    try:
        proc = subprocess.run(
            [ALPACA_CLI, 'clock'],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if proc.returncode == 0 and proc.stdout:
            payload = json.loads(proc.stdout)
        else:
            log(f'alpaca clock returned rc={proc.returncode} — using static ET window for session classification')
    except Exception as exc:
        log(f'alpaca clock failed ({type(exc).__name__}: {exc}) — using static ET window for session classification')

    # Compute current ET wall-clock time.
    try:
        from zoneinfo import ZoneInfo
        now_et = datetime.now(ZoneInfo('America/New_York'))
    except Exception:
        # Best-effort fallback: assume UTC offset = -4h (EDT). Off by one hour
        # during EST, but the boundaries are coarse enough this won't reclassify
        # in production. Avoid hard-coding here for the failure to hide.
        now_et = datetime.now(timezone.utc).astimezone()

    pre_open  = time(4, 0)
    rth_open  = time(9, 30)
    rth_close = time(16, 0)
    post_end  = time(20, 0)
    cur_t = now_et.time()

    # 1. Live `is_open=True` → trivially RTH. Use the clock as the source of
    #    truth (handles half-days, holidays Alpaca knows about, etc.).
    if payload is not None and payload.get('is_open'):
        session = 'rth'
    elif payload is not None:
        # 2. Clock says closed. Are we on a trading day at all?
        #    We use next_open/next_close to triangulate. If next_open is later
        #    today, we're in pre-market (and today is a trading day). If
        #    next_close is later today, we're in RTH (shouldn't hit — is_open
        #    would have been True). If next_open is on a *later* date, we're
        #    in afterhours of a trading day OR overnight of a non-trading day.
        from datetime import datetime as _dt
        def _parse_iso(s):
            if not s:
                return None
            try:
                # Alpaca returns ISO 8601 with tz; convert to ET.
                # Python's fromisoformat handles +HH:MM offsets in 3.11+.
                _t = _dt.fromisoformat(s.replace('Z', '+00:00'))
                try:
                    from zoneinfo import ZoneInfo
                    return _t.astimezone(ZoneInfo('America/New_York'))
                except Exception:
                    return _t
            except (ValueError, TypeError):
                return None

        next_open_et  = _parse_iso(payload.get('next_open'))
        # If next_open is today (same ET date) → we're pre-market today.
        # If next_open is tomorrow (or later) AND we're within RTH window
        # somehow, would be RTH (already handled). Otherwise we're after-hours
        # if cur_t is in the post window AND today is a trading day.
        # Heuristic: if next_open exists and falls on a future calendar day,
        # then "today" already had its open — so we're post-RTH (afterhours
        # if before 20:00 ET, else closed). If next_open is today, we're
        # pre-open (premarket if after 04:00 ET, else closed/overnight).
        if next_open_et is not None and next_open_et.date() == now_et.date():
            # Today's open is still in the future → we're before today's open.
            if pre_open <= cur_t < rth_open:
                session = 'premarket'
            else:
                session = 'closed'
        elif next_open_et is not None and next_open_et.date() > now_et.date():
            # Today's open is past (or today is not a trading day).
            if rth_close <= cur_t < post_end:
                session = 'afterhours'
            elif pre_open <= cur_t < rth_open:
                # Weekend morning where next_open is the following Monday:
                # technically "pre-market" of a non-trading day. ECNs are
                # closed on weekends, so classify as closed to be safe.
                if now_et.weekday() >= 5:
                    session = 'closed'
                else:
                    session = 'premarket'
            else:
                session = 'closed'
        else:
            # next_open missing/unparseable — fall through to static logic.
            session = _static_session(now_et, pre_open, rth_open, rth_close, post_end)
    else:
        # 3. Clock payload entirely unavailable — pure static ET-window check.
        session = _static_session(now_et, pre_open, rth_open, rth_close, post_end)

    _session_kind_cache.update({'session': session, 'cached_at': now})
    return session


def _static_session(now_et, pre_open, rth_open, rth_close, post_end) -> str:
    """Static-fallback session classifier from ET wall clock alone. Used when
    the alpaca clock CLI is unavailable. Treats Sat/Sun as 'closed' across
    all hours (ignores holiday-specific closures — best-effort only)."""
    if now_et.weekday() >= 5:
        return 'closed'
    cur_t = now_et.time()
    if rth_open <= cur_t < rth_close:
        return 'rth'
    if pre_open <= cur_t < rth_open:
        return 'premarket'
    if rth_close <= cur_t < post_end:
        return 'afterhours'
    return 'closed'


def in_market_hours():
    """Return True if regular trading hours per `alpaca clock`.

    Thin wrapper around _alpaca_session_kind() preserved for backward
    compatibility with existing callers + tests. Returns True iff
    _alpaca_session_kind() == 'rth'. Caches via the session-kind cache.

    Falls back to a static 09:30–16:00 ET weekday window if the CLI is
    unavailable (e.g. credentials missing in tests).
    """
    import time as _time
    now = _time.time()
    if (_market_hours_cache['is_open'] is not None and
            now - _market_hours_cache['cached_at'] < 60.0):
        return _market_hours_cache['is_open']
    is_open = _alpaca_session_kind() == 'rth'
    _market_hours_cache.update({'is_open': is_open, 'cached_at': now})
    return is_open


def check_signal_quality(conn):
    cur = conn.cursor()
    cur.execute("""
        SELECT avg_ev, ev_pos, n_signals, run_date
        FROM daily_signal_summary
        ORDER BY run_date DESC, created_at DESC
        LIMIT 1
    """)
    row = cur.fetchone()
    cur.close()
    if not row:
        return True, 'no signal_summary row — allowing'
    avg_ev_frac, ev_pos, n_signals, row_date = row
    avg_ev_pct = float(avg_ev_frac) * 100 if avg_ev_frac is not None else 0.0
    green_cnt  = int(ev_pos or 0)
    # Mirror pipeline_orchestrator thresholds: block only if avg_ev < -0.5% AND zero green.
    if avg_ev_pct < -0.5 and green_cnt < 1:
        return False, f'avg EV={avg_ev_pct:+.2f}% ({green_cnt} green, {row_date}) — quality gate blocked'
    return True, f'avg EV={avg_ev_pct:+.2f}%, green={green_cnt}, {n_signals} signals'


def already_executed(conn, run_date, strategy_id, ticker):
    cur = conn.cursor()
    cur.execute("""
        SELECT 1 FROM alpaca_submissions
        WHERE run_date = %s AND strategy_id = %s AND ticker = %s
          AND alpaca_order_id IS NOT NULL
        LIMIT 1
    """, (run_date, strategy_id, ticker))
    hit = cur.fetchone() is not None
    cur.close()
    return hit


def _placed_or_order_stop(order, resp) -> float:
    """Prefer the stop actually submitted (in the execute result) over the
    pre-submit order's stop, when W1 is ON. The result's `stop` is the
    post-recompute/stacked leg sent to the broker."""
    if _record_placed_bracket_on():
        s = (resp or {}).get('stop')
        if s is not None:
            return float(s)
    return float(order.get('stop') or 0.0)


def _placed_or_order_target(order, resp) -> float:
    """Prefer the take-profit actually submitted over the pre-submit order's.
    Logs a WARN if the chosen target is degenerate (<= entry for a long /
    >= entry for a short) so a bad placement is never recorded silently.
    The degenerate check only runs when the gate is ON (comparing placed
    result vs placed entry is apples-to-apples); gate-OFF compares pre-submit
    order levels against executed-result entry — mismatched sources that would
    produce spurious warnings."""
    if _record_placed_bracket_on():
        t = (resp or {}).get('target')
        tgt = float(t) if t is not None else float(order.get('t1') or order.get('target') or 0.0)
        entry = float((resp or {}).get('entry') or order.get('entry') or 0.0)
        side = (order.get('direction') or 'long').lower()
        if entry > 0 and tgt > 0:
            degenerate = (tgt <= entry) if side != 'short' else (tgt >= entry)
            if degenerate:
                log(f"  ⚠ {order.get('ticker','?')}: recording DEGENERATE target "
                    f"{tgt:.2f} vs entry {entry:.2f} (side={side}) — placement bug upstream")
        return tgt
    return float(order.get('t1') or order.get('target') or 0.0)


def record_submission(conn, run_date, order, alpaca_resp, tif, order_class, coid):
    """Persist a row in alpaca_submissions tying the sized order to its
    Alpaca order_id. UNIQUE (run_date, strategy_id, ticker) makes re-runs
    idempotent."""
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO alpaca_submissions (
          run_date, ticker, strategy_id, direction, qty,
          entry_price, stop_price, target_price, pct_nav, notional_usd,
          time_in_force, order_class, client_order_id,
          alpaca_order_id, alpaca_status, alpaca_http, alpaca_error,
          instrument_class,
          submitted_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
        ON CONFLICT (run_date, strategy_id, ticker) DO UPDATE SET
          alpaca_order_id = COALESCE(EXCLUDED.alpaca_order_id, alpaca_submissions.alpaca_order_id),
          alpaca_status   = EXCLUDED.alpaca_status,
          alpaca_http     = EXCLUDED.alpaca_http,
          alpaca_error    = EXCLUDED.alpaca_error,
          submitted_at    = EXCLUDED.submitted_at,
          -- 2026-05-22 followup to the bracket-on-cover incident: also
          -- refresh the order-shape columns so retry attempts that
          -- downgrade bracket→simple (or change qty/price) leave an
          -- accurate audit trail instead of the original-attempt values.
          order_class     = EXCLUDED.order_class,
          time_in_force   = EXCLUDED.time_in_force,
          qty             = EXCLUDED.qty,
          entry_price     = EXCLUDED.entry_price,
          stop_price      = EXCLUDED.stop_price,
          target_price    = EXCLUDED.target_price,
          notional_usd    = EXCLUDED.notional_usd,
          pct_nav         = EXCLUDED.pct_nav,
          -- SP-5.1a (mig 122): refresh instrument_class on retry so a
          -- downgrade or re-tag leaves an accurate audit trail; preserve
          -- prior non-NULL value if the retry omits it (legacy equity rows
          -- pass NULL — keep them NULL via COALESCE on EXCLUDED first).
          instrument_class = COALESCE(EXCLUDED.instrument_class, alpaca_submissions.instrument_class)
    """, (
        run_date, order['ticker'], order.get('strategy_id') or 'unknown',
        (order.get('direction') or 'long').lower(),
        alpaca_resp.get('qty') or 0,
        alpaca_resp.get('entry') or order.get('entry') or 0.0,
        _placed_or_order_stop(order, alpaca_resp),
        _placed_or_order_target(order, alpaca_resp),
        order.get('pct_nav'),
        alpaca_resp.get('notional'),
        tif, order_class, coid,
        alpaca_resp.get('order_id'),
        alpaca_resp.get('status'),
        alpaca_resp.get('http'),
        alpaca_resp.get('body') or alpaca_resp.get('reason'),
        order.get('instrument_class'),
    ))
    conn.commit()
    cur.close()


def _record_dtbp_skip(conn, run_date, order, equity: float) -> None:
    """Persist a skipped-for-buying-power order as an audit row
    (alpaca_status='skipped_dtbp', no order id) via record_submission."""
    notional = round(float(equity) * float(order.get('pct_nav') or 0.0), 2)
    resp = {
        'status':   'skipped_dtbp',
        'qty':      0,
        'notional': notional,
        'order_id': None,
        'http':     None,
        'reason':   'dtbp_budget_exhausted',
        'entry':    order.get('entry'),
    }
    # Audit rows need UNIQUE client_order_ids: on 2026-06-04 (first live fill
    # day) two dtbp skips both wrote coid='' → unique-key violation → the
    # alpaca step aborted with ~30 of 48 sized orders never attempted.
    import uuid as _uuid
    audit_coid = f"skip-dtbp-{run_date}-{order.get('ticker', '?')}-{_uuid.uuid4().hex[:8]}"
    record_submission(
        conn, run_date, order, resp,
        order.get('tif') or 'day',
        order.get('order_class') or 'simple',
        audit_coid,
    )


def _normalize_alpaca_symbol(raw: str) -> str | None:
    """Map engine/handoff ticker → Alpaca-accepted symbol.
    Returns None for symbols Alpaca paper doesn't support (futures, indices,
    FX pairs, crypto not on Alpaca's list) — caller must skip those."""
    if not raw:
        return None
    t = raw.strip().upper()
    # Yahoo class-share convention uses a dash; Alpaca wants a dot.
    if '.' not in t and '-' in t and t.count('-') == 1 and len(t.split('-')[1]) <= 2 and not t.endswith('-USD'):
        t = t.replace('-', '.')
    # Known non-equity symbols Alpaca paper rejects — skip cleanly.
    if t.startswith('^'):                       return None   # indices (^VIX, ^GSPC, ^DJI, …)
    if '=' in t:                                return None   # futures/FX (NG=F, GC=F, AUDUSD=X, …)
    if t.endswith('-USD'):                      return None   # crypto tickers not in Alpaca universe
    # Exchange-suffixed futures/indices: TICKER.NYB, .CME, .CBT, .NYM, .COMEX, etc.
    # Class-share tickers use a single-letter suffix (BRK.A, BRK.B, BF.A) —
    # only block ≥3-letter alpha suffixes.
    if '.' in t:
        suffix = t.rsplit('.', 1)[-1]
        if suffix.isalpha() and len(suffix) >= 3:
            return None
    return t


def _build_occ_symbol(spec, strike: float, expiry) -> str:
    """OCC option symbol: <root><YYMMDD><C|P><strike*1000, 8-digit zero-pad>.
    e.g. SPY 2026-06-18 call 750 -> 'SPY260618C00750000'."""
    if strike <= 0:
        raise ValueError(f"strike must be > 0; got {strike}")
    root = spec.underlying.upper()
    ymd = expiry.strftime('%y%m%d')
    cp = 'C' if str(spec.right).lower() == 'call' else 'P'
    strike_int = int(round(strike * 1000))
    return f"{root}{ymd}{cp}{strike_int:08d}"


def _spot_price(symbol: str) -> float | None:
    """Latest equity spot via Alpaca CLI."""
    ok, payload, _ = _run_alpaca_cli(['data','latest-trade','--symbol', symbol])
    if not ok or not payload: return None
    try:
        return float(payload.get('trade',{}).get('p') or payload.get('p'))
    except (TypeError, ValueError): return None


def _list_strikes(underlying: str, expiry, option_type: str) -> list[float]:
    """Listed strikes for (underlying, expiry, call|put) via `alpaca option contracts`."""
    ok, payload, _ = _run_alpaca_cli([
        'option','contracts','--underlying-symbols', underlying,
        '--expiration-date', expiry.isoformat(),
        '--type', option_type,
        '--limit','200'])
    if not ok or not payload: return []
    rows = payload.get('option_contracts') or []
    out = []
    for r in rows:
        try: out.append(float(r.get('strike_price')))
        except (TypeError, ValueError): continue
    return sorted(set(out))


def _list_expiries(underlying: str, gte: str | None = None) -> list:
    """Distinct listed expiry dates for an underlying (monthly+weekly).

    `gte` (ISO date) narrows the API page server-side via --expiration-date-gte.
    Without it, SPY's first 500 rows are all same-day expiries (~500 strikes
    × 1 expiry); the eligible set then comes back empty for any dte_target>0.
    Regression caught by SP-5.1a smoke 2026-05-29 13:35 UTC."""
    args = ['option','contracts','--underlying-symbols', underlying, '--limit','500']
    if gte:
        args.extend(['--expiration-date-gte', gte])
    ok, payload, _ = _run_alpaca_cli(args)
    if not ok or not payload: return []
    rows = payload.get('option_contracts') or []
    out = set()
    import datetime as _dt
    for r in rows:
        try: out.add(_dt.date.fromisoformat(r.get('expiration_date')))
        except (TypeError, ValueError): continue
    return sorted(out)


def _resolve_strike(spec, as_of, expiry) -> float | None:
    spot = _spot_price(spec.underlying)
    if spot is None or spot <= 0: return None
    listed = _list_strikes(spec.underlying, expiry, str(spec.right).lower())
    if not listed: return None
    if spec.strike_rule == 'atm':
        target = spot
    elif spec.strike_rule == 'fixed_moneyness':
        target = spot * float(spec.moneyness or 1.0)
    elif spec.strike_rule == 'target_delta':
        # Heuristic fallback: for calls, OTM = strike > spot; pick listed
        # whose distance from spot is closest to (1-target_delta)*spot.
        # NOTE: a future iteration may query the live chain greeks for exact
        # delta-matching; this heuristic is documented in the spec as the
        # SP-5.1a baseline.
        offset = (1.0 - float(spec.target_delta)) * spot * 0.25  # rough OTM step
        target = (spot + offset) if str(spec.right).lower() == 'call' else (spot - offset)
    else:
        return None
    return min(listed, key=lambda k: abs(k - target))


def _resolve_expiry(spec, as_of):
    """Nearest monthly expiry >= as_of + dte_target days, intersected with listed."""
    import datetime as _dt
    target = as_of + _dt.timedelta(days=int(spec.dte_target))
    listed = _list_expiries(spec.underlying, gte=target.isoformat())
    eligible = [e for e in listed if e >= target]
    return eligible[0] if eligible else None


def _options_position_intent(direction: str, current_qty: float) -> tuple[str, str]:
    """Resolves (side, position_intent) from desired direction and existing position.
    Prevents the 422 'inferred sell_to_close vs specified sell_to_open' class of error
    surfaced by SP-5.0 test-5."""
    d = str(direction).lower()
    if d == 'long':
        return ('buy', 'buy_to_open') if current_qty >= 0 else ('buy', 'buy_to_close')
    if d == 'short':
        return ('sell', 'sell_to_open') if current_qty <= 0 else ('sell', 'sell_to_close')
    raise ValueError(f"unknown direction: {direction!r}")


def _options_current_qty(occ_symbol: str) -> float:
    """Lookup existing option position qty by OCC symbol. Returns 0.0 if none."""
    ok, payload, _ = _run_alpaca_cli(['position','list'])
    if not ok or not payload: return 0.0
    for p in payload:
        if str(p.get('symbol')) == occ_symbol:
            try: return float(p.get('qty', 0))
            except (TypeError, ValueError): return 0.0
    return 0.0


def _cash_collateral_required(right: str, side: str, strike: float, qty: float) -> float:
    """Cash collateral for the cash-secured-put L1 path. Zero for everything else."""
    if str(right).lower() == 'put' and str(side).lower() == 'sell':
        return float(strike) * 100.0 * float(qty)
    return 0.0


def _apply_cash_collateral_guard(right, side, strike, qty, account_cash):
    """Returns (final_qty, skip_reason|None). Reduces qty to fit cash; SKIPs at 0."""
    required = _cash_collateral_required(right, side, strike, qty)
    if required == 0.0:
        return int(qty), None
    if required <= account_cash:
        return int(qty), None
    fit = int(account_cash // (float(strike) * 100.0))
    if fit < 1:
        return 0, f'option: insufficient cash for short put (need {required:.0f}, have {account_cash:.0f})'
    return fit, f'short-put qty reduced {int(qty)}->{fit} for cash budget'


def _account_cash() -> float | None:
    """Live account.cash from Alpaca CLI."""
    ok, payload, _ = _run_alpaca_cli(['account','get'])
    if not ok or not payload: return None
    try: return float(payload.get('cash', 0))
    except (TypeError, ValueError): return None


import os as _os

def _option_min_notional_usd() -> float:
    raw = _os.environ.get('OPENCLAW_OPTION_MIN_NOTIONAL_USD', '100')
    try: return float(raw)
    except ValueError: return 100.0


def _resolve_option_qty(order: dict, limit_price: float) -> tuple[int, str | None]:
    """Resolves integer contract qty with sub-contract refuse-or-floor.
    Returns (qty, skip_reason|None)."""
    contracts = order.get('contracts')
    if contracts is None:
        notional = float(order.get('notional_usd', 0))
        if limit_price <= 0:
            return 0, 'option: non-positive limit_price; cannot derive contracts'
        contracts = notional / (limit_price * 100.0)
    contracts = float(contracts)
    if contracts <= 0:
        return 0, 'option: zero contracts sized'
    if contracts >= 1:
        return int(contracts), None
    notional = float(order.get('notional_usd', 0))
    if notional >= _option_min_notional_usd():
        return 1, None
    return 0, f'option: sub-contract sizing below minimum (notional ${notional:.0f})'


def _options_session_gate() -> tuple[bool, str | None]:
    """Options orders are RTH-only (per SP-5.0 grounding §4b).
    Returns (allowed, skip_reason|None)."""
    kind = _alpaca_session_kind()
    if kind == 'rth':
        return True, None
    if kind == 'closed':
        return False, 'option: market closed'
    return False, f'option: RTH-only (current session={kind})'


def _option_quote(occ_symbol: str) -> dict | None:
    """Best bid/ask for an OCC symbol via the exact-symbol latest-quotes endpoint.

    Regression (SP-5.1a smoke 2026-06-01): the prior `data option chain
    --underlying-symbol <U>` path defaults to `--limit 100`, and SPY has
    hundreds of strikes per expiry, so the resolved OCC was almost always
    truncated out of the snapshot page (true even when filtered by
    expiry+type) → lookup miss → None → fail-closed 'no quote for limit price'.
    `data option latest-quotes --symbols <OCC>` returns the one contract we
    asked for with no pagination. Response shape: {"quotes": {<OCC>: {bp,ap}}}."""
    ok, payload, _ = _run_alpaca_cli([
        'data','option','latest-quotes','--symbols', occ_symbol])
    if not ok or not payload: return None
    q = (payload.get('quotes') or {}).get(occ_symbol) or {}
    try:
        bid = float(q.get('bp') or q.get('bid') or 0)
        ask = float(q.get('ap') or q.get('ask') or 0)
        if bid > 0 and ask > 0:
            return {'bid': bid, 'ask': ask}
    except (TypeError, ValueError): pass
    return None


def _occ_strike(occ_symbol: str) -> float | None:
    """Strike from an OCC symbol: last 8 digits / 1000 (e.g. ...00760000 -> 760.0)."""
    try:
        return int(occ_symbol[-8:]) / 1000.0
    except ValueError:
        return None


def _option_chain_greeks(
    underlying: str, expiry, right: str, spot: float, band_pct: float = 0.15,
) -> list[tuple[float, float, str]]:
    """[(strike, delta, occ)] for `underlying` at `expiry`/`right`, over a strike
    band of spot*(1±band_pct). Reads per-contract greeks.delta from the chain
    snapshot (verified live in the SP-5.1b grounding spike). Bounded by
    --strike-price-gte/lte so the 100-row page covers the band; paginates via
    --page-token if a full page returns."""
    lo, hi = spot * (1.0 - band_pct), spot * (1.0 + band_pct)
    args = ['data','option','chain','--underlying-symbol', underlying,
            '--expiration-date', expiry.isoformat() if isinstance(expiry, date) else str(expiry),
            '--type', str(right).lower(),
            '--strike-price-gte', f'{lo:.2f}', '--strike-price-lte', f'{hi:.2f}']
    out, token = [], None
    for _ in range(6):  # page cap
        a = args + (['--page-token', token] if token else [])
        ok, payload, _ = _run_alpaca_cli(a)
        if not ok or not payload:
            break
        for occ, snap in (payload.get('snapshots') or {}).items():
            d = ((snap or {}).get('greeks') or {}).get('delta')
            k = _occ_strike(occ)
            if d is not None and k is not None:
                try:
                    out.append((k, float(d), occ))
                except (TypeError, ValueError):
                    pass
        token = payload.get('next_page_token')
        if not token:
            break
    return out


def _with(spec, **overrides):
    """Shallow clone of an OptionSpec-like object with field overrides (for reusing
    _resolve_strike's atm path per leg without mutating the caller's spec)."""
    import copy
    s = copy.copy(spec)
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _resolve_structure_legs(spec, as_of, expiry):
    """Return [(right, strike, side)] for straddle/strangle/vertical/credit_vertical/iron_condor,
    resolved against LIVE listed strikes/greeks. side ∈ {'buy','sell'}. Fail-closed -> None."""
    spot = _spot_price(spec.underlying)
    if spot is None or spot <= 0:
        return None
    if spec.structure == 'straddle':
        k = _resolve_strike(_with(spec, strike_rule='atm', right='call'), as_of, expiry)
        if k is None:
            return None
        return [('call', k, 'buy'), ('put', k, 'buy')]
    if spec.structure == 'strangle':
        tgt = float(spec.target_delta)
        legs = []
        for right in ('call', 'put'):
            grk = _option_chain_greeks(spec.underlying, expiry, right, spot)
            if not grk:
                return None
            k = min(grk, key=lambda sdo: abs(abs(sdo[1]) - tgt))[0]
            legs.append((right, k, 'buy'))
        return legs
    if spec.structure == 'vertical':
        right = str(spec.right).lower()
        near = _resolve_strike(_with(spec, right=right), as_of, expiry)
        if near is None:
            return None
        grk = _option_chain_greeks(spec.underlying, expiry, right, spot)
        if not grk:
            return None
        if float(spec.spread_width_pct) <= 0:
            return None   # non-positive width can't define a distinct OTM short leg (fail-closed)
        sign = 1.0 if right == 'call' else -1.0
        far_target = near + sign * float(spec.spread_width_pct) * spot
        far = min((s for s, _d, _o in grk), key=lambda s: abs(s - far_target))
        if far == near:
            return None
        return [(right, near, 'buy'), (right, far, 'sell')]
    if spec.structure == 'credit_vertical':
        right = str(spec.right).lower()
        if float(spec.spread_width_pct) <= 0:
            return None
        grk = _option_chain_greeks(spec.underlying, expiry, right, spot)
        if not grk:
            return None
        if spec.strike_rule == 'target_delta':
            near = min(grk, key=lambda sdo: abs(abs(sdo[1]) - float(spec.target_delta)))[0]
        else:
            near = _resolve_strike(_with(spec, right=right), as_of, expiry)
        if near is None:
            return None
        sign = 1.0 if right == 'call' else -1.0
        far_target = near + sign * float(spec.spread_width_pct) * spot
        far = min((s for s, _d, _o in grk), key=lambda s: abs(s - far_target))
        if right == 'call' and not far > near:
            return None
        if right == 'put' and not far < near:
            return None
        return [(right, near, 'sell'), (right, far, 'buy')]
    if spec.structure == 'iron_condor':
        if float(spec.spread_width_pct) <= 0:
            return None
        tgt = float(spec.target_delta)
        width = float(spec.spread_width_pct) * spot
        legs, bounds = [], {}
        for right, sign in (('call', 1.0), ('put', -1.0)):
            grk = _option_chain_greeks(spec.underlying, expiry, right, spot)
            if not grk:
                return None
            near = min(grk, key=lambda sdo: abs(abs(sdo[1]) - tgt))[0]
            far_target = near + sign * width
            far = min((s for s, _d, _o in grk), key=lambda s: abs(s - far_target))
            if far == near:
                return None
            legs += [(right, near, 'sell'), (right, far, 'buy')]
            bounds[right] = (near, far)
        nearC, farC = bounds['call']; nearP, farP = bounds['put']
        if not (farP < nearP < spot < nearC < farC):
            return None   # degenerate/crossed strikes -> fail-closed
        return legs
    return None


def _structure_net_quote(spec, legs, expiry):
    """For each (right, strike, side) leg: build OCC + fetch quote. Returns
    (net_debit, [(occ, right, side)]) or None if any leg has no quote.
    net = Σ(buy ask) − Σ(sell bid)."""
    leg_q = []
    net = 0.0
    for right, strike, side in legs:
        occ = _build_occ_symbol(_with(spec, right=right), strike, expiry)
        q = _option_quote(occ)
        if q is None:
            return None
        if side == 'buy':
            net += float(q['ask'])
        else:
            net -= float(q['bid'])
        leg_q.append((occ, right, side))
    return net, leg_q


def _build_mleg_legs_json(leg_q) -> str:
    """JSON `--legs` array. Per leg, side+intent via _options_position_intent on the
    leg's intended side ('buy'->long, 'sell'->short) and its existing qty. ratio_qty='1'."""
    import json
    out = []
    for occ, _right, side in leg_q:
        d = 'long' if side == 'buy' else 'short'
        s, intent = _options_position_intent(d, _options_current_qty(occ))
        out.append({'symbol': occ, 'side': s, 'ratio_qty': '1', 'position_intent': intent})
    return json.dumps(out)


def _option_marketable_limit(side: str, quote: dict, slip: float = 0.02) -> float:
    """For buys: ask + slip (immediate fill bounded by slip). For sells: bid - slip."""
    if str(side).lower() == 'buy':
        return round(float(quote['ask']) + slip, 2)
    return round(max(0.01, float(quote['bid']) - slip), 2)


def _route_option_order(order: dict, equity: float, coid: str) -> dict | None:
    """SP-5.1a/b — options exec lane. Single-leg (calls/puts, cash-secured short put)
    + multi-leg long straddle/strangle (--order-class mleg). Returns a result dict or
    None to fall through (equity byte-identical when None)."""
    import os as _os, datetime as _dt
    # Check ordering matters (opus review NIT 1): classify the order FIRST, gate
    # SECOND. A genuine equity order (no instrument_class) returns None and falls
    # through to the equity path. An OPTION order with the gate OFF returns a
    # SKIP dict — fail-closed by construction, never accidentally routed as
    # equity shares of the underlying (the old order let gate-off option orders
    # fall through, saved only by the equity path's missing-levels SKIP).
    if order.get('instrument_class') != 'option':
        return None
    if _os.environ.get('OPENCLAW_OPTION_EXEC') != '1':
        return _option_skip(order, coid, equity,
            'option: OPENCLAW_OPTION_EXEC gate is OFF')
    spec = order.get('option_spec')
    if spec is None:
        return _option_skip(order, coid, equity,
            'option: instrument_class=option but no option_spec (malformed order)')
    # 5.1b: single + straddle/strangle supported. 5.2: debit verticals + credit_vertical/iron_condor added.
    # SP-5 Phase 1b (G4a): the OPEN-envelope structure check is exempt for close_only —
    # a close reads HELD legs and is structure-agnostic, so the synthesized 'held_legs'
    # close (G4b orphan-close) must NOT be refused here. A non-close 'held_legs' (which
    # never happens) would still be refused (an open needs a known structure to resolve).
    if (not order.get('close_only')
            and getattr(spec, 'structure', 'single') not in
            ('single', 'straddle', 'strangle', 'vertical', 'credit_vertical', 'iron_condor')):
        return _option_skip(order, coid, equity,
            f"option: structure={spec.structure!r} not in SP-5.1b (single/straddle/strangle)")
    # RTH gate
    ok_session, sess_reason = _options_session_gate()
    if not ok_session:
        return _option_skip(order, coid, equity, sess_reason)

    # Structure CLOSE dispatch — hoisted above expiry/short-call resolution: a close
    # reads HELD legs from the book and must NOT be blocked by open-path resolution
    # (e.g. a chain hiccup making expiry unresolvable would otherwise strand the position).
    # SP-5 Phase 1b (G4a): generalized from the 5 known structures to ANY non-'single'
    # structure (incl. the synthesized 'held_legs') — _route_mleg_close is held-legs-driven
    # (reads `position list` filtered by underlying, closes per-leg; structure-agnostic).
    # The 5 known structures take the SAME branch (byte-identical); 'single' keeps its
    # existing proven _route_option_close path below.
    if order.get('close_only') and getattr(spec, 'structure', 'single') != 'single':
        return _route_mleg_close(order, spec, coid)

    direction = str(order.get('direction','long')).lower()
    right = str(spec.right).lower()
    side = 'buy' if direction == 'long' else 'sell'

    # Short calls REFUSED for single-leg (covered-call lookup out of scope).
    # Structures fall through to the mleg envelope guard below (which refuses
    # non-long / hedged structures with a clearer message).
    if spec.structure == 'single' and right == 'call' and side == 'sell':
        return _option_skip(order, coid, equity,
            'option: short call requires covered-call lookup (not in SP-5.1a)')

    today = _dt.date.today()
    expiry = _resolve_expiry(spec, today)
    if expiry is None:
        return _option_skip(order, coid, equity, 'option: expiry unresolved')

    if spec.structure in ('straddle', 'strangle', 'vertical', 'credit_vertical', 'iron_condor'):
        # SP-5.1b-i envelope: long debit only, hedge='none' only. (delta-hedge =
        # the separate 5.1b-ii subsystem.) Fail-closed on out-of-envelope specs
        # rather than submit a malformed short/unhedged structure.
        # SP-5.2b: credit_vertical/iron_condor are short-only; handled in the _is_credit branch below.
        _is_credit = spec.structure in ('credit_vertical', 'iron_condor')
        _dir = str(order.get('direction', 'long')).lower()
        if _is_credit:
            if _dir != 'short':
                return _option_skip(order, coid, equity,
                    f"option: credit structures are short-only (direction={order.get('direction')!r}; use 'vertical' for debit)")
            if (getattr(spec, 'hedge', 'none') or 'none') != 'none':
                return _option_skip(order, coid, equity,
                    "option: hedge not supported on credit structures (SP-5.2b)")
            if order.get('contracts') is None:
                return _option_skip(order, coid, equity,
                    'option: credit structures require explicit contracts (notional sizing undefined for credits)')
        elif _dir != 'long':
            return _option_skip(order, coid, equity,
                f"option: structure exec is long-only in SP-5.1b-i (direction={order.get('direction')!r})")
        if not _is_credit:
            # SP-5.1c envelope-lift: hedge='delta' is allowed for LONG structures
            # when the delta-hedge subsystem gate is ON (5.1b-ii target-producer +
            # the on-fill ledger write below). Any other non-none hedge stays
            # refused. Credit structures never reach here (hedge refused above).
            _hedge = getattr(spec, 'hedge', 'none') or 'none'
            if _hedge == 'delta':
                if spec.structure not in ('straddle', 'strangle'):
                    # Post-5.2 fail-closed: only ALL-BUY structures may delta-hedge.
                    # A vertical's SHORT far leg has no side in the ledger leg
                    # schema and compute_structure_delta sums deltas unsigned —
                    # the EOD hedge target would be WRONG. Refuse until the
                    # ledger schema carries per-leg side.
                    return _option_skip(order, coid, equity,
                        f"option: hedge='delta' supported on straddle/strangle only "
                        f"(ledger legs carry no side; {spec.structure} has a short leg)")
                if _os.environ.get('OPENCLAW_OPTION_DELTA_HEDGE') != '1':
                    return _option_skip(order, coid, equity,
                        "option: hedge='delta' requires OPENCLAW_OPTION_DELTA_HEDGE=1")
            elif _hedge != 'none':
                return _option_skip(order, coid, equity,
                    f"option: hedge={spec.hedge!r} unsupported (only none|delta)")
        legs = _resolve_structure_legs(spec, today, expiry)
        if not legs:
            return _option_skip(order, coid, equity, 'option: structure legs unresolved')
        nq = _structure_net_quote(spec, legs, expiry)
        if nq is None:
            return _option_skip(order, coid, equity, 'option: no quote for a structure leg')
        net_ask, leg_q = nq
        if _is_credit:
            if net_ask >= 0:
                return _option_skip(order, coid, equity,
                    f'option: non-negative net for credit structure ({net_ask:.2f}) — crossed quotes (credit-only)')
        elif net_ask <= 0:
            return _option_skip(order, coid, equity,
                f'option: non-positive net debit ({net_ask:.2f}) — crossed quotes or inverted spread (debit-only)')
        override = order.get('limit_price_override')
        net_limit = float(override) if override is not None else round(net_ask + 0.02, 2)
        if _is_credit and net_limit >= 0:
            return _option_skip(order, coid, equity,
                f'option: credit limit not negative after pad/override ({net_limit:.2f}) — '
                f'credit too small or bad override (must receive premium)')
        raw_qty, qty_reason = _resolve_option_qty(
            {'contracts': order.get('contracts'), 'notional_usd': order.get('notional_usd')},
            net_limit)
        if qty_reason and raw_qty == 0:
            return _option_skip(order, coid, equity, qty_reason)
        legs_json = _build_mleg_legs_json(leg_q)
        _lp = f'{net_limit:.2f}'
        _lp_args = ([f'--limit-price={_lp}'] if net_limit < 0 else ['--limit-price', _lp])
        args = ['order','submit','--order-class','mleg','--qty', str(int(raw_qty)),
                '--legs', legs_json,
                '--type','limit', *_lp_args,
                '--time-in-force','day','--client-order-id', coid]
        ok, payload, err = _run_alpaca_cli(args)
        if not ok or not payload:
            return _option_skip(order, coid, equity, f'option: mleg submit failed ({err or "no payload"})')
        # SP-5.1c: write the option_hedge_ledger structural row when hedge='delta'.
        # Guarded: the structure is ALREADY submitted — a ledger-write failure must
        # never abort the order path. Logs loudly on failure so the operator knows
        # the hedge target will be MISSING from the EOD compute step. (Credit
        # structures never reach here with hedge='delta' — refused in the envelope.)
        if getattr(spec, 'hedge', 'none') == 'delta':
            try:
                from execution.option_hedge import upsert_hedge_ledger_on_fill
                _leg_dicts = [
                    {'occ': occ, 'right': right, 'strike': float(strike),
                     'expiry': expiry.isoformat()}
                    for (occ, _r, _s), (right, strike, _side) in zip(leg_q, legs)]
                _uri = _os.environ.get('POSTGRES_URI', '')
                if not _uri:
                    raise RuntimeError('POSTGRES_URI not set')
                with psycopg2.connect(_uri) as _hc:
                    with _hc.cursor() as _hcur:
                        upsert_hedge_ledger_on_fill(
                            _hcur,
                            option_strategy_id=order.get('strategy_id') or spec.underlying,
                            underlying=spec.underlying,
                            legs=_leg_dicts,
                            contracts=int(raw_qty))
            except Exception as _e:
                log(f'[sp5.1c] hedge ledger-write failed for {spec.underlying} '
                    f'(structure submitted, hedge target will be MISSING): {_e}')
        # mleg result uses ticker=underlying (e.g. 'SPY') + separate structure/legs fields; downstream (record_submission, close) keys on the underlying, not a per-leg OCC.
        return {
            'ticker': spec.underlying, 'structure': spec.structure, 'status': 'submitted',
            'order_id': payload.get('id'), 'qty': int(raw_qty),
            'notional': float(raw_qty) * 100.0 * float(net_limit), 'entry': net_limit,
            'tif': 'day', 'order_class': 'mleg', 'client_order_id': coid,
            'instrument_class': 'option', 'legs': [lq[0] for lq in leg_q],
        }

    strike = _resolve_strike(spec, today, expiry)
    if strike is None:
        return _option_skip(order, coid, equity, 'option: strike unresolved')
    occ = _build_occ_symbol(spec, strike, expiry)

    # close_only branch
    if order.get('close_only'):
        return _route_option_close(order, occ, coid)

    # Quote-driven marketable limit
    quote = _option_quote(occ)
    if quote is None:
        return _option_skip(order, coid, equity, 'option: no quote for limit price')
    # Sub-contract guard uses the limit price
    side_intent_qty_lookup = _options_current_qty(occ)
    final_side, intent = _options_position_intent(direction, side_intent_qty_lookup)
    # `limit_price_override` lets a sentinel probe (system_check / smoke
    # rest-then-cancel) force a deliberately non-marketable limit so the order
    # RESTS instead of filling. The production sizer never sets this key, so
    # real orders take the computed marketable limit and are byte-identical.
    override = order.get('limit_price_override')
    limit = (float(override) if override is not None
             else _option_marketable_limit(final_side, quote))

    raw_qty, qty_reason = _resolve_option_qty(order, limit)
    if qty_reason and raw_qty == 0:
        return _option_skip(order, coid, equity, qty_reason)
    if qty_reason: log(qty_reason)

    # Cash-collateral guard (short put only; zero otherwise)
    cash = _account_cash()
    if cash is None:
        return _option_skip(order, coid, equity, 'option: account.cash fetch failed')
    final_qty, cash_reason = _apply_cash_collateral_guard(
        right=right, side=final_side, strike=strike, qty=raw_qty, account_cash=cash)
    if final_qty == 0:
        return _option_skip(order, coid, equity, cash_reason)
    if cash_reason: log(cash_reason)

    # Submit
    args = ['order','submit',
            '--symbol', occ, '--qty', str(int(final_qty)),
            '--side', final_side, '--type','limit',
            '--limit-price', f"{limit:.2f}",
            '--time-in-force','day',
            '--position-intent', intent,
            '--client-order-id', coid]
    ok, payload, err = _run_alpaca_cli(args)
    if not ok or not payload:
        return _option_skip(order, coid, equity, f'option: submit failed ({err or "no payload"})')

    notional = float(final_qty) * 100.0 * float(limit)
    return {
        'ticker': occ, 'status': 'submitted',
        'order_id': payload.get('id'),
        'qty': final_qty, 'notional': notional, 'entry': limit,
        'tif': 'day', 'order_class': 'simple',
        'client_order_id': coid,
        'instrument_class': 'option',
    }


def _route_option_close(order: dict, occ: str, coid: str) -> dict:
    """Single-leg option close via `position close`."""
    pct = order.get('close_percentage')
    args = ['position','close','--symbol-or-asset-id', occ]
    if pct is not None and 0 < float(pct) < 100:
        args += ['--percentage', f"{float(pct):.2f}"]
    ok, payload, err = _run_alpaca_cli(args)
    if not ok or not payload:
        return _option_skip(order, coid, equity=0.0,
            reason=f'option-close: failed ({err or "no payload"})')
    return {
        'ticker': occ, 'status': 'submitted',
        'order_id': payload.get('id') or payload.get('order_id'),
        'qty': payload.get('qty'),
        'notional': 0.0, 'entry': 0.0, 'tif': 'day',
        'order_class': 'simple', 'client_order_id': coid,
        'instrument_class': 'option',
    }


def _option_skip(order: dict, coid: str, equity: float, reason: str) -> dict:
    """Standard SKIP result dict for options. Writes a SKIP audit row downstream."""
    log(f'[option] SKIP {order.get("ticker","?")}: {reason}')
    return {
        'ticker': order.get('ticker', '?'),
        'status': 'skipped', 'reason': reason,
        'order_id': None, 'qty': 0, 'notional': 0.0, 'entry': 0.0,
        'tif': 'day', 'order_class': 'simple',
        'client_order_id': coid, 'instrument_class': 'option',
    }


def _held_option_legs(underlying: str) -> list:
    """OCC symbols of currently-HELD option positions on `underlying` (qty != 0),
    from `position list`. An option symbol's leading alpha run is its underlying
    root and it is longer than that root (plain equity 'SPY' is excluded, so the
    equity book is never closed)."""
    ok, payload, _ = _run_alpaca_cli(['position', 'list'])
    if not ok or not payload:
        return []
    out = []
    for p in payload:
        sym = str(p.get('symbol') or '')
        root = ''
        for c in sym:
            if c.isalpha(): root += c
            else: break
        try:
            qty = float(p.get('qty') or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if root == underlying and len(sym) > len(root) and qty != 0:
            out.append(sym)
    return out


def _deactivate_hedge_for_underlying_guarded(underlying):
    """SP-5 Phase 1b (G4c): deactivate option_hedge_ledger rows for `underlying` after a
    structure close, so it stops generating EOD hedge targets. GUARDED exactly like the
    on-fill ledger write (_route_option_order): opens its OWN psycopg2 connection from
    POSTGRES_URI, logs loudly on failure, and NEVER raises — a deactivation failure must
    not abort the close path (the structure is already flattened). Returns rows deactivated
    (0 on any failure)."""
    try:
        from execution.option_hedge import deactivate_hedge_ledger_for_underlying
        _uri = os.environ.get('POSTGRES_URI', '')
        if not _uri:
            raise RuntimeError('POSTGRES_URI not set')
        with psycopg2.connect(_uri) as _hc:
            with _hc.cursor() as _hcur:
                n = deactivate_hedge_ledger_for_underlying(_hcur, underlying)
        log(f'[sp5-G4c] deactivated {n} hedge-ledger row(s) for {underlying} on structure close')
        return n
    except Exception as _e:   # noqa: BLE001 — close path must never abort on ledger bookkeeping
        log(f'[sp5-G4c] hedge-ledger deactivation failed for {underlying} '
            f'(structure closed; stale hedge targets may persist until operator review): {_e}')
        return 0


def _route_mleg_close(order, spec, coid):
    """Close a straddle/strangle by closing the ACTUALLY-HELD option legs on the
    underlying (from `position list`) — NOT by re-resolving strikes from spec,
    which drift between open and close. Per-leg `position close` (credit-sign
    independent; spec §4.1).

    Success semantics: there is NO exception path — every held leg is attempted and
    recorded as (occ, ok, err). status='submitted' iff all legs closed OK, else 'partial'.
    """
    held = _held_option_legs(spec.underlying)
    if not held:
        return _option_skip(order, coid, equity=0.0, reason='option-close: no held legs')
    closed = []
    for occ in held:
        ok, _payload, err = _run_alpaca_cli(['position', 'close', '--symbol-or-asset-id', occ])
        closed.append((occ, ok, err))
    any_fail = any((not ok) for _occ, ok, _err in closed)
    # SP-5 Phase 1b (G4c): after the per-leg close loop (success OR partial), deactivate
    # the hedge ledger for this underlying — a closed structure must not keep generating
    # EOD hedge targets. Guarded/non-aborting (the close already happened). On a PARTIAL
    # close, deactivation is intentional too: the operator's flat-verify (runbook) catches
    # any residual leg, and a leftover ACTIVE ledger row would keep injecting hedge targets
    # against a half-gone structure.
    if any_fail:
        log(f'[sp5-G4c] PARTIAL close for {spec.underlying} — deactivating ledger anyway; '
            f'operator must flat-verify residual leg(s)')
    _deactivate_hedge_for_underlying_guarded(spec.underlying)
    return {
        'ticker': spec.underlying, 'structure': spec.structure,
        'status': 'submitted' if not any_fail else 'partial',
        'order_id': None, 'qty': 0, 'notional': 0.0, 'entry': 0.0,
        'tif': 'day', 'order_class': 'mleg', 'client_order_id': coid,
        'instrument_class': 'option', 'legs': [c[0] for c in closed],
    }


def _is_crypto_ticker(raw: str | None) -> bool:
    """True if `raw` is a crypto pair in the engine's BASE-USD convention
    (e.g. 'BTC-USD'). Mirrors collector.js _classifyMarketTicker's /-USD$/.
    Note: _normalize_alpaca_symbol() returns None for these (line 403), so a
    crypto order must be intercepted on the RAW ticker before that check."""
    return bool(raw) and raw.strip().upper().endswith('-USD')


def _alpaca_crypto_symbol(raw: str) -> str:
    """'BTC-USD' -> 'BTC/USD' (Alpaca crypto pair format). Python port of
    collector.js _alpacaCryptoSymbol (replace '-' with '/')."""
    return raw.strip().upper().replace('-', '/')


# SP-3.1 Phase A: crypto order params. Confirmed by Task 0 spike
# (docs/superpowers/specs/sp3.1-task0-crypto-cli-snapshot.md).
_CRYPTO_TIF = 'gtc'        # crypto rejects 'day'/'opg'; a market order rests-then-fills
_CRYPTO_QTY_DECIMALS = 9   # Alpaca BTC qty precision (Task 0 confirms min order size)


def _crypto_latest_price(api_symbol: str) -> float | None:
    """Latest trade price for an Alpaca crypto pair ('BTC/USD'), or None.
    Response path confirmed in Task 0 snapshot: .trades[api_symbol].p."""
    ok, payload, _err = _run_alpaca_cli(
        ['data', 'crypto', 'latest-trades', '--symbols', api_symbol], timeout=10)
    if not ok or not isinstance(payload, dict):
        return None
    trade = (payload.get('trades') or {}).get(api_symbol) or {}
    try:
        p = float(trade.get('p') or 0.0)
    except (TypeError, ValueError):
        return None
    return p if p > 0 else None


def _route_crypto_order(order: dict, equity: float, coid: str) -> dict | None:
    """SP-3.1 Phase A: handle a crypto order, or return None to fall through
    to the equity path. Crypto is 24/7, fractional-qty, market+gtc, simple
    order_class (no bracket, no broker-side stop — Phase D). Open orders are
    sized + submitted here; close_only delegates to _route_crypto_close.
    execute_single calls this once, above the equity session/qty logic, so
    neither path is missed."""
    raw_ticker = order.get('ticker') or ''
    if not _is_crypto_ticker(raw_ticker):
        return None
    api_symbol = _alpaca_crypto_symbol(raw_ticker)
    side = 'sell' if str(order.get('direction') or '').lower() == 'short' else 'buy'

    if order.get('close_only'):
        return _route_crypto_close(order, api_symbol, raw_ticker, coid)

    pct_nav = float(order.get('pct_nav') or 0.0)
    notional = equity * pct_nav
    if notional <= 0:
        return {'ticker': api_symbol, 'status': 'SKIP',
                'reason': 'crypto: non-positive notional',
                'client_order_id': coid, 'tif': _CRYPTO_TIF, 'order_class': 'simple'}
    price = _crypto_latest_price(api_symbol)
    if price is None:
        return {'ticker': api_symbol, 'status': 'SKIP',
                'reason': 'crypto: no latest-trade price',
                'client_order_id': coid, 'tif': _CRYPTO_TIF, 'order_class': 'simple'}
    qty = round(notional / price, _CRYPTO_QTY_DECIMALS)
    if qty <= 0:
        return {'ticker': api_symbol, 'status': 'SKIP',
                'reason': f'crypto: qty rounds to 0 (notional={notional:.2f} price={price:.2f})',
                'client_order_id': coid, 'tif': _CRYPTO_TIF, 'order_class': 'simple'}
    ok, pay, err = _submit_order_via_cli(
        ticker=api_symbol, side=side, qty=qty, tif=_CRYPTO_TIF,
        order_class='simple', target=None, stop=None, coid=coid,
        order_type='market', extended_hours=False, limit_price=None)
    if ok:
        _pay = pay if isinstance(pay, dict) else {}
        oid = _pay.get('id') or _pay.get('order_id')
        log(f'✔ {raw_ticker} CRYPTO {side.upper()} x{qty}  ~${notional:,.0f} @ {price:,.2f}'
            f'  order={oid or "?"}')
        stop_px = float(order.get('stop') or 0.0)
        if stop_px > 0 and oid:
            # Best-effort resting stop. This runs AFTER a successful entry and
            # ABOVE execute_single's try-block, so it must never raise — a CLI
            # subprocess hang (TimeoutExpired) would otherwise abort the whole
            # batch and orphan this just-opened position with no audit row.
            try:
                # 45s window: crypto paper fills can lag >10s; too short a poll
                # skips the protective stop (observed in the Phase D live smoke).
                filled_qty = _poll_crypto_fill(oid, timeout=45.0)
                if filled_qty > 0:
                    _submit_crypto_stop(api_symbol, stop_px, filled_qty, coid)
                else:
                    log(f'  ↳ crypto stop skipped — entry {oid} not filled in time (entry stands)')
            except Exception as _stop_err:   # noqa: BLE001 — entry must stand
                log(f'  ↳ crypto stop best-effort failed (entry stands): {_stop_err}')
        return {'ticker': api_symbol, 'status': 'submitted', 'qty': qty,
                'notional': notional, 'entry': price, 'order_id': oid, 'http': 200,
                'tif': _CRYPTO_TIF, 'order_class': 'simple', 'client_order_id': coid}
    log(f'CLI rc={err.get("exit_code",1)} {raw_ticker} (crypto open): {err.get("error","")}')
    return {'ticker': api_symbol, 'status': 'rejected',
            'reason': err.get('error') or 'crypto order submit failed',
            'http': err.get('status'), 'body': str(err),
            'tif': _CRYPTO_TIF, 'order_class': 'simple', 'client_order_id': coid}


def _route_crypto_close(order: dict, api_symbol: str, raw_ticker: str, coid: str) -> dict:
    """Flatten or partially reduce a crypto position. No session gate (24/7).
    Mirrors the equity close_only partial-reduce math (execute_single:863-870)."""
    notional_oc = abs(float(order.get('notional_usd') or 0))
    current_oc = abs(float(order.get('current_usd') or 0))
    cli_args = ['position', 'close', '--symbol-or-asset-id', api_symbol]
    is_partial = (current_oc > 0 and notional_oc < current_oc * 0.999)
    if is_partial:
        pct = max(0.01, min(99.99, round((notional_oc / current_oc) * 100.0, 2)))
        cli_args += ['--percentage', str(pct)]
    # Phase D: a resting sell stop reserves the position → close would 403.
    # Best-effort + guarded: a CLI hang here must never abort the close.
    try:
        _cancel_open_crypto_stops(api_symbol)
    except Exception as _cx:   # noqa: BLE001 — close must proceed
        log(f'  ↳ crypto stop-cancel best-effort failed (continuing to close): {_cx}')
    ok, pay, err = _run_alpaca_cli(cli_args, timeout=15)
    if ok:
        _pay = pay if isinstance(pay, dict) else {}
        order_id = _pay.get('id') or _pay.get('order_id')
        notional_close = abs(float(_pay.get('notional') or notional_oc))
        qty_close = float(_pay.get('qty') or 0.0)
        entry_approx = round(notional_close / qty_close, 6) if qty_close > 0 else 0.0
        kind = 'REDUCE' if is_partial else 'CLOSE'
        log(f'↩ {raw_ticker} CRYPTO {kind}  notional≈${notional_close:,.0f}  order={order_id or "?"}')
        return {'ticker': api_symbol, 'status': 'submitted', 'qty': qty_close,
                'notional': notional_close, 'entry': entry_approx, 'order_id': order_id,
                'http': 200, 'tif': _CRYPTO_TIF, 'order_class': 'simple',
                'client_order_id': coid}
    log(f'CLI rc={err.get("exit_code",1)} {raw_ticker} (crypto close): {err.get("error","")}')
    return {'ticker': api_symbol, 'status': 'rejected',
            'reason': err.get('error') or 'crypto position close failed',
            'http': err.get('status'), 'body': str(err),
            'tif': _CRYPTO_TIF, 'order_class': 'simple', 'client_order_id': coid}


def _poll_crypto_fill(order_id: str, timeout: float = 10.0,
                      poll_interval: float = 0.5) -> float:
    """SP-3.1 Phase D: poll a crypto entry order until it fills, return the
    actual filled qty (crypto market orders fill async + slightly short of the
    requested qty due to fees). Returns the filled qty (float) on a filled /
    partially_filled order, or 0.0 on terminal-non-fill (rejected/canceled/
    expired) / timeout / CLI error. Returns 0.0 on any CLI-error response; a CLI
    subprocess hang can still raise TimeoutExpired, so the caller wraps this in a
    best-effort guard (the entry must stand regardless)."""
    import time as _time
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        ok, payload, _err = _run_alpaca_cli(['order', 'get', order_id], timeout=5)
        if ok and isinstance(payload, dict):
            status = str(payload.get('status') or '').lower()
            try:
                fq = float(payload.get('filled_qty') or 0.0)
            except (TypeError, ValueError):
                fq = 0.0
            if status == 'filled':
                return fq
            if status in ('canceled', 'cancelled', 'rejected', 'expired'):
                return fq if fq > 0 else 0.0   # partial-then-canceled still protectable
        _time.sleep(poll_interval)
    # Timed out — return whatever last filled qty we saw (0.0 if none).
    ok, payload, _err = _run_alpaca_cli(['order', 'get', order_id], timeout=5)
    if ok and isinstance(payload, dict):
        try:
            return float(payload.get('filled_qty') or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _submit_crypto_stop(api_symbol: str, stop_price: float, qty: float,
                        coid: str) -> dict | None:
    """SP-3.1 Phase D: place a resting SELL stop_limit on a crypto long position.
    Crypto has no bracket/OCO, so the stop is a separate resting order placed
    after the entry fills (sized to the ACTUAL filled qty). stop_limit is the
    only crypto stop type Alpaca accepts (plain 'stop' → 422). The limit price
    sits BELOW the stop so the order is marketable on a downward break.
    Returns a small result dict on success, or None on failure / invalid input
    (the caller keeps the entry result — fail-safe, never unwinds the entry).
    Shape per docs/superpowers/specs/sp3.1-phase-d-crypto-stop-snapshot.md."""
    if not (stop_price and stop_price > 0 and qty and qty > 0):
        return None
    limit_price = round(stop_price * 0.995, 2)   # 0.5% below stop → marketable
    args = [
        'order', 'submit',
        '--symbol',           api_symbol,
        '--side',             'sell',
        '--qty',              str(qty),
        '--type',             'stop_limit',
        '--stop-price',       _price_str(stop_price),
        '--limit-price',      _price_str(limit_price),
        '--time-in-force',    _CRYPTO_TIF,
        '--client-order-id',  f'{coid}-stop',
    ]
    ok, pay, err = _run_alpaca_cli(args, timeout=15)
    if ok:
        _pay = pay if isinstance(pay, dict) else {}
        oid = _pay.get('id') or _pay.get('order_id')
        log(f'  ↳ crypto STOP rest @ {stop_price:,.2f} (limit {limit_price:,.2f}) order={oid or "?"}')
        return {'order_id': oid, 'status': _pay.get('status'), 'stop_price': stop_price}
    _e = err.get('error', '') if isinstance(err, dict) else str(err)
    log(f'  ↳ crypto STOP submit failed (entry stands): {_e}')
    return None


def _cancel_open_crypto_stops(api_symbol: str) -> int:
    """SP-3.1 Phase D: cancel any OPEN crypto orders for api_symbol (e.g.
    'BTC/USD'). A resting sell stop reserves the position, so a close/flatten
    would 403 unless the stop is cancelled first. Returns count cancelled.
    Returns 0 on any CLI-error response; a CLI subprocess hang can still raise
    TimeoutExpired, so the caller wraps this in a best-effort guard."""
    ok, payload, _err = _run_alpaca_cli(['order', 'list', '--status', 'open'], timeout=10)
    if not ok or not isinstance(payload, list):
        return 0
    n = 0
    for o in payload:
        if not isinstance(o, dict):
            continue
        if o.get('symbol') == api_symbol:
            oid = o.get('id') or o.get('order_id')
            if oid:
                c_ok, _p, _e = _run_alpaca_cli(['order', 'cancel', '--order-id', oid], timeout=10)
                if c_ok:
                    n += 1
    if n:
        log(f'  ↳ cancelled {n} open crypto order(s) for {api_symbol} before close')
    return n


def _cancel_open_equity_orders(ticker: str) -> int:
    """SP-6 Phase C: cancel any OPEN equity orders for `ticker` before an RTH
    `position close`.

    The OCO take-profit system (live since 2026-05-29) rests GTC OCO
    take-profit/stop legs that reserve the FULL position qty. While they rest,
    `alpaca position close` (DELETE /v2/positions) can never succeed — it 403s
    "insufficient qty available for order (requested: N, available: 0)". This
    blocked EVERY deliberate equity close on 2026-06-04: all 4 of the 3:55 PM ET
    EOD orphan-close orders (AAPL/LEN/PTC/SPGI) were submitted FIRST by the tier
    sort yet 403'd because their OCO legs reserved the qty. The crypto lane has
    fixed this exact bug since 2026-05-26 (_cancel_open_crypto_stops); this is
    the equity sibling.

    Settle-poll (the load-bearing part): a cancel ACK does NOT immediately
    release the reserved qty — Alpaca flips the order `pending_cancel` →
    `canceled` asynchronously, releasing the hold only at the second transition.
    Firing `position close` right after the cancel ACK can therefore STILL 403
    "insufficient qty available" — the exact failure this change exists to
    eliminate, and worse than the crypto precedent because an OCO holds the qty
    across TWO legs (two holds to clear). So after issuing the cancels we poll
    EACH successfully-cancelled order id to a terminal status ('canceled' is in
    `_TERMINAL_ORDER_STATUSES`) with a SHORT bound before returning, so the
    caller's close fires only once the holds have cleared. Bound: timeout_s=10
    per leg (worst case N×10s; realistic N ≤ 4 OCO legs ⇒ ≤ 40s). interval_s
    is floored to 1s inside `_poll_to_terminal` (cancels settle sub-second in
    practice, usually on the first poll before any sleep). On poll
    timeout/None/error we log and return anyway — best-effort degrades to the
    pre-change 403, never blocks or aborts the close.

    Reach + re-protection window (operator-accepted 2026-06-05 — stated here so
    a future reader does not rediscover it as a surprise): the gate
    `OPENCLAW_EOD_RECONCILE=1` is ON in production, so this cancel-then-close
    sequence fires on ALL RTH `position close` calls — the 10 AM daily cycle and
    partial reduces too — NOT only the 3:55 PM ET EOD dump. Cancelling the
    protective legs opens a brief unprotected window if the close itself then
    fails. The 10 AM daily cycle re-covers in-cycle via its own stop_reattach
    step. The EOD execute path (`trade,alpaca,reconcile,report,health`) has NO
    such in-cycle step — its only re-cover is the standalone 16:10 ET
    stop_reattach timer, so a failed EOD close can leave a position naked for up
    to ~15 min (worst case until the 16:10 floor runs). This is the accepted
    design.

    Returns the count cancelled. Returns 0 on any CLI-error response; a CLI
    subprocess hang can still raise TimeoutExpired, so the caller wraps this in
    a best-effort guard (the close must proceed regardless)."""
    ok, payload, _err = _run_alpaca_cli(['order', 'list', '--status', 'open'], timeout=10)
    if not ok or not isinstance(payload, list):
        return 0
    cancelled_ids: list[str] = []
    for o in payload:
        if not isinstance(o, dict):
            continue
        if o.get('symbol') == ticker:
            oid = o.get('id') or o.get('order_id')
            if oid:
                c_ok, _p, _e = _run_alpaca_cli(['order', 'cancel', '--order-id', oid], timeout=10)
                if c_ok:
                    cancelled_ids.append(oid)
    n = len(cancelled_ids)
    if not n:
        return 0
    log(f'  ↳ cancelled {n} open order(s) for {ticker}; settling holds before close')
    # Cancel-all THEN poll-all: both OCO holds clear in parallel, and the close
    # fires only once the async pending_cancel→canceled release has settled.
    for oid in cancelled_ids:
        try:
            final = _poll_to_terminal(oid, timeout_s=10, interval_s=0.5)
            if final is None:
                log(f'  ↳ cancel-settle poll timed out for {oid} (proceeding to close)')
        except Exception as _px:   # noqa: BLE001 — best-effort; close must proceed
            log(f'  ↳ cancel-settle poll errored for {oid} (proceeding to close): {_px}')
    return n


def _looks_like_unsupported_asset_error(err_text: str) -> bool:
    """Best-effort classifier for Alpaca 4xx errors that indicate the asset
    itself isn't tradable on this account/feed (delisted, halted, asset
    not found, not on paper feed, etc.). Returns True if the ticker
    should be cached as unsupported for the rest of this run."""
    s = (err_text or '').lower()
    needles = (
        'asset',                      # "asset not found", "invalid asset"
        'not tradable', 'not_tradable',
        'symbol not found', 'symbol_not_found',
        'cannot be traded', 'cannot trade',
        'asset is no longer tradable',
        'security halted', 'halted',
        'not supported',
    )
    return any(n in s for n in needles)


def _run_alpaca_cli(args, timeout=30):
    """Run the alpaca CLI subprocess.

    Returns (ok, payload, err) where:
      ok=True  → payload is the parsed stdout JSON (or raw stdout if not JSON);
                 err is None.
      ok=False → payload is None; err is a dict with keys
                 {exit_code, status, code, error, error_json, raw_stderr}.
                 `status` is the HTTP status (422, 404, etc.) when the CLI
                 emits a structured error JSON to stderr; None otherwise.

    The CLI emits JSON to stdout on success and a JSON error envelope to
    stderr on failure (with `"status": <http>`, `"error": "..."`,
    `"code": <numeric>`). Exit code is 0 on success, non-zero on error.
    """
    proc = subprocess.run(
        [ALPACA_CLI, *args],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if proc.returncode == 0:
        try:
            return True, json.loads(proc.stdout), None
        except json.JSONDecodeError:
            return True, proc.stdout, None
    err = {
        'exit_code': proc.returncode,
        'raw_stderr': proc.stderr,
        'status': None,
        'error': proc.stderr.strip(),
        'code': None,
        'error_json': None,
    }
    try:
        ej = json.loads(proc.stderr)
        err['error_json'] = ej
        err['status'] = ej.get('status')
        err['code']   = ej.get('code')
        if ej.get('error'):
            err['error'] = ej.get('error')
    except json.JSONDecodeError:
        pass
    return False, None, err


def _exec_priority_for_test(o: dict) -> tuple[int, float]:
    """Tier-priority key used by the main loop's `sorted(orders, key=...)`.

    Exposed at module scope so tests can pin the contract:
      0 = orphan close   (strategy_id == '__close_orphan__')
      1 = other close    (close_only=True; partial reduce OR flip close)
      2 = short open
      3 = long open
    Secondary key = -notional so larger sizes sort first within a tier.
    """
    is_orphan = (o.get('strategy_id') or '') == '__close_orphan__'
    is_close  = bool(o.get('close_only'))
    is_short  = str(o.get('direction') or '').lower() in ('short', 'sell')
    notional  = float(o.get('notional_usd') or 0.0)
    if is_orphan:
        tier = 0
    elif is_close:
        tier = 1
    elif is_short:
        tier = 2
    else:
        tier = 3
    return (tier, -notional)


def _dtbp_opening_budget(account: dict) -> float:
    """Max notional of NEW opening orders the account can fund this cycle.

    = max(0, min(buying_power, regt_buying_power [, daytrading_buying_power])).
    FINRA retired the PDT rule 2026-06-04 and Alpaca REMOVES the
    daytrading_buying_power account field on 2026-07-06 (dummy values until
    then) — the old `min(dtbp, regt)` would read the missing field as 0 and
    permanently skip every open. Under the Intraday Margin Framework the
    binding intraday constraint is the real-time `buying_power` field;
    regt_buying_power stays the Reg-T overnight cap. While the legacy field
    is still present AND positive it is honored as an extra min() term, so
    pre-removal behavior is unchanged; a 0/dummy/absent value is ignored.
    Missing/negative inputs -> 0.0 (fail safe: skip opens, never over-submit).

    A failed account re-fetch (fetched is explicitly False) returns 0.0 so
    the guard skips opens rather than over-submitting on stale fallback values.
    (Absent 'fetched' key — e.g. unit-test dicts — is treated as fine.)
    """
    if account.get('fetched') is False:
        return 0.0
    bp = float(account.get('buying_power') or 0.0)
    regt = float(account.get('regt_buying_power') or 0.0)
    budget = min(bp, regt)
    try:
        legacy = float(account.get('daytrading_buying_power'))
    except (TypeError, ValueError):
        legacy = None
    if legacy is not None and legacy > 0:
        budget = min(budget, legacy)
    return max(0.0, budget)


def _open_conviction(o: dict) -> float:
    """Conviction rank for an opening order: kelly_final, else pct_nav."""
    k = o.get('kelly_final')
    if k is not None:
        try:
            return abs(float(k))
        except (TypeError, ValueError):
            pass
    return abs(float(o.get('pct_nav') or 0.0))


def _compute_dtbp_skips(open_orders: list, account: dict, equity: float) -> set:
    """Return the set of (ticker, strategy_id) opening orders to skip because
    they exceed the day-trade/Reg-T budget. Highest-conviction opens are
    funded first; at the first open that does not fit the remaining budget,
    it and ALL lower-conviction opens are skipped. Notional basis =
    equity * pct_nav."""
    budget = _dtbp_opening_budget(account)
    ranked = sorted(open_orders, key=_open_conviction, reverse=True)
    skips: set = set()
    remaining = budget
    cutoff = False
    for o in ranked:
        key = (o.get('ticker') or '???', o.get('strategy_id') or 'unknown')
        notional = float(equity) * float(o.get('pct_nav') or 0.0)
        if cutoff or notional > remaining:
            cutoff = True
            skips.add(key)
        else:
            remaining -= notional
    return skips


def _dtbp_guard_enabled() -> bool:
    """Kill-switch, default ON. Set OPENCLAW_DTBP_GUARD=0 to disable (then
    the executor behaves byte-identically to the pre-guard path)."""
    return os.environ.get('OPENCLAW_DTBP_GUARD', '1') != '0'


def _record_placed_bracket_on() -> bool:
    """W1: when ON, alpaca_submissions records the legs actually submitted
    (from the execute result) rather than the pre-submit per-strategy order."""
    return os.environ.get('OPENCLAW_RECORD_PLACED_BRACKET') == '1'


def _dtbp_summary_line(skipped: list) -> str:
    """One-line buying-power-skip summary, or '' if none were skipped for BP."""
    bp = [s for s in skipped if s.get('reason') == 'dtbp_budget_exhausted']
    if not bp:
        return ''
    tickers = ', '.join(s['ticker'] for s in bp[:10])
    return f'⛔ {len(bp)} opens skipped for buying-power (lowest-conviction): {tickers}'


def _bp_snapshot(account: dict, run_date, path: str | None = None) -> None:
    """Append one buying-power snapshot row to logs/bp_snapshots.csv
    (append-only; header written once). Best-effort — never raises into
    the executor."""
    import csv as _csv, datetime as _dt
    try:
        p = Path(path) if path else (ROOT / 'logs' / 'bp_snapshots.csv')
        p.parent.mkdir(parents=True, exist_ok=True)
        new = not p.exists()
        with open(p, 'a', newline='') as f:
            w = _csv.writer(f)
            if new:
                w.writerow(['timestamp', 'run_date', 'equity',
                            'daytrading_buying_power', 'regt_buying_power',
                            'daytrade_count', 'multiplier'])
            w.writerow([
                _dt.datetime.utcnow().isoformat(), str(run_date),
                account.get('equity'), account.get('daytrading_buying_power'),
                account.get('regt_buying_power'), account.get('daytrade_count'),
                account.get('multiplier'),
            ])
    except Exception as e:
        log(f'[dtbp-guard] bp_snapshot write failed: {e}')


def _wait_for_fill(order_id: str, timeout: float = 15.0, poll_interval: float = 0.5) -> bool:
    """Poll `alpaca order get <id>` until status='filled' or timeout/terminal.

    Used by the main executor loop between a direction-flip's close-leg and
    its matched open-leg: Alpaca cannot transition long↔short in a single
    bracket order, so the close must FILL (not just be submitted) before
    the open is sent. Returns True on filled; False on timeout, terminal
    failure (canceled/rejected/expired), or CLI error. On False the caller
    skips the matched open to avoid oversell against the still-existing
    position.
    """
    import time as _time
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        proc = subprocess.run(
            [ALPACA_CLI, 'order', 'get', order_id],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if proc.returncode == 0:
            try:
                o = json.loads(proc.stdout)
            except json.JSONDecodeError:
                return False
            status = o.get('status', '')
            if status == 'filled':
                return True
            if status in ('canceled', 'rejected', 'expired'):
                return False
        _time.sleep(poll_interval)
    return False


def _submit_order_via_cli(*, ticker, side, qty, tif, order_class, target, stop, coid,
                          order_type='market', extended_hours=False, limit_price=None):
    """Submit a single order via `alpaca order submit`.

    Args:
      tif: time-in-force, one of 'day', 'gtc', 'opg', 'cls', 'ioc', 'fok'.
        'opg' (at-open / market-on-open) is accepted as of SP-6 Task 10 for the
        9:30-reconcile OPG dual-path (is_dropped close_only orders only); it must
        be paired with order_type='market' and is pre-open-only (Alpaca rejects
        opg in RTH). All other callers continue to pass 'day'/'gtc'.
      order_type: 'market' (default, RTH) or 'limit' (extended hours).
      extended_hours: when True, passes the bare `--extended-hours` flag so
        Alpaca routes the order to the pre/post ECN session. Requires
        order_type='limit' + tif='day' per Alpaca's contract.
      limit_price: required when order_type='limit'. Must be a positive float;
        passed as string with 2-decimal price formatting.
      target, stop: only consumed when order_class='bracket'.

    Returns the same (ok, payload, err) tuple as _run_alpaca_cli.
    """
    args = [
        'order', 'submit',
        '--symbol',          ticker,
        '--side',             side,
        '--qty',              str(qty),
        '--type',             order_type,
        '--time-in-force',    tif,
        '--client-order-id',  coid,
    ]
    if order_type == 'limit':
        if limit_price is None:
            # Caller should have filtered; defensive guard.
            return False, None, {
                'exit_code': -1, 'status': None, 'code': None,
                'error': 'limit order requested without limit_price',
                'error_json': None, 'raw_stderr': '',
            }
        args += ['--limit-price', _price_str(limit_price)]
    if extended_hours:
        # Bare flag — CLI treats presence as True per `alpaca order submit --help`.
        args += ['--extended-hours']
    if order_class == 'bracket':
        # Brackets are RTH-only; Alpaca rejects bracket+extended-hours.
        args += [
            '--order-class',  'bracket',
            '--take-profit',  json.dumps({'limit_price': _price_str(target)}),
            '--stop-loss',    json.dumps({'stop_price':  _price_str(stop)}),
        ]
    return _run_alpaca_cli(args)


def _pick_limit_price(ticker: str, side: str, *, max_offset_pct: float = 0.005) -> float | None:
    """Return a marketable limit price for an extended-hours order, or None
    if no usable quote/trade is available.

    Strategy:
      1. Fetch latest quote via `alpaca data latest-quote --symbol <T>`.
         Use bid for sells (offset DOWN) and ask for buys (offset UP) so the
         order is marketable against the thin ext-hours book.
      2. If bid/ask are both zero/missing, fall back to last trade via
         `alpaca data latest-trade --symbol <T>` and apply the same offset.
      3. Return None if all CLI calls fail or yield no usable price.

    Offsets:
      buy:  ask × (1 + max_offset_pct)   default 0.5% above the ask
      sell: bid × (1 - max_offset_pct)   default 0.5% below the bid
    Output rounded to two decimals.

    `side` is the order side ('buy' to open a long / close a short,
    'sell' to open a short / close a long). Timeout 5s per CLI call —
    ext-hours data is sometimes slow.
    """
    side = (side or '').lower()
    if side not in ('buy', 'sell'):
        return None

    # 1. Latest quote.
    ok, payload, _err = _run_alpaca_cli(
        ['data', 'latest-quote', '--symbol', ticker], timeout=5,
    )
    bid = ask = 0.0
    if ok and isinstance(payload, dict):
        q = payload.get('quote') or {}
        try:
            bid = float(q.get('bp') or 0.0)
            ask = float(q.get('ap') or 0.0)
        except (TypeError, ValueError):
            bid = ask = 0.0

    chosen = 0.0
    if side == 'buy' and ask > 0:
        chosen = ask * (1.0 + max_offset_pct)
    elif side == 'sell' and bid > 0:
        chosen = bid * (1.0 - max_offset_pct)

    # 2. Fall back to last trade.
    if chosen <= 0:
        ok2, payload2, _err2 = _run_alpaca_cli(
            ['data', 'latest-trade', '--symbol', ticker], timeout=5,
        )
        if ok2 and isinstance(payload2, dict):
            tr = payload2.get('trade') or {}
            try:
                last = float(tr.get('p') or 0.0)
            except (TypeError, ValueError):
                last = 0.0
            if last > 0:
                if side == 'buy':
                    chosen = last * (1.0 + max_offset_pct)
                else:
                    chosen = last * (1.0 - max_offset_pct)

    if chosen <= 0:
        return None
    return round(chosen, 2)


def _get_asset_meta(ticker: str) -> dict | None:
    """Fetch and cache `alpaca asset get --symbol-or-asset-id <ticker>`.

    Returns the parsed asset metadata dict (with keys `class`, `tradable`,
    `easy_to_borrow`, `shortable`, `status`, …) or None if the CLI call
    failed (e.g. asset not found). Cached for the cycle via _asset_cache;
    cleared in main().

    Note: Alpaca's asset payload uses `class` (one of `crypto`,
    `us_equity`, `us_option`), NOT `asset_class`. Callers must read
    `meta.get('class')` accordingly.
    """
    if ticker in _asset_cache:
        return _asset_cache[ticker]
    ok, payload, _err = _run_alpaca_cli(
        ['asset', 'get', '--symbol-or-asset-id', ticker], timeout=5,
    )
    meta = payload if (ok and isinstance(payload, dict)) else None
    _asset_cache[ticker] = meta or {}
    return meta


def _skip_extended_hours(ticker: str, side: str) -> str | None:
    """Return a reason string if `ticker` is NOT eligible for an
    extended-hours order, else None. Best-effort — Alpaca paper does not
    reliably populate every field, so unknowns are treated as 'pass'
    (don't pre-emptively block).

    Filters (only enforced when meta fetch succeeded):
      - class != 'us_equity'    → skip (options, crypto, etc.)
      - tradable == False       → skip
    Soft check (do NOT skip on missing):
      - easy_to_borrow + sell_short — many paper assets don't populate
        this field; missing → 'unknown' → allow through.
    """
    meta = _get_asset_meta(ticker)
    if not meta:
        # Meta fetch failed entirely — best effort, allow through. The order
        # submit itself will reject with a clear error if the asset really
        # isn't supported, and _unsupported_assets will cache it for the run.
        return None
    cls = meta.get('class')
    if cls and cls != 'us_equity':
        return f'asset_class={cls} (ext-hours us_equity-only)'
    tradable = meta.get('tradable')
    if tradable is False:
        return 'tradable=false'
    # easy_to_borrow is a soft signal; do NOT skip on missing/false here.
    # (Many shortable names show easy_to_borrow=False on paper; Alpaca's
    # locate flow handles availability at submit time.)
    return None


def _get_order_by_coid(coid):
    return _run_alpaca_cli(['order', 'get-by-client-id', '--client-order-id', coid])


# Minimum stop/target distance as a fraction of base. 1% gap absorbs the
# typical ~0.5% intraday drift between our pre-flight quote and the
# `base_price` Alpaca's order endpoint uses for bracket validation. Floored
# higher than the prior 0.5% after 2026-05-15 cycle showed 38 longs
# rejected with `stop_loss must be <= base_price - 0.01` despite signal
# stops 0.3-5% away from entry — drift exceeded the gap on every one.
MIN_BRACKET_GAP_PCT = 0.01


def _recompute_bracket_from_quote(entry, stop, target, side, base):
    """Re-anchor (entry, stop, target) to the current market quote while
    preserving the signal's stop_pct and target_pct relative to entry.

    Side semantics:
      side == 'buy'  (long):  stop <  entry  AND  target >  entry
      side == 'sell' (short): stop >  entry  AND  target <  entry

    Returns: (new_entry, new_stop, new_target, why_str). `why_str` is
    empty when the recompute used preserved ratios; otherwise it explains
    which fallback fired (e.g., 'min-gap floor' or 'degenerate ratio').

    Guarantees on output (with base > 0):
      long:  new_stop   ≤ base × (1 - MIN_BRACKET_GAP_PCT)
             new_target ≥ base × (1 + MIN_BRACKET_GAP_PCT)
      short: new_stop   ≥ base × (1 + MIN_BRACKET_GAP_PCT)
             new_target ≤ base × (1 - MIN_BRACKET_GAP_PCT)
    """
    import math as _math
    # Defensive: caller is responsible for entry > 0 and finite. If
    # something slipped past, snap to base ± min-gap and bail.
    try:
        e = float(entry); s = float(stop); t = float(target); b = float(base)
    except (TypeError, ValueError):
        b = float(base) if base else 0.0
        gap = max(0.02, b * MIN_BRACKET_GAP_PCT)
        if side == 'buy':
            return (round(b, 2), round(b - gap, 2), round(b + gap, 2), 'non-numeric input')
        return (round(b, 2), round(b + gap, 2), round(b - gap, 2), 'non-numeric input')
    if not (_math.isfinite(e) and _math.isfinite(s) and _math.isfinite(t) and _math.isfinite(b) and b > 0):
        b = b if (_math.isfinite(b) and b > 0) else 0.0
        gap = max(0.02, b * MIN_BRACKET_GAP_PCT)
        if side == 'buy':
            return (round(b, 2), round(b - gap, 2), round(b + gap, 2), 'non-finite input')
        return (round(b, 2), round(b + gap, 2), round(b - gap, 2), 'non-finite input')

    # Signal-time ratios relative to entry. If entry is degenerate (≤ 0),
    # fall back to min-gap. Otherwise clamp ratios to a sane envelope so a
    # rogue strategy that emits stop=$0 or target=$10000 can't blow up the
    # downstream order math.
    why_parts = []
    if e <= 0:
        stop_pct = MIN_BRACKET_GAP_PCT
        target_pct = MIN_BRACKET_GAP_PCT
        why_parts.append('degenerate entry')
    else:
        if side == 'buy':
            stop_pct   = (e - s) / e
            target_pct = (t - e) / e
        else:  # 'sell' (short)
            stop_pct   = (s - e) / e
            target_pct = (e - t) / e
        # Both ratios should be > 0 by construction. If a signal emitted
        # them on the wrong side (stop above entry for long, etc.), fall
        # back to the min gap rather than carry the bug through.
        if stop_pct <= 0:
            stop_pct = MIN_BRACKET_GAP_PCT
            why_parts.append('inverted stop')
        if target_pct <= 0:
            target_pct = MIN_BRACKET_GAP_PCT
            why_parts.append('inverted target')
        # Outer clamp: keep the rebuilt distance reasonable. 50% of base
        # is more than any sane bracket on a daily strategy.
        if stop_pct > 0.5:
            stop_pct = 0.5
            why_parts.append('stop_pct clamped 50%')
        if target_pct > 0.5:
            target_pct = 0.5
            why_parts.append('target_pct clamped 50%')

    # Anchor everything to current base. Floor the gap at MIN_BRACKET_GAP_PCT
    # so Alpaca's $0.01 validity rule never bites.
    stop_pct   = max(stop_pct,   MIN_BRACKET_GAP_PCT)
    target_pct = max(target_pct, MIN_BRACKET_GAP_PCT)

    if side == 'buy':
        new_entry  = b
        new_stop   = b * (1.0 - stop_pct)
        new_target = b * (1.0 + target_pct)
    else:
        new_entry  = b
        new_stop   = b * (1.0 + stop_pct)
        new_target = b * (1.0 - target_pct)

    return (round(new_entry, 2), round(new_stop, 2), round(new_target, 2),
            '; '.join(why_parts))


# ════════════════════════════════════════════════════════════════════════════
# SP-6 Task 10 — OPG dual-path for is_dropped close_only orders
# ════════════════════════════════════════════════════════════════════════════
#
# OPG (market-on-open) was removed 2026-05-19 after the 2026-05-18 paper-fill
# incident (214/224 OPG closes ack'd-but-EXPIRED unfilled while the DB wrongly
# said "closed"). Task 10 RE-INTRODUCES OPG for the SP-6 9:30 reconcile's
# DROP/FLATTEN closes ONLY, behind OPENCLAW_OPEN_CLOSE_MODE, with the ack≠fill
# discipline baked in: the result for an is_dropped close reflects the POLLED
# terminal outcome, never the submit-ack.
#
#   rth_market   (default, safe) — the EXISTING RTH `position close` path.
#                Gate-off / default behaviour is byte-identical to pre-Task-10.
#                For an is_dropped close we ADDITIONALLY poll-to-terminal so the
#                returned status is a confirmed `filled` (with the real fill
#                price), never a bare submit-ack — the ledger-close gate in
#                open_reconcile._resolve_fill_price now requires `filled`.
#   opg_then_day (paper)  — submit a MARKET tif=opg order BEFORE the open; poll
#                to terminal; if it EXPIRES/does-not-fill, cancel (if needed) +
#                resubmit as a tif=day RTH `position close` (the 9:31 day-sweep)
#                and report the SWEEP's polled terminal outcome.
#   opg_live     — OPG at the open + the day-sweep retained as belt-and-suspenders
#                (same code path as opg_then_day for Phase A).
ENV_OPEN_CLOSE_MODE = 'OPENCLAW_OPEN_CLOSE_MODE'
_OPEN_CLOSE_MODES = ('rth_market', 'opg_then_day', 'opg_live')


def _open_close_mode() -> str:
    """Resolve OPENCLAW_OPEN_CLOSE_MODE (default 'rth_market'). Read at call-time
    so tests can monkeypatch os.environ. Unknown values fall back to the safe
    default so a typo can never silently arm OPG."""
    mode = os.environ.get(ENV_OPEN_CLOSE_MODE, 'rth_market')
    return mode if mode in _OPEN_CLOSE_MODES else 'rth_market'


def _fetch_position_qty(ticker: str) -> float:
    """Return the ABSOLUTE current broker position qty for a ticker via
    `alpaca position get`, or 0.0 if no position / lookup fails.

    The OPG `order submit` needs an explicit qty (unlike `position close`, which
    flattens the whole position server-side). The is_dropped close order dict
    carries only signed USD (current_usd/notional_usd), NOT a qty or a per-share
    price, so we read the real qty from the broker rather than dividing by a
    guessed price. Routed through _run_alpaca_cli so tests mock it."""
    ok, payload, _err = _run_alpaca_cli(
        ['position', 'get', '--symbol-or-asset-id', ticker], timeout=10,
    )
    if not ok or not isinstance(payload, dict):
        return 0.0
    try:
        return abs(float(payload.get('qty') or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _opg_terminal_filled(final: dict | None, requested_qty: float) -> bool:
    """True iff a polled OPG/sweep order dict represents a CONFIRMED fill.

    `status=='filled'` OR (requested>0 AND filled_qty>=requested). A None final
    (all polls failed / timed out before terminal) is NEVER a fill. This is the
    single ack≠fill gate the OPG path keys off."""
    if not isinstance(final, dict):
        return False
    status = str(final.get('status') or '').lower()
    if status == 'filled':
        return True
    try:
        filled = abs(float(final.get('filled_qty') or 0.0))
    except (TypeError, ValueError):
        filled = 0.0
    return bool(requested_qty and requested_qty > 0 and filled >= requested_qty)


def _rth_position_close(ticker, coid, *, notional_usd, current_usd, equity, pct_nav):
    """The shared RTH `alpaca position close` submission (full close or partial
    REDUCE via --percentage). Returns (result_dict, order_id).

    Extracted so BOTH the legacy non-dropped close path and the is_dropped
    rth_market / day-sweep paths submit identically; the is_dropped paths then
    poll the returned order_id to terminal before reporting `filled`."""
    notional_oc = abs(float(notional_usd or 0))
    current_oc = abs(float(current_usd or 0))
    cli_args_oc = ['position', 'close', '--symbol-or-asset-id', ticker]
    is_partial_reduce = (current_oc > 0 and notional_oc < current_oc * 0.999)
    pct_oc = None
    if is_partial_reduce:
        pct_oc = round((notional_oc / current_oc) * 100.0, 2)
        pct_oc = max(0.01, min(99.99, pct_oc))
        cli_args_oc += ['--percentage', str(pct_oc)]
    # SP-6 Phase C: resting GTC OCO take-profit/stop legs reserve the full
    # position qty → `position close` 403s "insufficient qty available"
    # (proven 2026-06-04: all 4 EOD orphan closes rejected). Cancel them
    # first AND settle-poll each cancel to terminal (the qty-hold release is
    # async: pending_cancel→canceled; firing the close on the bare ACK can
    # re-403) — the helper returns only after the holds clear. Applies
    # uniformly to full closes AND partial reduces — a REDUCE 403s identically
    # because the full qty is reserved. Gated on OPENCLAW_EOD_RECONCILE: when
    # OFF, ZERO new CLI calls (legacy path byte-identical). Best-effort +
    # guarded: a CLI hang (TimeoutExpired), a cancel failure, or a settle-poll
    # timeout must NEVER abort the close attempt (mirror the crypto call-site
    # guard). Worst-case added latency is bounded: N legs × 10s, N ≤ 4.
    if os.environ.get('OPENCLAW_EOD_RECONCILE') == '1':
        try:
            _cancel_open_equity_orders(ticker)
        except Exception as _cx:   # noqa: BLE001 — close must proceed
            log(f'  ↳ equity order-cancel best-effort failed (continuing to close): {_cx}')
    ok_co, pay_co, err_co = _run_alpaca_cli(cli_args_oc, timeout=15)
    if not ok_co:
        log(f'CLI rc={err_co.get("exit_code",1)} {ticker} (close_only RTH): {err_co.get("error","")}')
        return ({'ticker': ticker, 'status': 'rejected',
                 'reason': err_co.get('error') or 'position close failed',
                 'http': err_co.get('status'), 'body': str(err_co),
                 'tif': 'day', 'order_class': 'simple', 'client_order_id': coid},
                None)
    order_id = ((pay_co or {}).get('id') or (pay_co or {}).get('order_id'))
    notional_co = abs(float((pay_co or {}).get('notional') or equity * pct_nav))
    qty_co_r = int((pay_co or {}).get('qty') or 0)
    entry_approx = round(notional_co / qty_co_r, 4) if qty_co_r > 0 else 0.0
    kind_co = 'REDUCE' if is_partial_reduce else 'CLOSE'
    pct_tag = f' ({pct_oc}%)' if is_partial_reduce else ''
    log(f'↩ {ticker} {kind_co}{pct_tag}  notional≈${notional_co:,.0f}'
        f'  order={order_id or "?"}')
    return ({'ticker': ticker, 'status': 'submitted',
             'qty': qty_co_r, 'notional': notional_co, 'entry': entry_approx,
             'order_id': order_id, 'http': 200,
             'tif': 'day', 'order_class': 'simple', 'client_order_id': coid},
            order_id)


def execute_single(sess, equity, order, run_date):
    """Submit one bracket order. Returns result dict.

    Computes a unique `client_order_id` BEFORE any SKIP early-return so
    every result dict carries a unique coid (the alpaca_submissions table
    has a UNIQUE constraint on client_order_id; an empty string would
    collide on the second SKIP).
    """
    raw_ticker = order['ticker']
    ticker = _normalize_alpaca_symbol(raw_ticker)

    # Compute coid up-front from the RAW ticker + strategy_id so SKIP rows
    # also get a unique key. Alpaca allows 128 chars on client_order_id.
    # close_only orders get a distinguishing `_C` suffix so an open and a
    # subsequent close for the same (date, ticker, strategy_combo) — common
    # under an intraday regime redeploy that flips a position — don't
    # collide on Alpaca's coid uniqueness check. Without this suffix, the
    # CLI would reject with `client_order_id must be unique` AND the
    # record_submission INSERT would crash on the alpaca_submissions
    # coid unique-constraint (only the (run_date, strategy_id, ticker)
    # constraint has ON CONFLICT handling).
    import re as _re
    _coid_ticker = (ticker or raw_ticker or 'UNKNOWN').replace(' ', '_')
    _coid_ticker = _re.sub(r'[^A-Za-z0-9._-]', '_', _coid_ticker)
    _sid_clean = _re.sub(r'[^A-Za-z0-9._-]', '_', order.get('strategy_id') or 'unknown')
    _kind_tag = '_C' if order.get('close_only') else ''
    # Apply truncation to the strategy-combo segment so the kind-tag suffix
    # always lands at the end. Naively appending _C after a 128-char slice
    # would truncate the marker off — exactly the 2026-05-21 USO crash.
    _prefix  = f'AX{run_date.replace("-","")}_{_coid_ticker}_'
    _budget  = max(1, 128 - len(_prefix) - len(_kind_tag))
    coid     = _prefix + _sid_clean[:_budget] + _kind_tag

    # SP-3.1 Phase A: crypto (-USD) is 24/7, fractional-qty, no bracket — and
    # _normalize_alpaca_symbol() returns None for it, which would otherwise
    # SKIP it below. Intercept on the RAW ticker before that check. Covers
    # both open and close_only (the helper branches internally), so neither
    # path is missed. Non-crypto returns None and falls through unchanged.
    # Gated to match the sizer dispatch (regime_blended_sizer_live): when
    # OPENCLAW_INSTRUMENT_CLASS_ROUTING is OFF, crypto is NOT handled and the
    # order falls through to the equity path (which SKIPs -USD as unsupported),
    # preserving the SP-3 gate-OFF equity-byte-identical invariant.
    if os.environ.get('OPENCLAW_INSTRUMENT_CLASS_ROUTING') == '1':
        _crypto_res = _route_crypto_order(order, equity, coid)
        if _crypto_res is not None:
            return _crypto_res

    # Options intercept (SP-5.1a)
    _option_res = _route_option_order(order, equity, coid)
    if _option_res is not None:
        return _option_res

    if ticker is None:
        return {'ticker': raw_ticker, 'status': 'SKIP',
                'reason': f'unsupported on Alpaca paper ({raw_ticker})',
                'client_order_id': coid}
    if ticker in _unsupported_assets:
        return {'ticker': ticker, 'status': 'SKIP',
                'reason': 'cached unsupported (rejected earlier this run)',
                'client_order_id': coid}
    pct_nav = float(order.get('pct_nav') or 0.0)
    entry   = order.get('entry')
    stop    = order.get('stop')
    target  = order.get('t1') or order.get('target')
    side    = 'sell' if str(order.get('direction') or '').lower() == 'short' else 'buy'

    # Orphan-close orders: close the full broker position, no bracket needed.
    if order.get('close_only'):
        session_co = _alpaca_session_kind()
        if session_co == 'closed':
            return {'ticker': ticker, 'status': 'SKIP',
                    'reason': 'close_only: market closed',
                    'client_order_id': coid, 'tif': 'day', 'order_class': 'simple'}

        # ── SP-6 Task 10: is_dropped OPG dual-path ──────────────────────────
        # Drop/flatten closes (run_reconcile builds these with is_dropped=True)
        # branch on OPENCLAW_OPEN_CLOSE_MODE. The defining invariant is ack≠fill:
        # the returned status reflects the POLLED terminal outcome, so the
        # ledger-close in open_reconcile._resolve_fill_price fires only on a
        # confirmed `filled`. The reason-tagged coid keeps its `_C` suffix.
        if order.get('is_dropped'):
            mode = _open_close_mode()
            notional_d = abs(float(order.get('notional_usd') or 0))
            current_d = abs(float(order.get('current_usd') or 0))

            # opg_then_day / opg_live: submit a MARKET tif=opg order BEFORE the
            # open, poll to terminal, and day-sweep on a non-fill. Only meaningful
            # while still pre-open; once RTH the OPG window is gone, so we fall
            # straight to the polled RTH close below.
            if mode in ('opg_then_day', 'opg_live') and session_co == 'premarket':
                qty_opg = int(_fetch_position_qty(ticker))
                if qty_opg < 1:
                    log(f'{ticker} OPG: no broker qty resolved — falling back to RTH day-sweep')
                else:
                    # 2026-07-01: 55 of 73 premarket OPG closes 403'd
                    # "insufficient qty available (requested N, available 0)" —
                    # the prior evening's reattached GTC OCO stop legs reserve
                    # the full position qty overnight; this is the same hold
                    # the RTH path already clears (_rth_position_close). Same
                    # gate, same settle-polling helper, same best-effort guard:
                    # a cancel failure must never abort the close attempt.
                    if os.environ.get('OPENCLAW_EOD_RECONCILE') == '1':
                        try:
                            _cancel_open_equity_orders(ticker)
                        except Exception as _cx:   # noqa: BLE001 — close must proceed
                            log(f'  ↳ equity order-cancel best-effort failed (continuing to OPG): {_cx}')
                    ok_opg, pay_opg, err_opg = _submit_order_via_cli(
                        ticker=ticker, side=side, qty=qty_opg, tif='opg',
                        order_class='simple', target=None, stop=None, coid=coid,
                        order_type='market', extended_hours=False, limit_price=None,
                    )
                    if not ok_opg:
                        # OPG submit itself failed — do NOT day-sweep blindly here;
                        # report rejected so the reconciler counts an error (no
                        # ledger write). The 9:31 sweep can retry on the next pass.
                        log(f'CLI rc={err_opg.get("exit_code",1)} {ticker} (OPG submit): {err_opg.get("error","")}')
                        return {'ticker': ticker, 'status': 'rejected',
                                'reason': err_opg.get('error') or 'OPG submit failed',
                                'http': err_opg.get('status'), 'body': str(err_opg),
                                'tif': 'opg', 'order_class': 'simple', 'client_order_id': coid}
                    opg_order_id = ((pay_opg or {}).get('id')
                                    or (pay_opg or {}).get('order_id'))
                    log(f'↩ {ticker} OPG (pre-open) x{qty_opg}  order={opg_order_id or "?"}')
                    # NEVER ack=fill: poll the OPG order to a terminal status.
                    final_opg = _poll_to_terminal(opg_order_id, timeout_s=3600, interval_s=3)
                    if _opg_terminal_filled(final_opg, qty_opg):
                        filled_qty = int(abs(float((final_opg or {}).get('filled_qty') or qty_opg)))
                        fill_px = (final_opg or {}).get('filled_avg_price')
                        try:
                            fill_px = float(fill_px) if fill_px is not None else 0.0
                        except (TypeError, ValueError):
                            fill_px = 0.0
                        notional_f = round(fill_px * filled_qty, 2) if fill_px > 0 else 0.0
                        log(f'↩ {ticker} OPG FILLED x{filled_qty}  px={fill_px:.2f}'
                            f'  order={opg_order_id or "?"}')
                        return {'ticker': ticker, 'status': 'filled',
                                'qty': filled_qty, 'notional': notional_f, 'entry': fill_px,
                                'order_id': opg_order_id, 'http': 200,
                                'tif': 'opg', 'order_class': 'simple', 'client_order_id': coid}
                    # OPG did NOT fill (the 2026-05-18 case: ack'd then EXPIRED).
                    # Cancel any non-terminal remnant, then fall through to the
                    # tif=day RTH `position close` day-sweep below.
                    opg_status = str((final_opg or {}).get('status') or 'unknown').lower()
                    if opg_status not in _TERMINAL_ORDER_STATUSES and opg_order_id:
                        _run_alpaca_cli(['order', 'cancel', '--order-id', opg_order_id], timeout=10)
                    log(f'{ticker} OPG not filled (status={opg_status}) → firing tif=day day-sweep')

            # Day-sweep / rth_market: RTH `position close` (tif=day), then POLL to
            # terminal so we report a confirmed `filled` (never the submit-ack).
            # If not yet RTH (e.g. opg path with OPG still resolving pre-9:30, or
            # an is_dropped order seen in premarket under rth_market mode), report
            # pending so no ledger-close fires until the sweep runs.
            session_now = _alpaca_session_kind()
            if session_now != 'rth':
                log(f'{ticker} is_dropped close: session={session_now} (not RTH) — '
                    f'deferring to 9:31 day-sweep, reporting pending')
                return {'ticker': ticker, 'status': 'pending',
                        'reason': f'is_dropped close awaiting RTH (session {session_now})',
                        'http': 200, 'tif': 'day', 'order_class': 'simple',
                        'client_order_id': coid}
            res_sweep, sweep_oid = _rth_position_close(
                ticker, coid, notional_usd=notional_d, current_usd=current_d,
                equity=equity, pct_nav=pct_nav,
            )
            if res_sweep.get('status') != 'submitted':
                return res_sweep  # CLI error already shaped as rejected
            # Poll the close order to terminal — ack≠fill. Only report `filled`
            # (with the real fill price) when the broker confirms a fill; an
            # expired/canceled close reports its terminal status with NO price so
            # _resolve_fill_price returns None and the position stays open.
            # req_qty is the share count returned by the CLI's position-close
            # submission. If the CLI omits 'qty' (unusual but possible), we fall
            # back to current_d (the USD notional of the position) and then 1.0.
            # current_d is a USD value, not a share count, so the fallback is
            # intentionally oversized. This is SAFE: _opg_terminal_filled checks
            # filled_qty >= req_qty, so a large USD req_qty causes that comparison
            # to fail → the position is reported as un-filled rather than phantom-
            # closed. The position stays open for the next sweep pass to retry.
            req_qty = float(res_sweep.get('qty') or 0) or current_d or 1.0
            final_sweep = _poll_to_terminal(sweep_oid, timeout_s=300, interval_s=3)
            if _opg_terminal_filled(final_sweep, req_qty):
                filled_qty = int(abs(float((final_sweep or {}).get('filled_qty')
                                           or res_sweep.get('qty') or 0)))
                fill_px = (final_sweep or {}).get('filled_avg_price')
                try:
                    fill_px = float(fill_px) if fill_px is not None else float(res_sweep.get('entry') or 0.0)
                except (TypeError, ValueError):
                    fill_px = float(res_sweep.get('entry') or 0.0)
                notional_f = round(fill_px * filled_qty, 2) if (fill_px > 0 and filled_qty) \
                    else res_sweep.get('notional')
                log(f'↩ {ticker} day-sweep FILLED x{filled_qty}  px={fill_px:.4f}'
                    f'  order={sweep_oid or "?"}')
                res_sweep.update({'status': 'filled', 'qty': filled_qty,
                                  'entry': fill_px, 'notional': notional_f})
                return res_sweep
            term_status = str((final_sweep or {}).get('status') or 'pending').lower()
            log(f'{ticker} day-sweep NOT filled (status={term_status}) — '
                f'no ledger-close; position remains held')
            return {'ticker': ticker, 'status': term_status,
                    'reason': f'is_dropped close not filled (status={term_status})',
                    'order_id': sweep_oid, 'http': 200,
                    'tif': 'day', 'order_class': 'simple', 'client_order_id': coid}

        # ── Legacy (non-dropped) close path — UNCHANGED ─────────────────────
        if session_co == 'rth':
            # RTH: `alpaca position close` flattens by default. For a partial
            # REDUCE (ticker still in signals but target < |current|), the
            # sizer emits close_only with notional_usd = |delta| < |current_usd|;
            # we pass --percentage so only the delta portion is liquidated.
            # Orphan closes (target_usd = 0 → |notional| == |current|) take
            # the full-close branch (no --percentage flag).
            res_oc, _oid = _rth_position_close(
                ticker, coid,
                notional_usd=order.get('notional_usd'),
                current_usd=order.get('current_usd'),
                equity=equity, pct_nav=pct_nav,
            )
            return res_oc
        else:
            # Pre/afterhours: limit order using quoted price, approximate qty
            skip_ext = _skip_extended_hours(ticker, side)
            if skip_ext:
                return {'ticker': ticker, 'status': 'SKIP',
                        'reason': f'close_only ext-hours skip: {skip_ext}',
                        'client_order_id': coid, 'tif': 'day', 'order_class': 'simple'}
            lp_co = _pick_limit_price(ticker, side)
            if lp_co is None:
                return {'ticker': ticker, 'status': 'SKIP',
                        'reason': 'close_only: no quote/trade for limit price',
                        'client_order_id': coid, 'tif': 'day', 'order_class': 'simple'}
            notional_co = equity * pct_nav
            qty_co = max(1, int(notional_co / lp_co)) if lp_co > 0 else 1
            ok_co, pay_co, err_co = _submit_order_via_cli(
                ticker=ticker, side=side, qty=qty_co, tif='day',
                order_class='simple', target=None, stop=None, coid=coid,
                order_type='limit', extended_hours=True, limit_price=lp_co,
            )
            if ok_co:
                # `alpaca order submit` returns the order under key `id` (REST shape).
                # Accept both `id` and `order_id` defensively in case the CLI shape
                # ever shifts; the missing-id case still falls back to '?' for the
                # log line but writes None to the DB so reconcile can detect drift.
                oid_co = (
                    (pay_co or {}).get('id')
                    or (pay_co or {}).get('order_id')
                )
                log(f'↩ {ticker} CLOSE x{qty_co} sh  LIMIT={lp_co:.2f}  notional=${qty_co*lp_co:,.0f}'
                    f'  order={oid_co or "?"}')
                return {'ticker': ticker, 'status': 'submitted', 'qty': qty_co,
                        'notional': qty_co * lp_co, 'entry': lp_co,
                        'order_id': oid_co, 'http': 200,
                        'tif': 'day', 'order_class': 'simple', 'client_order_id': coid}
            log(f'CLI rc={err_co.get("exit_code",1)} {ticker} (close_only ext): {err_co.get("error","")}')
            return {'ticker': ticker, 'status': 'rejected',
                    'reason': err_co.get('error') or 'submit failed',
                    'http': err_co.get('status'), 'body': str(err_co),
                    'tif': 'day', 'order_class': 'simple', 'client_order_id': coid}

    if not (entry and stop and target):
        return {'ticker': ticker, 'status': 'SKIP', 'reason': 'missing levels',
                'client_order_id': coid}

    import math as _math
    try:
        entry, stop, target = float(entry), float(stop), float(target)
    except (TypeError, ValueError):
        return {'ticker': ticker, 'status': 'SKIP', 'reason': 'non-numeric levels',
                'client_order_id': coid}
    if not (_math.isfinite(entry) and _math.isfinite(stop) and _math.isfinite(target)):
        return {'ticker': ticker, 'status': 'SKIP', 'reason': 'non-finite levels (NaN/inf)',
                'client_order_id': coid}
    if entry <= 0:
        return {'ticker': ticker, 'status': 'SKIP', 'reason': 'non-positive entry',
                'client_order_id': coid}

    notional = equity * pct_nav
    qty = max(1, int(notional / entry))
    # Session-aware order shape (Phase 3, 2026-05-19):
    #   - rth        → market day bracket (or simple-on-short workaround below)
    #   - premarket  → limit day simple + --extended-hours
    #   - afterhours → limit day simple + --extended-hours
    #   - closed     → refuse (overnight 20:00–04:00 ET, weekends, holidays)
    # OPG was removed: the May-18 OPG paper-fill failure showed it isn't a
    # reliable substitute for live orders.
    session = _alpaca_session_kind()
    limit_price: float | None = None
    if session == 'rth':
        tif, order_class, ext_hours, order_type = 'day', 'bracket', False, 'market'
    elif session in ('premarket', 'afterhours'):
        # Eligibility filter for ext-hours: us_equity + tradable only.
        # Options/crypto/non-tradable assets skip immediately. Each skip is
        # logged so the next morning's reconcile can see why we deferred.
        skip_reason = _skip_extended_hours(ticker, side)
        if skip_reason:
            log(f'INFO _skip_extended_hours ticker={ticker} reason={skip_reason}')
            return {'ticker': ticker, 'status': 'SKIP',
                    'reason': f'ext-hours skip ({session}): {skip_reason}',
                    'client_order_id': coid,
                    'tif': 'day', 'order_class': 'simple'}
        tif, order_class, ext_hours, order_type = 'day', 'simple', True, 'limit'
        limit_price = _pick_limit_price(ticker, side)
        if limit_price is None:
            log(f'  ✗ {ticker}: no quote/trade for ext-hours limit price — skipping submit')
            return {'ticker': ticker, 'status': 'SKIP',
                    'reason': f'ext-hours skip ({session}): no quote/trade for limit price',
                    'client_order_id': coid,
                    'tif': 'day', 'order_class': 'simple'}
    else:  # 'closed'
        log(f'  ✗ {ticker}: market fully closed (session=closed) — no submit')
        return {'ticker': ticker, 'status': 'SKIP',
                'reason': 'market fully closed (session=closed)',
                'client_order_id': coid,
                'tif': 'day', 'order_class': 'simple'}
    # Shorts (side='sell' to open) can't reliably ride a bracket on Alpaca
    # paper — the parent gets rejected with "bracket orders must be entry
    # orders" when there's any prior fill in the same symbol, and the
    # shortable-asset universe is narrower than longs. Drop to a simple
    # order; downstream stop/target management lives in
    # alpaca_replace_stop.py and the strategy's own exit signals. Logged
    # for visibility (these used to fail silently with 14+ rejections/day).
    # In extended-hours, order_class is already 'simple' so this is a no-op.
    if side == 'sell' and order_class == 'bracket':
        log(f'  ↓ {ticker}: short → simple order_class (bracket-on-short rejected by Alpaca paper)')
        order_class = 'simple'

    # 2026-05-22 incident fix: buys that would close/reverse an existing
    # short (or sells that would close/reverse an existing long) also get
    # rejected by Alpaca with "bracket orders must be entry orders" because
    # the entry leg isn't an opening fill. Detect via broker-positions
    # cache and drop bracket→simple. Cache-miss (load failure or no
    # position) is treated as "open" and stays on bracket.
    if order_class == 'bracket' and _would_close_or_reverse(side, ticker):
        cur_q = _current_broker_qty(ticker)
        log(f'  ↓ {ticker}: {side} would close/reverse current qty={cur_q:+.0f} → simple order_class')
        order_class = 'simple'

    # Pre-flight bracket recompute: re-anchor entry/stop/target to current
    # market quote while preserving the SIGNAL'S ratio shape (stop_pct =
    # |entry-stop|/entry, target_pct = |target-entry|/entry). This both
    # prevents Alpaca's 422 base_price rejections AND keeps the strategy's
    # risk/reward intent intact when the market has drifted between signal
    # generation and submission.
    # Bracket validity has TWO legs Alpaca enforces against base_price:
    #   long: stop <= base - 0.01   AND   target >= base + 0.01
    #   short: stop >= base + 0.01  AND   target <= base - 0.01
    # Fallback (quote fetch fail / degenerate ratios): use absolute snap
    # offsets so the order still passes validation.
    adjusted_notes: list[str] = []
    if order_class == 'bracket':
        # paper-api.alpaca.markets doesn't serve market data — quotes/snapshot
        # endpoints return 404. Hit data.alpaca.markets explicitly. Prefer
        # latestTrade.p (what Alpaca's order endpoint validates against)
        # over the quote midpoint, which can be miles off in wide markets.
        try:
            qr = sess.get(
                f'https://data.alpaca.markets/v2/stocks/{ticker}/snapshot',
                timeout=5,
            )
            if qr.status_code == 200:
                sj = qr.json() or {}
                latest_trade = sj.get('latestTrade') or {}
                latest_quote = sj.get('latestQuote') or {}
                trade_p = float(latest_trade.get('p') or 0.0)
                bid     = float(latest_quote.get('bp') or 0.0)
                ask     = float(latest_quote.get('ap') or 0.0)
                if trade_p > 0:
                    base = trade_p
                elif bid > 0 and ask > 0:
                    base = (bid + ask) / 2.0
                else:
                    base = bid or ask
                if base > 0:
                    new_entry, new_stop, new_target, why = _recompute_bracket_from_quote(
                        entry, stop, target, side, base
                    )
                    if abs(new_stop - stop) > 0.005:
                        adjusted_notes.append(f'stop {stop:.2f}→{new_stop:.2f}')
                    if abs(new_target - target) > 0.005:
                        adjusted_notes.append(f'target {target:.2f}→{new_target:.2f}')
                    if abs(new_entry - entry) > 0.005:
                        adjusted_notes.append(f'entry {entry:.2f}→{new_entry:.2f}')
                    entry, stop, target = new_entry, new_stop, new_target
                    if why:
                        adjusted_notes.append(f'({why})')
            else:
                log(f'  ↯ {ticker}: pre-flight snapshot HTTP {qr.status_code} — submitting unsnapped')
        except Exception as _qe:
            # Don't block submission on a quote fetch hiccup; let Alpaca's
            # own validation be the final word. The 422-retry block below
            # will rescue if Alpaca returns base_price in the error.
            log(f'  ↯ {ticker}: pre-flight quote fetch failed ({type(_qe).__name__}: {_qe}) — submitting unsnapped')

    if adjusted_notes:
        log(f'  ↪ {ticker}: bracket recomputed — {", ".join(adjusted_notes)}')

    try:
        ok, payload, err = _submit_order_via_cli(
            ticker=ticker, side=side, qty=qty, tif=tif,
            order_class=order_class, target=target, stop=stop, coid=coid,
            order_type=order_type, extended_hours=ext_hours, limit_price=limit_price,
        )
        if ok:
            oid = (payload or {}).get('id', '?') if isinstance(payload, dict) else '?'
            if order_type == 'limit':
                log(f'OK  {ticker} {side.upper()} x{qty} sh  LIMIT={limit_price:.2f}  ext_hours={ext_hours}  notional=${notional:,.0f}  order={oid}')
            else:
                log(f'OK  {ticker} {side.upper()} x{qty} sh  entry~{entry:.2f}  TP={target:.2f}  SL={stop:.2f}  notional=${notional:,.0f}  order={oid}')
            return {'ticker': ticker, 'status': 'submitted', 'order_id': oid,
                    'side': side, 'qty': qty, 'notional': notional,
                    'entry': entry, 'stop': stop, 'target': target,
                    'tif': tif, 'order_class': order_class, 'client_order_id': coid,
                    'extended_hours': ext_hours, 'order_type': order_type,
                    'limit_price': limit_price}

        err_text   = (err.get('error') or '').lower()
        err_status = err.get('status')

        # Duplicate client_order_id → recover the existing order so DB stays in sync.
        if err_status == 422 and 'client_order_id' in err_text:
            ok2, payload2, _err2 = _get_order_by_coid(coid)
            if ok2 and isinstance(payload2, dict):
                oid = payload2.get('id', '?')
                log(f'RECOVERED {ticker} (duplicate client_order_id) → existing order={oid}')
                return {'ticker': ticker, 'status': 'recovered', 'order_id': oid,
                        'side': side, 'qty': qty, 'notional': notional,
                        'entry': entry, 'stop': stop, 'target': target,
                        'tif': tif, 'order_class': order_class, 'client_order_id': coid}

        # 422 with base_price violation → Alpaca's error envelope carries the
        # base_price it validated against. Use the authoritative bp to run the
        # full bracket recompute (preserves the signal's stop/target ratios)
        # and retry once. Catches the case where our pre-flight quote was
        # stale or the quote fetch failed entirely. Bracket-only — ext-hours
        # uses simple/limit and never triggers Alpaca's base_price rule.
        if (err_status == 422 and side in ('buy', 'sell') and tif == 'day'
                and order_class == 'bracket'):
            ej     = err.get('error_json') or {}
            bp_raw = ej.get('base_price')
            wants_retry = bp_raw and ('stop_loss' in err_text
                                       or 'take_profit' in err_text
                                       or 'base_price' in err_text)
            if wants_retry:
                try:
                    bp = float(bp_raw)
                except (TypeError, ValueError):
                    bp = 0.0
                if bp > 0:
                    r_entry, r_stop, r_target, r_why = _recompute_bracket_from_quote(
                        entry, stop, target, side, bp
                    )
                    log(f'  ↻ {ticker}: 422 base_price={bp:.2f} — retry with '
                        f'stop {stop:.2f}→{r_stop:.2f}, target {target:.2f}→{r_target:.2f}'
                        + (f' ({r_why})' if r_why else ''))
                    # 422-base_price retry only fires for RTH bracket orders
                    # (extended-hours uses limit + simple — no bracket validation).
                    # Preserve market+no-ext-hours shape here.
                    ok3, payload3, err3 = _submit_order_via_cli(
                        ticker=ticker, side=side, qty=qty, tif=tif,
                        order_class=order_class, target=r_target, stop=r_stop, coid=coid,
                        order_type='market', extended_hours=False, limit_price=None,
                    )
                    if ok3:
                        oid = (payload3 or {}).get('id', '?') if isinstance(payload3, dict) else '?'
                        log(f'OK  {ticker} {side.upper()} x{qty} sh  entry~{r_entry:.2f}  TP={r_target:.2f}  SL={r_stop:.2f}  notional=${notional:,.0f}  order={oid} (recompute-retry)')
                        return {'ticker': ticker, 'status': 'submitted', 'order_id': oid,
                                'side': side, 'qty': qty, 'notional': notional,
                                'entry': r_entry, 'stop': r_stop, 'target': r_target,
                                'tif': tif, 'order_class': order_class, 'client_order_id': coid}
                    err = err3 or err

        body_text = (err.get('error') or '')[:200]
        http      = err.get('status') or 0
        # Persist unsupported-asset signals to the per-run cache so the
        # next signal that targets the same ticker SKIPs cleanly instead
        # of burning another API roundtrip + log line.
        if http and 400 <= http < 500 and _looks_like_unsupported_asset_error(body_text):
            _unsupported_assets.add(ticker)
            log(f'  ✕ {ticker}: cached as unsupported for the rest of this run')
        log(f'CLI rc={err.get("exit_code")} status={http} {ticker}: {body_text}')
        return {'ticker': ticker, 'status': 'error', 'http': http,
                'body': body_text, 'client_order_id': coid,
                'tif': tif, 'order_class': order_class}
    except subprocess.TimeoutExpired as exc:
        log(f'CLI timeout {ticker}: {exc}')
        return {'ticker': ticker, 'status': 'exception', 'reason': f'cli timeout: {exc}',
                'client_order_id': coid, 'tif': tif, 'order_class': order_class}
    except Exception as exc:
        log(f'Exception {ticker}: {exc}')
        return {'ticker': ticker, 'status': 'exception', 'reason': str(exc),
                'client_order_id': coid, 'tif': tif, 'order_class': order_class}


# ── Main ────────────────────────────────────────────────────────────────────

def _close_inflight_key(run_date) -> str:
    return f'execute:close:inflight:{run_date}'


def _executor_redis():
    """Lazy Redis client (decode_responses). None on any failure — the in-flight
    lock is best-effort (its TTL is the backstop); a missing lock never blocks
    submission."""
    try:
        import redis
        return redis.from_url(os.environ.get('REDIS_URL', 'redis://localhost:6379'),
                              socket_connect_timeout=3, decode_responses=True)
    except Exception as e:
        log(f'[executor] redis unavailable: {e}')
        return None


def _set_close_inflight(run_date, ttl=300, _r=None) -> bool:
    """Mark the close-execute phase in-flight so a concurrent intraday-regime
    redeploy defers instead of racing the market-into-close submission."""
    r = _r if _r is not None else _executor_redis()
    if r is None:
        return False
    try:
        r.setex(_close_inflight_key(run_date), ttl, '1')
        return True
    except Exception as e:
        log(f'[executor] close-inflight set failed: {e}')
        return False


def _clear_close_inflight(run_date, _r=None) -> None:
    r = _r if _r is not None else _executor_redis()
    if r is None:
        return
    try:
        r.delete(_close_inflight_key(run_date))
    except Exception as e:
        log(f'[executor] close-inflight clear failed: {e}')


def _handoff_fresh(run_date, _dir=None, _now=None):
    """True if the sized handoff for run_date exists AND was written today (ET).

    Execute-phase gate for the close-execution split: the ~3:55pm execute must
    refuse to trade on a stale/leftover handoff when the ~3:10pm compute didn't
    refresh it today. (The legacy single-run path writes the handoff seconds
    before, so it always reads as fresh.)"""
    from zoneinfo import ZoneInfo
    base = _dir or HANDOFF_DIR
    fpath = base / f'{run_date}_sized.json'
    if not fpath.exists():
        return False, 'sized handoff file missing'
    et = ZoneInfo('America/New_York')
    now = _now or datetime.now(et)
    mtime = datetime.fromtimestamp(fpath.stat().st_mtime, et)
    if mtime.date() != now.date():
        return False, f'sized handoff stale (written {mtime.date()}, today {now.date()})'
    return True, 'fresh'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=str(date.today()))
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    run_date = args.date
    uri = os.environ.get('POSTGRES_URI', '')
    if not uri:
        log('POSTGRES_URI not set — aborting')
        sys.exit(2)   # config error per Tier 3 exit-code discipline

    log(f'Run date: {run_date}')

    handoff = read_handoff(run_date, 'sized')
    if not handoff:
        log('No sized handoff — nothing to execute')
        sys.exit(0)
    fresh, why = _handoff_fresh(run_date)
    if not fresh:
        log(f'Handoff freshness gate: {why} — refusing to execute (compute phase did not produce a fresh handoff)')
        sys.exit(0)
    orders = handoff.get('orders') or []
    if not orders:
        log('Sized handoff contains zero orders — nothing to do')
        sys.exit(0)

    conn = psycopg2.connect(uri)

    # Gate 1: signal quality
    ok, why = check_signal_quality(conn)
    if not ok:
        log(f'Signal-quality gate blocked: {why}')
        conn.close(); sys.exit(0)
    log(f'Signal-quality OK — {why}')

    session = _alpaca_session_kind()
    rth = (session == 'rth')
    _session_descriptions = {
        'rth':        'RTH — submitting bracket day market orders',
        'premarket':  'PREMARKET (04:00–09:30 ET) — submitting simple day LIMIT orders with --extended-hours; us_equity-only',
        'afterhours': 'AFTERHOURS (16:00–20:00 ET) — submitting simple day LIMIT orders with --extended-hours; us_equity-only',
        'closed':     'CLOSED (overnight 20:00–04:00 ET / weekend / holiday) — refusing all submits',
    }
    log(f'Market session: {_session_descriptions.get(session, session)}')

    # Mark the close-execute phase in-flight so a concurrent intraday-regime
    # redeploy defers (300s TTL is the crash backstop). RTH-only — the legacy
    # 10am RTH run also benefits from not racing a redeploy.
    if rth:
        _set_close_inflight(run_date)

    sess    = _alpaca_session()
    account = _fetch_account_state(sess)
    equity  = account['equity']
    long_mv = account['long_market_value']
    regt_bp = account['regt_buying_power']
    _bp_snapshot(account, run_date)

    # Reset per-run unsupported-asset + asset-meta caches.
    _unsupported_assets.clear()
    _asset_cache.clear()

    # Load current broker positions once per run for close/reverse detection.
    _load_broker_positions()

    log(f'Account equity ${equity:,.2f}  long_mv ${long_mv:,.2f}  regt_bp ${regt_bp:,.2f}')
    log(f'[executor] session={session}, {len(orders)} orders queued '
        f'(DTBP guard {"ON" if _dtbp_guard_enabled() else "OFF"})')

    submitted = []
    skipped   = []
    new_notional_total = 0.0

    # Execution priority — must free capital before consuming it:
    #   0: orphan closes (ticker absent from current signals, strategy_id='__close_orphan__')
    #   1: other position reduces/closes (close_only=True, ticker still in signals)
    #       — partial reduces (same direction, smaller target)
    #       — flip closes (strategy_id='__flip_close__'; matched flip_open lives in tier 2/3)
    #   2: short opens / direction sells
    #   3: long opens / position increases
    # Within each tier: largest notional first to maximise freed buying power early.
    orders = sorted(orders, key=_exec_priority_for_test)

    # Direction-flip tracking. The sizer emits flip_close (tier-1,
    # strategy_id='__flip_close__') and flip_open (tier-2/3, real sid) as
    # an atomic pair on the same ticker. Alpaca cannot reverse long↔short
    # in a single bracket order, so the open MUST wait until the close
    # has filled. If the close doesn't fill or is rejected, the matched
    # open is skipped to avoid oversell against the still-existing position.
    flip_tickers = {o['ticker'] for o in orders
                    if (o.get('strategy_id') or '') == '__flip_close__'}
    failed_flip_closes: set[str] = set()
    if flip_tickers:
        log(f'[executor] direction-flip pairs detected for {len(flip_tickers)} tickers: {sorted(flip_tickers)}')

    # DTBP guard: computed lazily at the first opening-tier order so the
    # re-fetched account reflects buying power freed by the closes above.
    guard_on = _dtbp_guard_enabled()
    dtbp_skip_keys: set = set()
    dtbp_ready = False

    for order in orders:
        if guard_on and not dtbp_ready and _exec_priority_for_test(order)[0] >= 2:
            acct2 = _fetch_account_state(sess)
            open_orders = [o for o in orders if _exec_priority_for_test(o)[0] >= 2]
            dtbp_skip_keys = _compute_dtbp_skips(open_orders, acct2, equity)
            dtbp_ready = True
            if dtbp_skip_keys:
                log(f'[dtbp-guard] budget=${_dtbp_opening_budget(acct2):,.0f} '
                    f'(DTBP=${acct2.get("daytrading_buying_power",0):,.0f} '
                    f'regt=${acct2.get("regt_buying_power",0):,.0f}); '
                    f'skipping {len(dtbp_skip_keys)} low-conviction opens')

        sid    = order.get('strategy_id') or 'unknown'
        ticker = order.get('ticker') or '???'

        if guard_on and (ticker, sid) in dtbp_skip_keys \
                and not already_executed(conn, run_date, sid, ticker):
            # Dry-run must stay side-effect-free: log the would-skip, don't
            # write the skipped_dtbp audit row.
            if args.dry_run:
                log(f'DRY {ticker} {sid}  would skip (DTBP budget exhausted)')
            else:
                _record_dtbp_skip(conn, run_date, order, equity)
            skipped.append({'ticker': ticker, 'reason': 'dtbp_budget_exhausted'})
            continue

        # Pair-skip: flip_open for a ticker whose flip_close didn't complete
        # must not be submitted — it would oversell into the still-held
        # current position and never establish the new direction.
        is_flip_close = (sid == '__flip_close__')
        is_flip_open  = (not is_flip_close) and (ticker in flip_tickers)
        if is_flip_open and ticker in failed_flip_closes:
            log(f'  SKIP {ticker} flip_open: matched flip_close did not complete')
            skipped.append({'ticker': ticker, 'reason': 'flip_close did not fill — open skipped'})
            continue

        if already_executed(conn, run_date, sid, ticker):
            skipped.append({'ticker': ticker, 'reason': 'already executed'})
            continue

        pct_nav = float(order.get('pct_nav') or 0.0)
        projected = equity * pct_nav

        if args.dry_run:
            log(f'DRY {ticker} {sid}  would submit {pct_nav*100:.2f}% NAV (~${projected:,.0f})')
            continue

        result = execute_single(sess, equity, order, run_date)
        # Persist *every* attempt — submitted, recovered, AND rejects —
        # so the audit trail in alpaca_submissions reflects what we tried,
        # not just what succeeded. Reject rows carry alpaca_http +
        # alpaca_error and have alpaca_order_id NULL, so already_executed()
        # still treats them as "not yet executed" for retry semantics.
        # tif is always 'day' across all sessions (post-OPG removal 2026-05-19);
        # order_class is 'bracket' in RTH, 'simple' in pre/afterhours/closed-skip.
        record_submission(conn, run_date, order, result,
                          result.get('tif') or 'day',
                          result.get('order_class') or ('bracket' if rth else 'simple'),
                          result.get('client_order_id') or '')

        # Flip-close: block until the order fills so the matched flip_open
        # sees a flat position when it arrives. Treat any non-fill outcome
        # as failure so the matched open is skipped (set tracked above).
        if is_flip_close:
            if result.get('status') in ('submitted', 'recovered') and result.get('order_id'):
                filled = _wait_for_fill(result['order_id'], timeout=15.0)
                if not filled:
                    failed_flip_closes.add(ticker)
                    log(f'  WARN {ticker} flip_close did not fill within 15s; matched open will be skipped')
                else:
                    log(f'  ✓ {ticker} flip_close filled — matched open cleared to submit')
            else:
                failed_flip_closes.add(ticker)
                log(f'  WARN {ticker} flip_close not submitted (status={result.get("status")}); matched open will be skipped')

        if isinstance(result, dict):
            result.setdefault('action', order.get('action'))   # human-readable label for #trade-reports
        if result.get('status') in ('submitted', 'recovered'):
            submitted.append(result)
            new_notional_total += result.get('notional') or 0.0
        else:
            skipped.append({'ticker': ticker, 'reason': result.get('reason') or result.get('body') or result.get('status'),
                            'action': order.get('action')})

    conn.close()
    if rth:
        _clear_close_inflight(run_date)

    log(f'Done — submitted={len(submitted)}  skipped={len(skipped)}  new_notional=${new_notional_total:,.0f}')
    if skipped:
        for s in skipped[:10]:
            log(f'  SKIP {s["ticker"]}: {s["reason"]}')

    # Post a submission summary to #trade-reports whenever any orders were
    # requested — success, partial, or all-reject. Operator wants visibility
    # on every cycle that touched the broker, not just the partial-failure
    # case the old _alert_partial covered.
    if submitted or skipped:
        _post_executor_summary(run_date, submitted, skipped, new_notional_total)


def _format_submitted_sample(submitted, limit: int = 8) -> str:
    """#trade-reports sample line — shows the human-readable `action`
    (reduce_long/flip_to_short/…) so a long-reduction never reads as a 'short'."""
    return ', '.join(
        f'{s.get("ticker","?")}:{(s.get("action") or s.get("direction") or "?")} x{s.get("qty","?")}'
        for s in submitted[:limit]
    )


def _post_executor_summary(run_date, submitted, skipped, notional):
    """Post a one-message summary of the cycle's broker submissions to
    #trade-reports. Always fires when at least one order was attempted —
    no-op when nothing was attempted (the report step covers that)."""
    import requests as _rq
    token = (os.environ.get('DISCORD_BOT_TOKEN')
             or os.environ.get('DATABOT_TOKEN')
             or os.environ.get('BOT_TOKEN', ''))
    if not token:
        log('Discord post skipped: no DISCORD_BOT_TOKEN/DATABOT_TOKEN/BOT_TOKEN in env')
        return
    n_ok = len(submitted)
    n_skip = len(skipped)
    n_total = n_ok + n_skip
    # Status emoji: ✅ all-ok, ⚠️ partial, 🛑 all-reject
    emoji = '✅' if n_skip == 0 else ('🛑' if n_ok == 0 else '⚠️')
    lines = [
        f'{emoji} **Alpaca submissions — {run_date}** — {n_ok}/{n_total} '
        f'orders in · new notional ${notional:,.0f}',
    ]
    if submitted:
        sample = _format_submitted_sample(submitted)
        suffix = ' …' if len(submitted) > 8 else ''
        lines.append(f'• submitted: {sample}{suffix}')
    if skipped:
        reasons = ', '.join(
            f"{s['ticker']}({(s['reason'] or 'unknown')[:40]})"
            for s in skipped[:8]
        )
        suffix = ' …' if len(skipped) > 8 else ''
        lines.append(f'• rejected:  {reasons}{suffix}')
    _bp_line = _dtbp_summary_line(skipped)
    if _bp_line:
        lines.append(_bp_line)
    msg = '\n'.join(lines)[:1900]
    headers = {'Authorization': f'Bot {token}'}
    try:
        r = _rq.get('https://discord.com/api/v10/users/@me/guilds', headers=headers, timeout=5)
        if not r.ok:
            log(f'Discord guild lookup failed: {r.status_code}')
            return
        cid = None
        for g in r.json():
            rc = _rq.get(f"https://discord.com/api/v10/guilds/{g['id']}/channels", headers=headers, timeout=5)
            if not rc.ok:
                continue
            for ch in rc.json():
                if ch.get('name') == 'trade-reports' and ch.get('type') == 0:
                    cid = ch['id']
                    break
            if cid:
                break
        if not cid:
            log('Discord post skipped: #trade-reports not found in any visible guild')
            return
        post = _rq.post(
            f'https://discord.com/api/v10/channels/{cid}/messages',
            headers={**headers, 'Content-Type': 'application/json'},
            json={'content': msg}, timeout=5,
        )
        if not post.ok:
            log(f'Discord post failed: {post.status_code} {post.text[:200]}')
    except Exception as e:
        log(f'executor-summary post failed: {e}')


if __name__ == '__main__':
    main()
