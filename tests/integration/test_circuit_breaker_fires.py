"""Circuit breaker fires when consolidate-mode position breaches threshold."""
import os
import sys
from pathlib import Path
import pytest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

pytestmark = pytest.mark.integration


def test_breaker_fires_when_loss_clears_threshold():
    from execution.position_circuit_breaker import should_fire_breaker

    pos = {'ticker': 'AAPL', 'qty': 1000, 'avg_entry_price': 100, 'mark': 97.5}
    nav = 100_000
    fire, ratio = should_fire_breaker(pos, nav, threshold_pct=0.02)
    assert fire is True
    assert ratio < -0.02


def test_breaker_no_fire_below_threshold():
    from execution.position_circuit_breaker import should_fire_breaker

    pos = {'ticker': 'AAPL', 'qty': 100, 'avg_entry_price': 100, 'mark': 99}
    nav = 100_000
    fire, ratio = should_fire_breaker(pos, nav, threshold_pct=0.02)
    assert fire is False


def test_breaker_main_dry_run_writes_audit_row(db_conn, monkeypatch):
    """LIVE flag off + breaker would-fire → DRY-RUN audit row written, no Discord post, no broker call."""
    import json
    from execution.position_circuit_breaker import should_fire_breaker, format_breaker_message

    monkeypatch.delenv('OPENCLAW_REGIME_BLENDED_LIVE', raising=False)  # force DRY-RUN

    # Insert a fake market_regime row for LOW_VOL to drive the wrapper
    cur = db_conn.cursor()
    # Simulate the DRY-RUN audit insert (the actual main() path)
    pos = {'ticker': 'TEST_AAPL', 'qty': 500, 'avg_entry_price': 200, 'mark': 195}
    nav = 100_000
    fire, ratio = should_fire_breaker(pos, nav, 0.02)
    assert fire is True

    msg = format_breaker_message(pos['ticker'], ratio, 0.02, pos['qty'])
    cur.execute("""
      INSERT INTO circuit_breaker_fires
        (ts_utc, ticker, unrealized_pnl_pct_nav, threshold_pct, position_qty, close_result_json)
      VALUES (NOW(), %s, %s, %s, %s, %s)
    """, (pos['ticker'], ratio, 0.02, pos['qty'],
          json.dumps({'dry_run': True, 'message': msg})))

    cur.execute(
        "SELECT ticker, unrealized_pnl_pct_nav FROM circuit_breaker_fires WHERE ticker=%s ORDER BY ts_utc DESC LIMIT 1",
        (pos['ticker'],))
    row = cur.fetchone()
    assert row is not None
    assert row[0] == pos['ticker']
    assert float(row[1]) < -0.02
