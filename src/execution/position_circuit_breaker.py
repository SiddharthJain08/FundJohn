#!/usr/bin/env python3
"""Intraday 5-min circuit breaker for consolidate-mode positions.

In Phase 2 (default): logging-only — does NOT submit close orders.
Phase 3 flips OPENCLAW_REGIME_BLENDED_LIVE=1 and breaker fires real
closes via _close_symbol().

Independent-mode positions (HIGH_VOL/CRISIS) are skipped — strategy-level
brackets are their cutoff.

Spec: docs/superpowers/specs/2026-05-11-regime-blended-position-sizing-design.md §"position_circuit_breaker"
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

LIVE_FLAG_ENV = 'OPENCLAW_REGIME_BLENDED_LIVE'


def should_fire_breaker(position: dict, nav: float, threshold_pct: float) -> tuple[bool, float]:
    """Return (fire?, unrealized_pnl_pct_nav).

    Position dict shape comes from alpaca_trader.get_positions():
      symbol, qty (signed), avg_entry_price, current_price, unrealized_plpc, side,
      market_value. Legacy callers may pass {ticker, mark, ...} — accept both.
    Fires only on LOSS exceeding |threshold_pct| of NAV (positive moves don't fire).
    """
    qty = float(position['qty'])
    entry = float(position['avg_entry_price'])
    mark = float(position.get('current_price', position.get('mark', 0)))
    unrealized = (mark - entry) * qty
    ratio = unrealized / nav if nav > 0 else 0.0
    return (abs(ratio) > threshold_pct and ratio < 0), ratio


def format_breaker_message(ticker: str, ratio: float, threshold_pct: float, qty: float) -> str:
    """Format a human-readable circuit-breaker fire message."""
    return (f':rotating_light: **Circuit breaker** {ticker} '
            f'unrealized {ratio*100:.2f}% NAV '
            f'(threshold {threshold_pct*100:.2f}%, qty {qty})')


def main():
    """Scan broker positions; fire breaker on losses exceeding threshold for consolidate-mode regimes."""
    import psycopg2
    import psycopg2.extras

    uri = os.environ.get('POSTGRES_URI')
    if not uri:
        print('[circuit_breaker] POSTGRES_URI not set; aborting')
        return

    conn = psycopg2.connect(uri)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Current regime (consolidate-mode only).
    cur.execute("SELECT state FROM market_regime ORDER BY updated_at DESC LIMIT 1")
    row = cur.fetchone()
    if not row:
        print('[circuit_breaker] no market_regime; aborting')
        conn.close()
        return
    regime_state = row['state']
    # Regime gate removed 2026-05-16: with sharpe_cadence-LIVE running in
    # every regime, there's no "independent-mode strategy bracket" backstop
    # in HIGH_VOL/CRISIS anymore — losses can compound there too. The
    # breaker now fires in all four regimes, with each regime's own
    # threshold from regime_sizer_params (typically tighter in HIGH_VOL
    # and CRISIS).
    cur.execute("SELECT position_circuit_breaker_pct FROM regime_sizer_params WHERE regime_state=%s",
                (regime_state,))
    threshold_row = cur.fetchone()
    if not threshold_row:
        print(f'[circuit_breaker] no regime_sizer_params row for {regime_state}; aborting')
        conn.close()
        return
    threshold_pct = float(threshold_row['position_circuit_breaker_pct'])

    # Imports that may fail in non-prod environments — keep them lazy.
    try:
        from execution.alpaca_trader import _alpaca_session, _fetch_account_state, get_positions
        from execution.regime_liquidator import _close_symbol, _post_to_discord, _market_is_open
    except ImportError as e:
        print(f'[circuit_breaker] missing dependency ({e}); aborting')
        conn.close()
        return

    if not _market_is_open():
        print('[circuit_breaker] market closed; skipping')
        conn.close()
        return

    # _alpaca_session() attaches auth headers + sess._base — required by
    # alpaca_trader.{_fetch_account_state, get_positions}.
    sess = _alpaca_session()
    try:
        account = _fetch_account_state(sess)
        nav = float(account['equity'])
    except Exception as e:
        print(f'[circuit_breaker] account fetch failed ({e}); aborting')
        conn.close()
        return

    try:
        positions = get_positions(sess)
    except Exception as e:
        print(f'[circuit_breaker] position list failed ({e}); aborting')
        conn.close()
        return

    live = os.environ.get(LIVE_FLAG_ENV, '0') == '1'
    fired = 0

    for pos in positions:
        # alpaca_trader.get_positions() returns 'symbol' not 'ticker'.
        ticker = pos.get('symbol') or pos.get('ticker')
        fire, ratio = should_fire_breaker(pos, nav, threshold_pct)
        if not fire:
            continue
        msg = format_breaker_message(ticker, ratio, threshold_pct, pos['qty'])
        if live:
            try:
                ok, payload = _close_symbol(ticker, pos['qty'], market_open=True)
            except Exception as e:
                ok, payload = False, {'error': str(e)}
            cur.execute("""
              INSERT INTO circuit_breaker_fires
                (ts_utc, ticker, unrealized_pnl_pct_nav, threshold_pct, position_qty, close_result_json)
              VALUES (%s, %s, %s, %s, %s, %s)
            """, (datetime.now(timezone.utc), ticker, ratio, threshold_pct, pos['qty'],
                  json.dumps(payload)))
            conn.commit()
            try:
                _post_to_discord('trade-reports', msg + ('\n• Closed live' if ok else f'\n• Close FAILED: {payload}'))
            except Exception as e:
                print(f'[circuit_breaker] Discord post failed: {e}')
            fired += 1
        else:
            print(f'[circuit_breaker] DRY-RUN would fire: {msg}')
            cur.execute("""
              INSERT INTO circuit_breaker_fires
                (ts_utc, ticker, unrealized_pnl_pct_nav, threshold_pct, position_qty, close_result_json)
              VALUES (%s, %s, %s, %s, %s, %s)
            """, (datetime.now(timezone.utc), ticker, ratio, threshold_pct, pos['qty'],
                  json.dumps({'dry_run': True, 'message': msg})))
            conn.commit()
            fired += 1

    print(f'[circuit_breaker] {"LIVE" if live else "DRY-RUN"} regime={regime_state} threshold={threshold_pct*100:.2f}% nav=${nav:,.0f} fired={fired}')
    conn.close()


if __name__ == '__main__':
    main()
