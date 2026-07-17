"""Tests for the by-ticker closed-position digest in send_report.py.

These cover the PURE aggregation + formatting layer (no DB). The DB
fetch/enrich layer (`_load_closed_positions`) is verified separately
against the live read-only DB.

Dollar-P&L modelling note: the broker nets ONE position per ticker, and
`alpaca_submissions.strategy_id` is a pipe-delimited bundle of the
co-firing strategies — so dollar P&L cannot be attributed per strategy.
It is therefore estimated at the TICKER level:
    est_ticker_dollar_pnl = ticker_notional_usd * mean(realized_pct of legs)
Summing per-leg dollars would multiply-count a single netted position.

Row contract consumed by the pure layer — one dict per closed leg
(per (ticker, strategy)); `ticker_notional_usd` is a ticker property
repeated on every leg of that ticker (None when no fill is known):
    {
        'ticker', 'strategy_id', 'direction',
        'realized_pct': float,          # fraction, e.g. -0.0807 == -8.07%
        'days_held': int,
        'close_reason': str,            # stop_loss / target_1 / ...
        'ticker_notional_usd': float | None,
        'oue_kind': str,                # over / under / expected (computed live)
        'oue_sigma_delta': float | None,
    }
"""
from __future__ import annotations

import math

from src.execution.send_report import (
    _aggregate_closed_positions,
    _fmt_closed_positions_digest,
)


def _fixture_rows():
    return [
        # USO closed in 3 strategies same day; one netted broker position
        # ($1000 notional), so the ticker dollar estimate is computed once.
        {'ticker': 'USO', 'strategy_id': 'S_a', 'direction': 'LONG',
         'realized_pct': -0.0807, 'days_held': 7, 'close_reason': 'stop_loss',
         'ticker_notional_usd': 1000.0, 'oue_kind': 'under', 'oue_sigma_delta': -2.5},
        {'ticker': 'USO', 'strategy_id': 'S_b', 'direction': 'LONG',
         'realized_pct': -0.0807, 'days_held': 7, 'close_reason': 'stop_loss',
         'ticker_notional_usd': 1000.0, 'oue_kind': 'under', 'oue_sigma_delta': -2.5},
        {'ticker': 'USO', 'strategy_id': 'S_c', 'direction': 'LONG',
         'realized_pct': -0.0807, 'days_held': 7, 'close_reason': 'stop_loss',
         'ticker_notional_usd': 1000.0, 'oue_kind': 'expected', 'oue_sigma_delta': 0.0},
        # AAPL winner, $5000 notional.
        {'ticker': 'AAPL', 'strategy_id': 'S_a', 'direction': 'LONG',
         'realized_pct': 0.05, 'days_held': 3, 'close_reason': 'target_1',
         'ticker_notional_usd': 5000.0, 'oue_kind': 'over', 'oue_sigma_delta': 2.1},
        # MU loser (short), notional unknown -> dollar estimate omitted.
        {'ticker': 'MU', 'strategy_id': 'S_x', 'direction': 'SHORT',
         'realized_pct': -0.09, 'days_held': 2, 'close_reason': 'stop_loss',
         'ticker_notional_usd': None, 'oue_kind': 'expected', 'oue_sigma_delta': -1.0},
    ]


def test_aggregate_empty_returns_zeroed_summary():
    agg = _aggregate_closed_positions([])
    assert agg['total_closed'] == 0
    assert agg['n_tickers'] == 0
    assert agg['tickers'] == []
    assert agg['oue'] == {'over': 0, 'under': 0, 'expected': 0}


def test_aggregate_groups_by_ticker_with_strategy_legs():
    agg = _aggregate_closed_positions(_fixture_rows())
    assert agg['total_closed'] == 5
    assert agg['n_tickers'] == 3
    by_ticker = {t['ticker']: t for t in agg['tickers']}
    assert set(by_ticker) == {'USO', 'AAPL', 'MU'}
    # USO has 3 strategy legs preserved (not collapsed).
    assert by_ticker['USO']['n'] == 3
    assert {leg['strategy_id'] for leg in by_ticker['USO']['legs']} == {'S_a', 'S_b', 'S_c'}


def test_aggregate_win_loss_and_oue_counts():
    agg = _aggregate_closed_positions(_fixture_rows())
    assert agg['wins'] == 1          # AAPL only
    assert agg['losses'] == 4        # 3 USO + MU
    assert math.isclose(agg['win_rate'], 1 / 5)
    # O/U/E invariant: over + under + expected == total closed.
    assert agg['oue'] == {'over': 1, 'under': 2, 'expected': 2}
    assert sum(agg['oue'].values()) == agg['total_closed']
    assert agg['by_reason'] == {'stop_loss': 4, 'target_1': 1}


def test_aggregate_ticker_level_dollar_estimate():
    agg = _aggregate_closed_positions(_fixture_rows())
    by_ticker = {t['ticker']: t for t in agg['tickers']}
    # USO: $1000 notional * mean(-8.07%) = -80.7 (computed once, not per leg).
    assert math.isclose(by_ticker['USO']['est_dollar_pnl'], 1000.0 * -0.0807)
    # AAPL: $5000 * +5% = +250.
    assert math.isclose(by_ticker['AAPL']['est_dollar_pnl'], 250.0)
    # MU: notional unknown -> no dollar estimate.
    assert by_ticker['MU']['est_dollar_pnl'] is None
    # Net dollar = sum of known ticker estimates only; 2 tickers priced.
    assert math.isclose(agg['net_dollar_pnl'], 1000.0 * -0.0807 + 250.0)
    assert agg['dollar_known_tickers'] == 2


def test_aggregate_avg_realized_pct():
    agg = _aggregate_closed_positions(_fixture_rows())
    expected = (-0.0807 * 3 + 0.05 - 0.09) / 5
    assert math.isclose(agg['avg_realized_pct'], expected, rel_tol=1e-9)


def test_aggregate_avg_days_held():
    agg = _aggregate_closed_positions(_fixture_rows())
    expected = (7 + 7 + 7 + 3 + 2) / 5
    assert math.isclose(agg['avg_days_held'], expected, rel_tol=1e-9)


def test_aggregate_tickers_sorted_by_avg_realized_desc():
    agg = _aggregate_closed_positions(_fixture_rows())
    order = [t['ticker'] for t in agg['tickers']]
    assert order == ['AAPL', 'USO', 'MU']   # +5% , -8.07% , -9%


def test_fmt_digest_empty_has_no_attachment():
    summary, file_text = _fmt_closed_positions_digest('2026-05-28', [])
    assert '2026-05-28' in summary
    assert 'no positions closed' in summary.lower()
    assert file_text == ''


def test_fmt_digest_reports_all_tickers_and_counts():
    summary, file_text = _fmt_closed_positions_digest('2026-05-28', _fixture_rows())
    # Summary headline carries the close count and the date.
    assert '5' in summary
    assert '2026-05-28' in summary
    # O/U/E line present with live counts.
    assert 'xpected' in summary  # "Expected"/"expected"
    # Full breakdown lists every ticker and every USO strategy leg.
    for tok in ('USO', 'AAPL', 'MU', 'S_a', 'S_b', 'S_c', 'S_x'):
        assert tok in file_text
