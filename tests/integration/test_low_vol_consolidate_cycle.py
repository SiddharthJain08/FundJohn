"""End-to-end integration test: 8 strategies × 5 tickers in LOW_VOL.

Verifies the full regime_blended_sizer pipeline:
  1. Signals enriched with kelly_p (via _kelly.enrich_with_kelly)
  2. Cadence gate passes all (no prior fire dates)
  3. ticker_consolidator nets per-ticker
  4. Mocked TradeJohn approves all (no LLM cost in tests)
  5. Output: 5 per-ticker orders with attribution_weights summing to 1.0

Signal format: direction is numeric (1=LONG, -1=SHORT), entry_price/stop_loss/take_profit_1
for ticker_consolidator bracket + target_1 for enrich_with_kelly (which checks 't1' first,
then 'target_1'). signal_id is an integer to match consolidation_contributions UUID schema.

All bracket geometries use R=2 (or near-2) and p_t1 >= 0.55 → kelly >= 0.325 > 0.
"""
from __future__ import annotations

import sys
import uuid
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

pytestmark = pytest.mark.integration


def _sig(sid, ticker, direction, p_t1, entry, stop, t1, memo_mult=1.0):
    """Build a signal dict compatible with both enrich_with_kelly and ticker_consolidator.

    direction: 1 (LONG) or -1 (SHORT)
    Uses target_1 (for enrich_with_kelly fallback) AND take_profit_1 (for bracket lookup).
    signal_id is a stable integer derived from the strategy and ticker.
    """
    sig_id = abs(hash((sid, ticker, direction))) % (2 ** 53)  # safe BIGINT range
    return {
        'signal_id': sig_id,
        'strategy_id': sid,
        'ticker': ticker,
        'direction': direction,
        'p_t1': p_t1,
        # enrich_with_kelly checks 't1' first; we supply 'target_1' as fallback
        'target_1': t1,
        'take_profit_1': t1,   # ticker_consolidator bracket
        'entry_price': entry,
        'stop_loss': stop,
        'strategy_memo_mult': memo_mult,
    }


def _build_signals():
    """8 strategies × 5 tickers.

    All brackets have R = reward/risk >= 1.875 and p_t1 >= 0.55 → kelly >= 0.325 > 0.

    AAPL: 3 longs (S1, S2, S3) + 1 short (S4)
      Long kelly: p=0.60→k=0.40, p=0.55→k=0.325, p=0.65→k=0.475
      Short kelly: p=0.55→k=0.325 (R=2, entry=100, stop=105, t1=90)
      Net direction: long (Σlong_kelly > Σshort_kelly)

    MSFT: 1 long (S5) — single signal, weight=1.0

    GOOGL: 2 shorts (S6, S7) → net short
      Entry=150, stop=158, t1=135 → R=(150-135)/(158-150)=15/8=1.875

    AMZN: 1 long (S2b, re-use S2 id but different ticker) — single signal

    NVDA: 1 long (S8) — single signal
    """
    return [
        # AAPL longs
        _sig('S1', 'AAPL', 1, p_t1=0.60, entry=100, stop=95, t1=110),
        _sig('S2', 'AAPL', 1, p_t1=0.55, entry=100, stop=95, t1=110),
        _sig('S3', 'AAPL', 1, p_t1=0.65, entry=100, stop=95, t1=110),
        # AAPL short (one opposing signal)
        _sig('S4', 'AAPL', -1, p_t1=0.55, entry=100, stop=105, t1=90),
        # MSFT single long
        _sig('S5', 'MSFT', 1, p_t1=0.60, entry=200, stop=190, t1=220),
        # GOOGL two shorts
        _sig('S6', 'GOOGL', -1, p_t1=0.55, entry=150, stop=158, t1=135),
        _sig('S7', 'GOOGL', -1, p_t1=0.60, entry=150, stop=158, t1=135),
        # AMZN single long
        _sig('S2-AMZN', 'AMZN', 1, p_t1=0.60, entry=180, stop=170, t1=200),
        # NVDA single long
        _sig('S8', 'NVDA', 1, p_t1=0.60, entry=800, stop=760, t1=880),
    ]


def _fake_confirmer(proposals, runner=None):
    """Approve all proposals at full size — no LLM invocation."""
    return {
        p['ticker']: {'action': 'approve', 'multiplier': 1.0, 'rationale': 'test-auto-approve'}
        for p in proposals
    }


def _make_strategy_state(signals):
    """Bootstrap state — no cadence blocks for any strategy."""
    return {
        sig['strategy_id']: {
            'last_fire_date': None,
            'next_fire_date': None,
            'avg_holding_days': 1.0,
            'source': 'bootstrap_daily',
        }
        for sig in signals
    }


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: pipeline logic (no DB)
# ─────────────────────────────────────────────────────────────────────────────

def test_low_vol_consolidate_cycle_produces_per_ticker_orders():
    """Full pipeline: 9 signals across 5 tickers → 5 per-ticker orders in LOW_VOL.

    AAPL: 3 longs + 1 short → net LONG (sum_kelly_long > sum_kelly_short)
    GOOGL: 2 shorts → net SHORT
    MSFT, AMZN, NVDA: single signal each → direction matches signal, weight=1.0
    """
    from execution.regime_blended_sizer import size_positions

    signals = _build_signals()
    account = {
        'equity': 100_000,
        'regt_buying_power': 400_000,
        'long_market_value': 0,
        'cash': 100_000,
    }
    regime = {'state': 'LOW_VOL'}
    params = {
        'liquidity_param': 1.0,
        'min_signal_notional_usd': 100,
        'position_circuit_breaker_pct': 0.02,
    }
    state = _make_strategy_state(signals)
    confirmer_calls = []

    def recording_confirmer(proposals, runner=None):
        confirmer_calls.append(list(proposals))
        return _fake_confirmer(proposals, runner)

    orders = size_positions(
        signals=signals,
        account_state=account,
        regime=regime,
        run_date=date(2026, 5, 12),
        strategy_state=state,
        regime_params=params,
        confirmer=recording_confirmer,
    )

    # ── 5 per-ticker orders ───────────────────────────────────────────────────
    assert len(orders) == 5, (
        f'expected 5 ticker orders, got {len(orders)}: {[o["ticker"] for o in orders]}'
    )
    tickers = sorted(o['ticker'] for o in orders)
    assert tickers == ['AAPL', 'AMZN', 'GOOGL', 'MSFT', 'NVDA']

    # ── AAPL: net long (3 longs dominate 1 short by kelly weight) ────────────
    aapl = next(o for o in orders if o['ticker'] == 'AAPL')
    assert aapl['direction'] == 1, f'AAPL expected LONG (1), got {aapl["direction"]}'
    assert len(aapl['contributions']) == 4, (
        f'AAPL should have 4 contributions (3 long + 1 short), got {len(aapl["contributions"])}'
    )
    aapl_weight_sum = sum(c['attribution_weight'] for c in aapl['contributions'])
    assert aapl_weight_sum == pytest.approx(1.0, abs=1e-6), (
        f'AAPL attribution weights sum to {aapl_weight_sum}, not 1.0'
    )

    # ── GOOGL: net short (both signals short) ────────────────────────────────
    googl = next(o for o in orders if o['ticker'] == 'GOOGL')
    assert googl['direction'] == -1, f'GOOGL expected SHORT (-1), got {googl["direction"]}'
    assert len(googl['contributions']) == 2
    googl_weight_sum = sum(c['attribution_weight'] for c in googl['contributions'])
    assert googl_weight_sum == pytest.approx(1.0, abs=1e-6)

    # ── MSFT, AMZN, NVDA: single signal → weight=1.0 ────────────────────────
    for t in ['MSFT', 'AMZN', 'NVDA']:
        order = next(o for o in orders if o['ticker'] == t)
        assert len(order['contributions']) == 1, f'{t} should have 1 contribution'
        assert order['contributions'][0]['attribution_weight'] == pytest.approx(1.0), (
            f'{t} single-signal attribution_weight should be 1.0'
        )

    # ── TradeJohn confirmer called exactly once with all 5 proposals ──────────
    assert len(confirmer_calls) == 1, (
        f'confirmer should be called once, got {len(confirmer_calls)}'
    )
    assert len(confirmer_calls[0]) == 5, (
        f'confirmer should receive 5 proposals (one per ticker), got {len(confirmer_calls[0])}'
    )

    # ── All orders are source_mode='consolidate' (LOW_VOL path) ──────────────
    assert all(o['source_mode'] == 'consolidate' for o in orders), (
        f'All orders must be source_mode=consolidate; got: {[o.get("source_mode") for o in orders]}'
    )

    # ── Each order has a positive notional ───────────────────────────────────
    assert all(o['notional_usd'] > 0 for o in orders), (
        'All orders should have positive notional'
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: DB write (requires Postgres; rolls back via fixture)
# ─────────────────────────────────────────────────────────────────────────────

def test_low_vol_consolidate_cycle_writes_attribution_to_db(db_conn):
    """Verify per-strategy attribution can be persisted to consolidation_contributions.

    Uses real Postgres connection from conftest.db_conn fixture, which rolls back
    at teardown so no rows leak between test runs.
    """
    from execution.regime_blended_sizer import size_positions

    signals = _build_signals()
    account = {
        'equity': 100_000,
        'regt_buying_power': 400_000,
        'long_market_value': 0,
        'cash': 100_000,
    }
    regime = {'state': 'LOW_VOL'}
    params = {
        'liquidity_param': 1.0,
        'min_signal_notional_usd': 100,
        'position_circuit_breaker_pct': 0.02,
    }
    state = _make_strategy_state(signals)

    orders = size_positions(
        signals=signals,
        account_state=account,
        regime=regime,
        run_date=date(2026, 5, 12),
        strategy_state=state,
        regime_params=params,
        confirmer=_fake_confirmer,
    )
    assert len(orders) == 5, f'expected 5 orders before DB write, got {len(orders)}'

    # ── Simulate caller writing attribution rows ───────────────────────────────
    cur = db_conn.cursor()

    rows_inserted = 0
    consolidated_ids = []  # Track IDs for later verification
    for order_idx, o in enumerate(orders):
        # Generate a stable but unique UUID per order (using uuid4 for randomness)
        consolidated_id = uuid.uuid4()
        consolidated_ids.append(consolidated_id)
        for c in o['contributions']:
            # contributing_signal_id is also now a UUID
            contributing_id = uuid.uuid4()
            cur.execute(
                """
                INSERT INTO consolidation_contributions
                  (consolidated_signal_id, contributing_signal_id, strategy_id,
                   signal_position_size_usd, attribution_weight, contributed_direction)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    str(consolidated_id),
                    str(contributing_id),
                    c['strategy_id'],
                    float(c['signal_position_size_usd']),
                    float(c['attribution_weight']),
                    int(c['contributed_direction']),
                ),
            )
            rows_inserted += 1

    # ── Verify the rows we inserted exist in the transaction ─────────────────
    expected_rows = sum(len(o['contributions']) for o in orders)
    assert expected_rows == rows_inserted, (
        f'Inserted {rows_inserted} rows but expected {expected_rows}'
    )

    # Verify all rows were inserted (now using UUID, so just count total)
    cur.execute('SELECT COUNT(*) FROM consolidation_contributions')
    actual = cur.fetchone()[0]
    # Note: This may include rows from other tests if run in parallel; check at least our expected count
    assert actual >= expected_rows, (
        f'Expected at least {expected_rows} rows in consolidation_contributions, found {actual}'
    )

    # ── Verify attribution weights sum to 1.0 per ticker (from DB) ───────────
    for consolidated_id in consolidated_ids:
        cur.execute(
            """
            SELECT SUM(attribution_weight)
            FROM consolidation_contributions
            WHERE consolidated_signal_id = %s
            """,
            (str(consolidated_id),),
        )
        result = cur.fetchone()
        weight_sum = result[0] if result[0] is not None else 0.0
        assert abs(weight_sum - 1.0) < 1e-5, (
            f'consolidated_signal_id={consolidated_id}: attribution weights sum to '
            f'{weight_sum}, not 1.0'
        )

    # ── Teardown: db_conn fixture rolls back, leaving no persistent rows ──────
